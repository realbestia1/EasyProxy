"""ScoreZone extractor (partite live + canali TV 24/7).

Porting della logica di ScoreZoneProvider / scoreZoneStream (live-sport-plugin):

  match : GET  {API}/api/matches/channels  -> {key, channels:[{id,name,quality,locked}]}
          POST {API}/api/matches/playback  {matchKey, channelId} -> {url}   (m3u8 firmato, token a scadenza breve)
  iptv  : GET  {IPTV}/api/iptv/channels    -> {channels:[{id,name,url,country,expiresAt}]}

Il token viene richiesto dall'IP di uscita di EasyProxy al momento della riproduzione (e di nuovo
a ogni re-extract su 401/403), quindi non scade mai dentro una playlist.

Target `d` accettati (tutti gli esempi sono equivalenti a livello di contratto):
  https://scorezonetv.live/match/<eventId>?p=<base64url(JSON)>        <- payload compresso/serializzato
  https://scorezonetv.live/match/<eventId>?home=..&away=..&sport=..&start=..&competition=..&channel=..
  https://scorezonetv.live/iptv/<channelId>
  scorezone://<eventId>?...   |   scorezone://iptv/<channelId>
oppure gli stessi campi come parametri query dell'endpoint (eventId, home, away, sport, start,
competition, channel / iptv).
"""

import base64
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any, Dict, Optional

import aiohttp

from extractors.base import BaseExtractor, ExtractorError

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://testscore.pxxl.pro"
DEFAULT_SITE_URL = "https://scorezonetv.live"
DEFAULT_IPTV_URL = "https://scorezone-live.scorezone.workers.dev/api/iptv/channels"

CACHE_TTL = 60  # secondi: elenco canali partita / lista IPTV
CACHE_MAX_ENTRIES = 500
PLAYBACK_CACHE_TTL = 45  # 45 secondi di cache per il token playback (evita overload API e previene token scaduti)
_PLAYBACK_CACHE: Dict[tuple, tuple] = {}  # (eventId, channelId) -> (ts, stream_url)
_CHANNELS_CACHE: Dict[str, tuple] = {}    # eventId -> (ts, key, channels)
_IPTV_CACHE: Optional[tuple] = None       # (ts, channels)


def _api_base() -> str:
    return os.getenv("SCOREZONE_API_BASE", DEFAULT_API_BASE).strip().rstrip("/")


def _site_url() -> str:
    return os.getenv("SCOREZONE_SITE_URL", DEFAULT_SITE_URL).strip().rstrip("/")


def _iptv_url() -> str:
    return os.getenv("SCOREZONE_IPTV_URL", DEFAULT_IPTV_URL).strip()


def _b64url_json(value: str) -> dict:
    """Decodifica il parametro `p` (JSON in base64url); {} se non valido."""
    try:
        raw = value.replace("-", "+").replace("_", "/")
        raw += "=" * (-len(raw) % 4)
        data = json.loads(base64.b64decode(raw).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first(qs: dict, *names) -> str:
    for name in names:
        vals = qs.get(name)
        if vals and str(vals[0]).strip():
            return str(vals[0]).strip()
    return ""


def parse_scorezone_target(url: str, **kwargs) -> dict:
    """Ritorna {"kind": "match"|"iptv", ...}. Solleva ExtractorError se mancano i dati minimi."""
    raw = str(url or "").strip()
    raw = raw.split("#", 1)[0]
    if "%3f" in raw.lower() or "%26" in raw.lower():
        raw = urllib.parse.unquote(raw)

    scheme_iptv = re.match(r"^scorezone://iptv/([^/?#]+)", raw, re.IGNORECASE)
    path_iptv = re.search(r"/iptv/([^/?#]+)", raw) if "scorezone" in raw.lower() else None
    path_match = re.search(r"/match/([^/?#]+)", raw)
    scheme_match = re.match(r"^scorezone://(?!iptv/)([^/?#]+)", raw, re.IGNORECASE)

    parsed = urllib.parse.urlsplit(raw)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

    def _kw(*names) -> str:
        for name in names:
            v = kwargs.get(name)
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    # --- canale IPTV 24/7
    iptv_id = (scheme_iptv.group(1) if scheme_iptv else None) or (path_iptv.group(1) if path_iptv else None) \
        or _kw("iptv", "iptv_id", "iptvId")
    if iptv_id and not (path_match or scheme_match):
        return {"kind": "iptv", "channel": urllib.parse.unquote(iptv_id)}

    # --- partita
    meta: Dict[str, Any] = {}
    packed = _first(qs, "p") or _kw("p")
    if packed:
        meta = _b64url_json(packed)

    def _field(meta_key: str, *names) -> str:
        v = meta.get(meta_key)
        if v is not None and str(v).strip():
            return str(v).strip()
        return _first(qs, *names) or _kw(*names)

    event_id = urllib.parse.unquote(
        (path_match.group(1) if path_match else None)
        or (scheme_match.group(1) if scheme_match else None)
        or _kw("eventId", "event_id", "eid")
        or _first(qs, "eventId", "event_id", "eid")
        or ""
    )
    if not event_id:
        raise ExtractorError(
            f"SCOREZONE: eventId mancante nel target '{raw[:120]}' "
            f"(atteso {_site_url()}/match/<eventId>?p=... oppure /iptv/<channelId>)"
        )

    return {
        "kind": "match",
        "eventId": event_id,
        "home": _field("home", "home"),
        "away": _field("away", "away"),
        "sport": _field("sport", "sport") or "football",
        "start": _field("start", "start"),
        "competition": _field("competition", "competition"),
        "channel": _field("channel", "channel", "channelId", "channel_id", "ch"),
        "key": _field("key", "key", "matchKey", "match_key"),
    }


def _quality_rank(q: str) -> int:
    q = (q or "").lower()
    if "1080" in q or "fhd" in q:
        return 0
    if "720" in q or q.strip() == "hd":
        return 1
    if "480" in q or "sd" in q:
        return 2
    return 3


def _is_channel_locked(c: dict) -> bool:
    """True se la fonte ha il lucchetto (locked / protetta)."""
    for k in ("locked", "is_locked", "lock", "isLocked"):
        v = c.get(k)
        if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
            return True
        if bool(v):
            return True
    return False


class ScoreZoneExtractor(BaseExtractor):
    """Risolve un evento/canale ScoreZone in una playlist HLS riproducibile via hls_proxy."""

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="scorezone", bypass_warp=bypass_warp)
        self.mediaflow_endpoint = "hls_proxy"

    # ------------------------------------------------------------------ helpers
    def _api_headers(self) -> dict:
        site = _site_url()
        return {
            "User-Agent": self.base_headers["User-Agent"],
            "Origin": site,
            "Referer": f"{site}/",
            "Accept": "application/json, text/plain, */*",
        }

    def _stream_headers(self) -> dict:
        site = _site_url()
        return {
            "User-Agent": self.base_headers["User-Agent"],
            "Referer": f"{site}/",
            "Origin": site,
        }

    @staticmethod
    def _cache_put(cache: dict, key: str, value: tuple) -> None:
        if len(cache) >= CACHE_MAX_ENTRIES and key not in cache:
            cache.pop(next(iter(cache)))
        cache[key] = value

    async def _match_channels(self, meta: dict, url: str, force: bool = False):
        eid = str(meta["eventId"])
        cached = _CHANNELS_CACHE.get(eid)
        if cached and not force and time.time() - cached[0] < CACHE_TTL:
            return cached[1], cached[2]

        params = {"home": meta["home"], "away": meta["away"], "sport": meta["sport"], "eventId": eid}
        if meta.get("start"):
            params["start"] = meta["start"]
        if meta.get("competition"):
            params["competition"] = meta["competition"]

        resp = await self._make_request(
            f"{_api_base()}/api/matches/channels",
            headers=self._api_headers(),
            params=params,
            timeout=aiohttp.ClientTimeout(total=25, connect=10, sock_read=20),
        )
        data = resp.json
        channels = data.get("channels") if isinstance(data, dict) else None
        key = data.get("key") if isinstance(data, dict) else None
        if not key or not isinstance(channels, list):
            raise ExtractorError(f"SCOREZONE: risposta canali non valida per evento {eid}")
        self._cache_put(_CHANNELS_CACHE, eid, (time.time(), key, channels))
        return key, channels

    async def _extract_match(self, meta: dict, url: str, force_refresh: bool = False) -> Dict[str, Any]:
        eid = str(meta["eventId"])
        wanted = meta.get("channel")
        now = time.time()

        if force_refresh:
            _CHANNELS_CACHE.pop(eid, None)
            keys_to_del = [k for k in _PLAYBACK_CACHE if k[0] == eid]
            for k in keys_to_del:
                _PLAYBACK_CACHE.pop(k, None)
        else:
            if wanted:
                cached_pb = _PLAYBACK_CACHE.get((eid, str(wanted)))
                if cached_pb and (now - cached_pb[0]) < PLAYBACK_CACHE_TTL:
                    logger.info("ScoreZone: playback da cache per evento %s canale %s", eid, wanted)
                    return self._result(cached_pb[1], self._stream_headers())
            else:
                for (cached_eid, ch_id), (ts, stream_url) in list(_PLAYBACK_CACHE.items()):
                    if cached_eid == eid and (now - ts) < PLAYBACK_CACHE_TTL:
                        logger.info("ScoreZone: playback da cache per evento %s canale %s", eid, ch_id)
                        return self._result(stream_url, self._stream_headers())

        key = meta.get("key")
        channel_id = str(wanted) if wanted else None

        # Se key o channel_id non sono già noti (es. link manuale), interroga l'elenco canali
        if not key or not channel_id:
            fetched_key, channels = await self._match_channels(meta, url, force=force_refresh)
            if not key:
                key = fetched_key
            if not channel_id:
                free = [c for c in channels if isinstance(c, dict) and c.get("id") and not _is_channel_locked(c)]
                if not free:
                    raise ExtractorError(f"SCOREZONE: nessun canale sbloccato per evento {eid}")
                chosen = sorted(free, key=lambda c: _quality_rank(c.get("quality")))[0]
                channel_id = str(chosen["id"])

        cache_key = (eid, channel_id)

        try:
            resp = await self._make_request(
                f"{_api_base()}/api/matches/playback",
                method="POST",
                headers={**self._api_headers(), "Content-Type": "application/json"},
                json={"matchKey": key, "channelId": channel_id},
                timeout=aiohttp.ClientTimeout(total=15, connect=5, sock_read=12),
            )
            data = resp.json
            stream_url = data.get("url") if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("ScoreZone: errore minting playback iniziale per evento %s: %s", eid, e)
            stream_url = None

        if not stream_url:
            # Se la key passata nel payload era scaduta o non valida, riprova rigenerando matchKey fresca
            logger.info("ScoreZone: rigenero canali/key freschi per evento %s", eid)
            key, channels = await self._match_channels(meta, url, force=True)
            if wanted:
                free = [c for c in channels if isinstance(c, dict) and c.get("id") and not _is_channel_locked(c)]
                matched = next((c for c in free if str(c["id"]) == wanted), None)
                if matched:
                    channel_id = str(matched["id"])
                elif free:
                    channel_id = str(sorted(free, key=lambda c: _quality_rank(c.get("quality")))[0]["id"])
            resp = await self._make_request(
                f"{_api_base()}/api/matches/playback",
                method="POST",
                headers={**self._api_headers(), "Content-Type": "application/json"},
                json={"matchKey": key, "channelId": channel_id},
                timeout=aiohttp.ClientTimeout(total=15, connect=5, sock_read=12),
            )
            data = resp.json
            stream_url = data.get("url") if isinstance(data, dict) else None

        if not stream_url:
            _CHANNELS_CACHE.pop(eid, None)
            _PLAYBACK_CACHE.pop(cache_key, None)
            raise ExtractorError(f"SCOREZONE: playback senza URL (evento {eid}, canale {channel_id})")

        _PLAYBACK_CACHE[cache_key] = (now, stream_url)
        logger.info("ScoreZone: evento %s canale %s -> %s...", eid, channel_id, stream_url[:70])
        return self._result(stream_url, self._stream_headers())

    async def _extract_iptv(self, channel_id: str, force_refresh: bool = False) -> Dict[str, Any]:
        global _IPTV_CACHE
        cached = _IPTV_CACHE
        channels = cached[1] if cached and not force_refresh and time.time() - cached[0] < CACHE_TTL else None
        channel = next((c for c in (channels or []) if str(c.get("id")) == channel_id), None)
        if channel is None:
            resp = await self._make_request(
                _iptv_url(),
                headers=self._api_headers(),
                timeout=aiohttp.ClientTimeout(total=8, connect=4, sock_read=6),
            )
            data = resp.json
            channels = data.get("channels") if isinstance(data, dict) else None
            if not isinstance(channels, list):
                raise ExtractorError("SCOREZONE: lista canali IPTV non valida")
            _IPTV_CACHE = (time.time(), channels)
            channel = next((c for c in channels if isinstance(c, dict) and str(c.get("id")) == channel_id), None)
        if not channel or not channel.get("url"):
            raise ExtractorError(f"SCOREZONE: canale IPTV {channel_id} non trovato")

        logger.info("ScoreZone IPTV: %s (%s)", channel_id, channel.get("name"))
        return self._result(str(channel["url"]), self._stream_headers())

    # ------------------------------------------------------------------ entry point
    async def extract(self, url: str, **kwargs) -> Dict[str, Any]:
        # MANDATORY (AGENTS.md): merge del routing per-richiesta (proxy / warp / direct)
        self._apply_routing_kwargs(url, kwargs)

        force_refresh = bool(kwargs.get("force_refresh")) or str(kwargs.get("force", "")).lower() == "true"
        target = parse_scorezone_target(url, **kwargs)
        try:
            if target["kind"] == "iptv":
                return await self._extract_iptv(target["channel"], force_refresh=force_refresh)
            return await self._extract_match(target, url, force_refresh=force_refresh)
        except ExtractorError:
            raise
        except Exception as e:
            logger.error("ScoreZoneExtractor error: %s", e, exc_info=True)
            raise ExtractorError(f"SCOREZONE extraction failed: {e}")
