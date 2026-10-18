import logging
import re
from urllib.parse import urljoin, urlparse

from config import get_solver_proxy_url, is_direct_connection_allowed
from extractors.base import BaseExtractor, ExtractorError
from services.flaresolverr import FlareSolverrError, solve_cloudflare
from utils.packed import eval_solver

logger = logging.getLogger(__name__)

_TURNSTILE_GATE_RE = re.compile(
    r"challenges\.cloudflare\.com/turnstile|cf-turnstile", re.IGNORECASE
)
_EMBED_CODE_RE = re.compile(r"/e/([A-Za-z0-9_-]+)")

_MEDIA_PATTERNS = [
    r'file:"(.*?)"',
    r'sources:\s*\[\s*\{\s*file:\s*"([^"]+)"',
    r'src\("(.*?)"\)',
    r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
    r'https?://[^"\'\s]+\.mp4[^"\'\s]*',
]


class DroploadExtractor(BaseExtractor):
    """Dropload URL extractor.

    The embed page can sit behind a Cloudflare Turnstile gate. FlareSolverr
    clicks the widget; the page callback then submits ``op=embed`` and the
    browser player immediately consumes the stream token in the returned
    page.  Re-submitting the form over HTTP with the solver cookies (which
    carry the cleared ``vcap`` captcha marker) returns a fresh player page
    whose token has not been consumed yet.

    With a trusted egress IP the media can already be in the first response, so
    that page is checked before paying for a solver round-trip.  Real gate pages
    carry no stream at all: there FlareSolverr stays mandatory, and a missing
    solver is reported immediately instead of guessing.
    """

    TURNSTILE_TABS = 1
    REQUEST_TIMEOUT_TOTAL = 90
    # The Turnstile solve happens in an IPv4-only browser and the cleared
    # ``vcap`` marker is IP-bound, so every request must leave via IPv4.
    force_ipv4 = True

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="dropload", bypass_warp=bypass_warp)

    @staticmethod
    def _media_from_html(text: str) -> str | None:
        """Media URL already present in a page, m3u8 before mp4."""
        for ext in ("m3u8", "mp4"):
            match = re.search(rf'https?://[^"\'\s]+\.{ext}[^"\'\s]*', text)
            if match:
                return match.group(0)
        return None

    async def _solve_turnstile(self, url: str):
        """Return ``(html, cookie_header, user_agent)`` for a solved page."""
        proxy = (
            None
            if (self._force_direct or self.bypass_warp_active) and not self._session_proxy
            else get_solver_proxy_url(self._session_proxy)
        )
        allow_direct = self._force_direct or is_direct_connection_allowed(self.bypass_warp_active)
        html = ""
        cookie_header = ""
        user_agent = ""
        for _ in range(2):
            try:
                solution = await solve_cloudflare(
                    url,
                    proxy_url=proxy,
                    allow_direct=allow_direct,
                    tabs_till_verify=self.TURNSTILE_TABS,
                )
            except FlareSolverrError as exc:
                raise ExtractorError(
                    f"Dropload extraction failed: FlareSolverr unavailable: {exc}"
                ) from exc
            html = solution.response or ""
            cookie_header = solution.cookie_header
            user_agent = solution.user_agent
            if not _TURNSTILE_GATE_RE.search(html):
                break
            logger.warning("Dropload: Turnstile still present in solver response, retrying")
        return html, cookie_header, user_agent

    async def _fetch_player_page(
        self, url: str, code: str, cookie_header: str, user_agent: str
    ) -> str:
        """Re-submit the embed form over HTTP for an unconsumed stream token."""
        parsed = urlparse(url)
        headers = {
            "User-Agent": user_agent or self.base_headers["User-Agent"],
            "Referer": url,
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        resp = await self._make_request(
            url,
            method="POST",
            headers=headers,
            data={"op": "embed", "file_code": code},
        )
        return resp.text or ""

    async def extract(self, url: str, **kwargs) -> dict:
        """Extract Dropload URL."""
        self._apply_routing_kwargs(url, kwargs)

        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Referer": referer,
            "User-Agent": self.base_headers["User-Agent"],
        }

        session = await self._get_session(url)
        resp = await self._make_request(url, headers=headers)
        html = resp.text or ""
        extra_headers = {}
        gate_stuck = False

        # Some pages the gate still references already carry the stream (trusted
        # IP, cached variant): take it and skip the solver round-trip. A real
        # gate page has no media here, so the solver stays mandatory below.
        final_url = self._media_from_html(html)

        if not final_url and _TURNSTILE_GATE_RE.search(html):
            logger.info("Dropload: Turnstile gate detected, trying FlareSolverr")
            solved_html, cookie_header, user_agent = await self._solve_turnstile(url)
            if user_agent:
                extra_headers["User-Agent"] = user_agent
            if cookie_header:
                extra_headers["Cookie"] = cookie_header

            html = solved_html
            # A solved page may still reference the widget script while already
            # handing over the player, so the marker alone is not a failure.
            gate_stuck = bool(_TURNSTILE_GATE_RE.search(solved_html))

            code_match = _EMBED_CODE_RE.search(parsed.path or "")
            if code_match:
                try:
                    fresh_html = await self._fetch_player_page(
                        url, code_match.group(1), cookie_header, user_agent
                    )
                    if "m3u8" in fresh_html or "mp4" in fresh_html:
                        # Fresh token: the browser already consumed the one in
                        # the solver page.
                        html = fresh_html
                        gate_stuck = False
                except ExtractorError as exc:
                    logger.warning("Dropload: fresh player page failed, using solver page: %s", exc)

            final_url = self._media_from_html(html)

        if not final_url:
            try:
                final_url = await eval_solver(
                    session, resp.url, headers, _MEDIA_PATTERNS, text=html
                )
            except Exception:
                final_url = None

        if not final_url:
            if gate_stuck:
                raise ExtractorError(
                    "Dropload extraction failed: Cloudflare Turnstile challenge "
                    "not solved by FlareSolverr"
                )
            raise ExtractorError("Dropload extraction failed: no media URL found")

        req_headers = {
            **self.base_headers,
            **extra_headers,
            "referer": url,
            "origin": referer.rstrip("/"),
        }
        mediaflow_endpoint = "proxy_stream_endpoint" if ".mp4" in final_url else self.mediaflow_endpoint

        return self._result(urljoin(url, final_url), req_headers, mediaflow_endpoint=mediaflow_endpoint)

