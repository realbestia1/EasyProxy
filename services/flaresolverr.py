"""On-demand FlareSolverr runner used by the VixSrc extractor.



The process is deliberately not started with EasyProxy.  It is spawned only

when a Cloudflare challenge is detected and is terminated after the API call

has returned the solved page/cookies.

"""



from __future__ import annotations



import asyncio

import json

import logging

import os

import shlex

import shutil
import subprocess

import signal

import sys

from collections import deque

from dataclasses import dataclass

from pathlib import Path

from urllib.parse import urlparse, urlsplit



import aiohttp

import psutil





logger = logging.getLogger(__name__)





_FLARESOLVERR_TURNSTILE_IMPORT = (

    "from selenium.common import TimeoutException",

    "from selenium.common import StaleElementReferenceException, TimeoutException",

)

_FLARESOLVERR_TURNSTILE_READ = (

    '        turnstile_token = token_input.get_attribute("value")\n'

    "        if turnstile_token:",

    "        try:\n"

    '            turnstile_token = token_input.get_attribute("value")\n'

    "        except StaleElementReferenceException:\n"

    "            # The widget solved and the page callback already submitted\n"

    "            # the form: the element is gone, the new page is the result.\n"

    "            return None\n"

    "        if turnstile_token:",

)





def patch_turnstile_navigation(script_path: str) -> None:

    """Tolerate Turnstile callbacks that navigate away mid-solve.



    FlareSolverr 3.5.x reads the Turnstile token after clicking the widget; on

    pages whose callback immediately submits a form that read raises

    StaleElementReferenceException and the whole solve fails even though the

    challenge was solved.  Patch the vendored source in place, idempotently.

    """

    path = Path(script_path).with_name("flaresolverr_service.py")

    try:

        source = path.read_text(encoding="utf-8")

    except OSError:

        return

    if "except StaleElementReferenceException" in source:

        return

    replacements = (_FLARESOLVERR_TURNSTILE_IMPORT, _FLARESOLVERR_TURNSTILE_READ)

    if any(old not in source for old, _ in replacements):

        logger.warning(

            "FlareSolverr source layout changed: Turnstile navigation patch skipped"

        )

        return

    for old, new in replacements:

        source = source.replace(old, new)

    try:

        path.write_text(source, encoding="utf-8")

        logger.info("Patched FlareSolverr Turnstile handler for callback navigation")

    except OSError as exc:

        logger.warning("Unable to patch FlareSolverr source %s: %s", path, exc)





class FlareSolverrError(RuntimeError):

    """Raised when the local FlareSolverr process/API cannot solve a request."""





@dataclass(frozen=True)

class FlareSolverrSolution:

    response: str

    status: int

    url: str

    cookies: tuple[dict, ...]

    user_agent: str



    @property

    def cookie_header(self) -> str:

        return "; ".join(

            f"{cookie.get('name')}={cookie.get('value', '')}"

            for cookie in self.cookies

            if cookie.get("name")

        )





def cookie_header_to_list(cookie_header: str | None) -> list[dict]:

    """Convert a normal Cookie header into FlareSolverr's cookie format."""

    cookies = []

    for item in (cookie_header or "").split(";"):

        name, separator, value = item.strip().partition("=")

        if name and separator:

            cookies.append({"name": name.strip(), "value": value.strip()})

    return cookies





def proxy_payload(proxy_url: str) -> dict:

    """Return FlareSolverr's proxy object without leaking credentials in URL logs."""

    parsed = urlsplit(proxy_url)

    payload = {"url": proxy_url}

    if parsed.username and parsed.password and parsed.hostname and parsed.port:

        payload["url"] = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"

        payload["username"] = parsed.username

        payload["password"] = parsed.password

    return payload





class FlareSolverrManager:

    def __init__(self) -> None:

        self._lock = asyncio.Lock()

        self._process: asyncio.subprocess.Process | None = None

        self._owns_process = False

        self._session: aiohttp.ClientSession | None = None

        self._process_output: deque[str] = deque(maxlen=80)

        self._output_task: asyncio.Task | None = None



    @property

    def api_url(self) -> str:

        configured = os.getenv("FLARESOLVERR_API_URL", "http://127.0.0.1:8191").rstrip("/")

        return configured if configured.endswith("/v1") else configured + "/v1"



    @staticmethod

    def _int_env(name: str, default: int, minimum: int = 1) -> int:

        try:

            return max(minimum, int(os.getenv(name, str(default))))

        except (TypeError, ValueError):

            return default



    def _command(self) -> list[str] | None:

        configured = os.getenv("FLARESOLVERR_COMMAND", "").strip()

        if configured:

            return shlex.split(configured, posix=os.name != "nt")



        # An explicitly configured source tree wins over a bundled binary.

        default_dir = "/opt/flaresolverr" if os.name != "nt" else ""

        root_dir = os.getenv("FLARESOLVERR_DIR", default_dir)

        if root_dir and os.path.isdir(root_dir):

            source_script = Path(root_dir) / "src" / "flaresolverr.py"

            if source_script.is_file():

                return [sys.executable, str(source_script)]



        # Check local flaresolverr binary next to executable, MEIPASS, or project

        bases = []

        if getattr(sys, "frozen", False):

            if hasattr(sys, "_MEIPASS"):

                bases.append(Path(sys._MEIPASS))

            bases.append(Path(sys.executable).parent)

            bases.append(Path(sys.executable).parent.parent)

        else:

            bases.append(Path(__file__).resolve().parent.parent)



        exe_name = "flaresolverr.exe" if os.name == "nt" else "flaresolverr"

        for base in bases:

            for candidate in (

                base / "flaresolverr" / exe_name,

                base / exe_name,

                base / "bin" / exe_name,

            ):

                if candidate.is_file():

                    return [str(candidate)]



        binary = shutil.which("flaresolverr")

        return [binary] if binary else None



    async def _api_available(self) -> bool:

        try:

            # Health checks must not install their 2s timeout on the session

            # used later for the actual browser-solving request.

            timeout = aiohttp.ClientTimeout(total=2)

            async with aiohttp.ClientSession(

                timeout=timeout,

                headers={"Content-Type": "application/json"},

            ) as session:

                async with session.get(self.api_url, allow_redirects=False) as response:

                    # GET /v1 commonly returns 405; that still proves the API is up.

                    return response.status < 500

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):

            return False



    async def _capture_process_output(self, stream) -> None:

        if stream is None:

            return

        try:

            async for raw_line in stream:

                line = raw_line.decode("utf-8", errors="replace").strip()

                if line:

                    self._process_output.append(line)

        except (asyncio.CancelledError, OSError):

            return



    async def _finish_process_output(self) -> None:

        if self._output_task is None:

            return

        task = self._output_task

        self._output_task = None

        if not task.done():

            task.cancel()

        try:

            await task

        except (asyncio.CancelledError, OSError, Exception):

            pass



    def _process_diagnostic(self) -> str:

        if not self._process_output:

            return ""

        return " | ".join(self._process_output)[-4000:]



    async def _start(self) -> None:

        if self._process is not None and self._process.returncode is None:

            return



        # An explicitly configured API may already be managed outside this

        # process (e.g. a container publishing the API port).  Use it as-is.

        if await self._api_available():

            return



        command = self._command()

        if not command:

            raise FlareSolverrError(

                "FlareSolverr non installato: configura FLARESOLVERR_COMMAND "

                "oppure ricostruisci l'immagine EasyProxy."

            )

        if command[-1].endswith("flaresolverr.py"):

            patch_turnstile_navigation(command[-1])



        parsed_api = urlparse(self.api_url)

        api_host = parsed_api.hostname or "127.0.0.1"

        api_port = parsed_api.port or 8191

        env = os.environ.copy()

        env.update(

            {

                "HOST": "127.0.0.1",

                "PORT": str(api_port),

                "HEADLESS": "true",

                "LOG_LEVEL": os.getenv("FLARESOLVERR_LOG_LEVEL", "error"),

                "LOG_HTML": "false",

                "DISABLE_MEDIA": "true",

                "PROMETHEUS_ENABLED": "false",

            }

        )



        # The default local API is spawned below when needed.

        if api_host not in {"127.0.0.1", "localhost", "::1"}:

            raise FlareSolverrError(f"FlareSolverr API non raggiungibile: {self.api_url}")



        try:

            self._process_output.clear()

            working_dir = os.getenv("FLARESOLVERR_DIR")

            if not working_dir or not os.path.isdir(working_dir):

                target = command[1] if len(command) > 1 and command[0].endswith(("python", "python.exe")) else command[0]

                working_dir = str(Path(target).resolve().parent)

            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

            self._process = await asyncio.create_subprocess_exec(

                *command,

                cwd=working_dir,

                env=env,

                stdin=asyncio.subprocess.DEVNULL,

                stdout=asyncio.subprocess.PIPE,

                stderr=asyncio.subprocess.STDOUT,

                start_new_session=os.name != "nt",

                creationflags=creationflags,

            )

            self._owns_process = True

            self._output_task = asyncio.create_task(

                self._capture_process_output(self._process.stdout)

            )

        except (OSError, ValueError) as exc:

            raise FlareSolverrError(f"Avvio FlareSolverr fallito: {exc}") from exc



        deadline = asyncio.get_running_loop().time() + self._int_env(

            "FLARESOLVERR_START_TIMEOUT", 30

        )

        while asyncio.get_running_loop().time() < deadline:

            if self._process.returncode is not None:

                await self._finish_process_output()

                diagnostic = self._process_diagnostic()

                suffix = f": {diagnostic}" if diagnostic else ""

                raise FlareSolverrError(

                    f"FlareSolverr terminato subito (exit={self._process.returncode}){suffix}"

                )

            if await self._api_available():

                logger.info("FlareSolverr avviato on-demand su 127.0.0.1:%s", api_port)

                return

            await asyncio.sleep(0.25)



        raise FlareSolverrError("Timeout avvio API FlareSolverr")



    async def _stop(self) -> None:

        process = self._process

        self._process = None

        owns_process = self._owns_process

        self._owns_process = False



        session = self._session

        self._session = None

        if session is not None and not session.closed:

            try:

                await session.close()

            except BaseException:

                pass



        # Collect descendant processes (Xvfb, chromedriver, chromium) BEFORE

        # terminating the parent process, otherwise they get reparented to PID 1.

        tracked_procs: dict[int, psutil.Process] = {}

        if process and process.pid:

            try:

                parent_ps = psutil.Process(process.pid)

                for child in parent_ps.children(recursive=True):

                    tracked_procs[child.pid] = child

                tracked_procs[parent_ps.pid] = parent_ps

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):

                pass



        if process and owns_process and process.returncode is None:

            try:

                if os.name != "nt":

                    os.killpg(process.pid, signal.SIGTERM)

                else:

                    process.terminate()

                await asyncio.wait_for(process.wait(), timeout=3)

            except BaseException:

                if process.returncode is None:

                    try:

                        if os.name != "nt":

                            os.killpg(process.pid, signal.SIGKILL)

                        else:

                            process.kill()

                        await asyncio.wait_for(process.wait(), timeout=2)

                    except BaseException:

                        pass



        # FlareSolverr can leave Chromium, chromedriver, or Xvfb outside its

        # process group (or reparented after a cancelled solve).

        for candidate in psutil.process_iter(["pid", "name", "cmdline"]):

            try:

                pid = candidate.info.get("pid")

                if not pid or pid == os.getpid() or pid in tracked_procs:

                    continue

                command = " ".join(candidate.info.get("cmdline") or [])

                lowered = command.lower()

                name = (candidate.info.get("name") or "").lower()

                is_flaresolverr = "flaresolverr.py" in lowered or "flaresolverr" in name

                is_xvfb = name == "xvfb" or "/xvfb" in lowered or lowered.startswith("xvfb ")

                is_chromedriver = "chromedriver" in name

                is_temp_browser = (

                    "--user-data-dir=/tmp/" in lowered

                    and ("chromium" in name or "chrome" in name or "chromedriver" in name)

                )

                if is_flaresolverr or is_xvfb or is_chromedriver or is_temp_browser:

                    tracked_procs[pid] = candidate

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):

                continue



        browser_processes = list(tracked_procs.values())

        for browser_process in browser_processes:

            try:

                browser_process.terminate()

            except (psutil.NoSuchProcess, psutil.AccessDenied):

                pass

        if browser_processes:

            _, alive = psutil.wait_procs(browser_processes, timeout=2)

            for browser_process in alive:

                try:

                    browser_process.kill()

                except (psutil.NoSuchProcess, psutil.AccessDenied):

                    pass

            psutil.wait_procs(alive, timeout=2)



        await self._finish_process_output()



        if process and owns_process:

            logger.info("FlareSolverr terminato dopo il recupero dei cookie")



    @staticmethod

    def _has_ram_headroom() -> bool:

        """Refuse to spawn Chromium when the box is almost out of RAM."""

        try:

            import psutil

            available_mb = psutil.virtual_memory().available / (1024 ** 2)

        except Exception:

            return True

        minimum = float(os.getenv("FLARESOLVERR_MIN_AVAILABLE_MB", "400"))

        if available_mb < minimum:

            logger.warning(

                "FlareSolverr: only %.0f MB RAM available (need %.0f MB), skipping solver",

                available_mb,

                minimum,

            )

            return False

        return True



    async def solve(

        self,

        url: str,

        proxy_url: str | None,

        cookie_header: str | None = None,

        allow_direct: bool = False,

        tabs_till_verify: int | None = None,

    ) -> FlareSolverrSolution:

        """Solve one challenge and stop the owned process before returning."""

        if not proxy_url and not allow_direct:

            raise FlareSolverrError(

                "Route FlareSolverr assente: direct fallback disabilitato mentre WARP è attivo"

            )

        if not self._has_ram_headroom():

            raise FlareSolverrError("low RAM: FlareSolverr skipped")



        async with self._lock:

            try:

                await self._start()

                if self._session is None or self._session.closed:

                    self._session = aiohttp.ClientSession(

                        timeout=aiohttp.ClientTimeout(

                            total=self._int_env("FLARESOLVERR_REQUEST_TIMEOUT", 90)

                        ),

                        headers={"Content-Type": "application/json"},

                    )



                payload = {

                    "cmd": "request.get",

                    "url": url,

                    "maxTimeout": self._int_env("FLARESOLVERR_MAX_TIMEOUT_MS", 60000),

                    "returnOnlyCookies": False,

                    "disableMedia": True,

                }

                if tabs_till_verify is not None:

                    payload["tabs_till_verify"] = int(tabs_till_verify)

                cookies = cookie_header_to_list(cookie_header)

                if cookies:

                    payload["cookies"] = cookies

                if proxy_url:

                    payload["proxy"] = proxy_payload(proxy_url)



                async with self._session.post(self.api_url, json=payload) as response:

                    if response.status >= 400:

                        try:

                            error = await response.json(content_type=None)

                            message = str(error.get("message") or "risposta senza dettagli")[:1500]

                        except (ValueError, AttributeError):

                            message = "risposta non JSON"

                        raise FlareSolverrError(f"FlareSolverr HTTP {response.status}: {message}")

                    response.raise_for_status()

                    result = await response.json(content_type=None)



                if result.get("status") != "ok":

                    message = result.get("message") or "risposta non valida"

                    raise FlareSolverrError(f"FlareSolverr: {message}")



                raw_solution = result.get("solution") or {}

                raw_cookies = raw_solution.get("cookies") or []

                solution = FlareSolverrSolution(

                    response=str(raw_solution.get("response") or ""),

                    status=int(raw_solution.get("status") or 200),

                    url=str(raw_solution.get("url") or url),

                    cookies=tuple(cookie for cookie in raw_cookies if isinstance(cookie, dict)),

                    user_agent=str(raw_solution.get("userAgent") or ""),

                )

                if not solution.response and not solution.cookies:

                    raise FlareSolverrError("FlareSolverr non ha restituito HTML o cookie")

                return solution

            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, json.JSONDecodeError) as exc:

                detail = str(exc) or type(exc).__name__

                raise FlareSolverrError(f"Richiesta FlareSolverr fallita [{detail}]") from exc

            finally:

                # Deliberately stop immediately after the solution is copied.

                # Shield cleanup so an outer timeout/cancellation cannot abort

                # _stop() halfway and leave FlareSolverr/Xvfb/chromedriver alive.

                stop_task = asyncio.create_task(self._stop())

                try:

                    await asyncio.shield(stop_task)

                except BaseException:

                    await asyncio.gather(stop_task, return_exceptions=True)

                    raise



    async def shutdown(self) -> None:

        async with self._lock:

            await self._stop()





_MANAGER = FlareSolverrManager()





async def solve_cloudflare(

    url: str,

    proxy_url: str | None,

    cookie_header: str | None = None,

    allow_direct: bool = False,

    tabs_till_verify: int | None = None,

) -> FlareSolverrSolution:

    return await _MANAGER.solve(

        url, proxy_url, cookie_header, allow_direct, tabs_till_verify

    )





async def shutdown_flare_solver() -> None:

    await _MANAGER.shutdown()

