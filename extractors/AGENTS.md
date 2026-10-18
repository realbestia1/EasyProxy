# AGENTS.md — New extractor (mandatory contract)

References (standard contract, nothing else needed):
- Direct aiohttp → `fctv33.py`.
- Node runner (wasm/obfs) → `cinejoy.py`.

## 1. Extractor file — `extractors/<name>.py`

```python
from config import get_connector_for_proxy
import config as _cfg
from extractors.base import BaseExtractor, ExtractorError
from aiohttp import ClientSession, ClientTimeout, TCPConnector

class <Name>Extractor(BaseExtractor):
    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers or {}, proxies=proxies, extractor_name="<name>")
        self.mediaflow_endpoint = "hls_proxy"  # hls_proxy | hls_manifest_proxy | proxy_stream_endpoint (.mp4) | mpd_manifest_proxy

    # BaseExtractor._get_session already implements the routing-aware session
    # (forced proxy, proxy_exclude_domains, WARP/direct fallback). Override it
    # ONLY for custom connectors/SSL, and build the proxy via self._resolve_proxy(url).

    async def extract(self, url: str, **kwargs) -> dict:
        # MANDATORY at the top of extract: canonical routing merge
        # (proxy_streaming re-extract passes proxy/warp/direct as kwargs, not via context).
        # Sets _forced_proxy/_force_direct/bypass_warp_active; proxy_exclude_domains
        # drops even an explicit ?proxy= (WARP exempt).
        self._apply_routing_kwargs(url, kwargs)

        session = await self._get_session(url)
        # ... your logic ...
        return {
            "destination_url": signed_url,
            "request_headers": {"User-Agent": ..., "Referer": ..., "Origin": ...},
            "mediaflow_endpoint": self.mediaflow_endpoint,
            **self._routing_result_fields(),  # MANDATORY: selected_proxy/force_direct/bypass_warp
        }
```

Notes:
- Node runner (wasm/obfs) → copy `cinejoy.py`: `get_http_bridge_for_proxy(proxy)` + `env["<NAME>_PROXY"]`, use `self._apply_routing_kwargs(url, kwargs)` then `proxy = await self._resolve_proxy(url)`, still return the 3 fields above.
- Stdlib/aiohttp for HTTP only. No new dependencies.
- Optional, only if needed: `force_ipv4 = True` (IPv4-bound tokens / CDN behind CF, see `vavoo.py`), `REQUEST_TIMEOUT_TOTAL = 60+` (slow solvers; default 30 in `proxy_extractor.py`), `disable_ssl` / `captured_manifest(s)` in the return dict.

## 2. `extractors/registry_imports.py` — one table row

```python
("extractors.<name>", ("<Name>Extractor",)),
```

Add it to `_MODULES` (load order) — `__all__` is derived from the table. A module
that fails to import degrades to `None` (the resolver then reports it as
unavailable) instead of killing startup.

Without this, `available_extractors` (admin warp_off/proxy_off/max_res toggles) won't see it. The admin toggles are then automatic via `extractor_name` — no further changes needed.

## 3. `extractors/registry_resolver.py` — one `_Entry`

```python
_Entry("<name>", <Name>Extractor, hosts=("<name>", "<alias>"),
       match=_sub("<domain>"), url_proxy="<domain>"),
```

- `hosts` → `?host=` aliases (lower-case, exact match).
- `match` → URL predicate for auto-detection (`_sub`, `_rex`, `_any_of`,
  `_all_of`, `_host_endswith`, `_path_startswith`, or a lambda).
- `url_proxy` → proxy lookup target when auto-detected: `"url"`, a literal host,
  or a callable `(url, bypass_warp)`; add `url_proxy_final=True` when the
  callable already returns the proxy (`_resolve_sportsonline_proxy`).
- `host_proxy` → override for the `?host=` lookup target (default: the alias).
- `key_name` → cache key when it differs from the alias (canonical admin name).
- `proxy_extractor_name="<name>"` → forwarded to `get_proxy_for_url`.
- `refresh_headers=True` → re-bind request headers on every hit.

FORBIDDEN: `get_proxy_for_url("<name>")` with a literal — it breaks `TRANSPORT_ROUTES` (substring match on the real URL), `warp/proxy_exclude_domains` (hostname parse → `None`) and `extractor_proxies`. Always use `url` + `extractor_name="<name>"`. Extractor `_Entry` rows are constructed with `bypass_warp=` passed through by the resolver.

## 4. `services/proxy_extractor.py` — help JSON

Add `"<name>"` to `available_hosts` + an example `.../extractor/video?host=<name>&d=...`.

## 5. UI — TWO static templates (not auto-generated)

- `templates/url_generator.html` → `extractorHostSelect`: `<option value="<name>">...</option>` (the DUAL selects clone from it via JS, once is enough).
- `templates/recordings.html` → `datalist`: `<option value="<name>"></option>`.

## 6. `extractors/extractor_test_urls.json`

`"<name>": "https://..."` — feeds the extractor test API/UI.

## 7. Verify

```powershell
python -m py_compile extractors/<name>.py extractors/registry_resolver.py extractors/registry_imports.py
```

Smoke test: `/extractor/video?host=<name>&d=<test-url>` then the same with `&warp=off` and `&proxy=off` — both must come out on the right route.

Pre-PR checklist: `?warp=off`, `?proxy=off`, forced `?proxy=<alias>` and `TRANSPORT_ROUTES` honored; admin `warp_off/proxy_off` work (separate cache key); 403 re-extract reuses the right route (returns the 3 fields); URL generator + recordings show the host; `available_hosts` lists it.
