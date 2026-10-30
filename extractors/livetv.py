import re
from urllib.parse import urljoin, urlparse, unquote
from aiohttp import FormData
from extractors.base import BaseExtractor, ExtractorError


class LiveTVExtractor(BaseExtractor):
    """LiveTV URL extractor for both M3U8 and MPD streams."""

    def __init__(self, request_headers: dict, proxies: list = None, bypass_warp: bool = False):
        super().__init__(request_headers, proxies, extractor_name="livetv", bypass_warp=bypass_warp)
        
        # Patterns for stream URL extraction
        self.fallback_pattern = re.compile(
            r"source: [\'\"](.*?)[\'\"]\s*,\s*[\s\S]*?mimeType: [\'\"](application/x-mpegURL|application/vnd\.apple\.mpegURL|application/dash\+xml)[\'\"]",
            re.IGNORECASE,
        )
        self.any_m3u8_pattern = re.compile(
            r'["\']?(https?://.*?\.m3u8(?:\?[^"\']*)?)["\'"]?',
            re.IGNORECASE,
        )

    def _result(
        self,
        destination_url: str,
        request_headers: dict = None,
        mediaflow_endpoint: str = "hls_proxy",
        **kwargs,
    ) -> dict:
        result = {
            "destination_url": destination_url,
            "request_headers": request_headers or self.base_headers,
            "mediaflow_endpoint": mediaflow_endpoint,
            "selected_proxy": self.last_used_proxy,
            "force_direct": self._force_direct,
            "bypass_warp": self.bypass_warp_active,
        }
        result.update(kwargs)
        return result

    async def extract(self, url: str, stream_title: str = None, **kwargs) -> dict:
        """Extract LiveTV URL and required headers."""
        self._apply_routing_kwargs(url, kwargs)
        try:
            is_livetv = (
                bool(re.search(r"livetv\d*\.(?:sx|me|ru|org)", url.lower()))
                or any(k in url.lower() for k in ["webplayer", "/eventinfo/", "emb.apl", "alieztv"])
            )
            if is_livetv:
                return await self._extract_livetvsx(url, stream_title, **kwargs)

            resp = await self._make_request(url, ssl=False)
            response_text = resp.text
            
            self.base_headers["referer"] = urljoin(url, "/")

            # Extract player API details
            player_api_base, method = await self._extract_player_api_base(response_text)
            if not player_api_base:
                # Fallback to livetv.sx logic if player_api is missing
                return await self._extract_livetvsx(url, stream_title, **kwargs)

            # Get player options
            options_data = await self._get_player_options(response_text)
            if not options_data:
                raise ExtractorError("No player options found")

            # Process player options to find matching stream
            for option in options_data:
                current_title = option.get("title")
                if stream_title and current_title != stream_title:
                    continue

                # Get stream URL based on player option
                stream_data = await self._process_player_option(
                    player_api_base, method, option.get("post"), option.get("nume"), option.get("type")
                )

                if stream_data:
                    stream_url = stream_data.get("url")
                    if not stream_url:
                        continue

                    result = self._result(
                        stream_url,
                        self.base_headers,
                        mediaflow_endpoint=self.mediaflow_endpoint,
                    )

                    # Set endpoint based on stream type
                    if stream_data.get("type") == "mpd":
                        if stream_data.get("drm_key_id") and stream_data.get("drm_key"):
                            result.update({
                                "query_params": {
                                    "key_id": stream_data["drm_key_id"],
                                    "key": stream_data["drm_key"],
                                },
                                "mediaflow_endpoint": "mpd_manifest_proxy",
                            })

                    return result

            raise ExtractorError("No valid stream found")

        except Exception as e:
            raise ExtractorError(f"Extraction failed: {str(e)}")

    async def _extract_livetvsx(self, url: str, stream_title: str = None, **kwargs) -> dict:
        """Extract stream from livetv.sx (event page or webplayer URL)."""
        # Event page resolution
        if "/eventinfo/" in url:
            resp = await self._make_request(url, ssl=False, headers={"Referer": "https://livetv.sx/"})
            html = resp.text
            matches = re.findall(r'<a[^>]+href=["\'](/webplayer[^"\']+)["\']', html)
            if not matches:
                matches = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*webplayer[^"\']+)["\']', html)
            if not matches:
                raise ExtractorError("No webplayer links found in livetv.sx event page")
            
            chosen_url = None
            if stream_title:
                for tr_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
                    tr_text = tr_match.group(1)
                    if stream_title.lower() in tr_text.lower():
                        link_match = re.search(r'href=["\'](/webplayer[^"\']+)["\']', tr_text)
                        if link_match:
                            chosen_url = urljoin(url, link_match.group(1))
                            break
            
            if not chosen_url:
                # Prioritize Italian flag: linkflag/12.png
                for tr_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
                    tr_text = tr_match.group(1)
                    if "linkflag/12.png" in tr_text or "italian" in tr_text.lower():
                        link_match = re.search(r'href=["\'](/webplayer[^"\']+)["\']', tr_text)
                        if link_match:
                            chosen_url = urljoin(url, link_match.group(1))
                            break
            
            if not chosen_url:
                chosen_url = urljoin(url, matches[0])
            url = chosen_url

        # Queue for multi-level iframe extraction
        queue = [(url, "https://livetv.sx/", 0)]
        visited = set()
        ad_domains = ["banner", "getbanner", "ads.", "cpxinteractive", "histats", "facebook", "twitter", "lshls.st", "links"]

        # Fast-track AliezTV if cid is in URL
        aliez_url_m = re.search(r'[?&]c=(\d+)', url)
        if ("alieztv" in url.lower() or "emb.apl" in url.lower()) and aliez_url_m:
            cid = aliez_url_m.group(1)
            queue.insert(0, (f"https://emb.apl625.online/player/live.php?id={cid}&w=700&h=480", "https://livetv.sx/", 1))

        while queue:
            curr_url, ref, depth = queue.pop(0)
            if curr_url in visited or depth > 4:
                continue
            visited.add(curr_url)

            # Skip ad domains
            if any(ad in curr_url.lower() for ad in ad_domains):
                continue

            # Automatically update outdated AliezTV domains (e.g. apl620.online -> apl625.online)
            if re.search(r'apl\d+\.online', curr_url):
                curr_url = re.sub(r'apl\d+\.online', 'apl625.online', curr_url)

            # Check if current URL is dlive / thedaddy / daddylive
            if any(d in curr_url.lower() for d in ["thedaddy.pk", "dlive.sx", "daddylive"]):
                from extractors.dlstreams import DLStreamsExtractor
                dl_extractor = DLStreamsExtractor(
                    self.request_headers,
                    proxies=self.proxies,
                    bypass_warp=bool(self.bypass_warp_active),
                )
                try:
                    return await dl_extractor.extract(curr_url, **kwargs)
                finally:
                    await dl_extractor.close()

            try:
                resp = await self._make_request(curr_url, ssl=False, headers={"Referer": ref})
                curr_html = resp.text
            except Exception:
                continue

            # Check for API-based player loaders (e.g. virazo.sx/ch.php?id=.. -> api/player.php?id=..)
            if "api/player.php" in curr_html or "ch.php?id=" in curr_url:
                ch_m = re.search(r'ch\.php\?id=(\d+)', curr_url) or re.search(r'[?&]id=(\d+)', curr_url)
                if ch_m:
                    ch_id = ch_m.group(1)
                    api_player_url = urljoin(curr_url, f"api/player.php?id={ch_id}")
                    try:
                        resp_api = await self._make_request(api_player_url, ssl=False, headers={"Referer": curr_url})
                        api_json = resp_api.json
                        if isinstance(api_json, dict) and api_json.get("url"):
                            player_url = api_json["url"]
                            if player_url not in visited:
                                queue.append((player_url, curr_url, depth + 1))
                    except Exception:
                        pass

            # 1. Check for dlive / thedaddy iframe
            dlive_match = re.search(r'<iframe[^>]+src=["\']((?:https?:)?//(?:www\.)?(?:thedaddy\.pk|dlive\.sx|daddylive)[^"\']+)["\']', curr_html, re.I)
            if dlive_match:
                dlive_url = dlive_match.group(1).replace("\n", "").replace("\r", "").strip()
                if dlive_url.startswith("//"):
                    dlive_url = "https:" + dlive_url
                from extractors.dlstreams import DLStreamsExtractor
                dl_extractor = DLStreamsExtractor(
                    self.request_headers,
                    proxies=self.proxies,
                    bypass_warp=bool(self.bypass_warp_active),
                )
                try:
                    return await dl_extractor.extract(dlive_url, **kwargs)
                finally:
                    await dl_extractor.close()

            # 2. Check for direct m3u8 in html (including protocol-relative //)
            m3u8_matches = re.findall(r'[\'"]((?:https?:)?//[^\s\'"<>]+?\.m3u8[^\s\'"<>]*)[\'"]', curr_html)
            for m in m3u8_matches:
                clean_m = m.replace(r'\/', '/').replace(r'\u0026', '&').replace('\\', '')
                if clean_m.startswith("//"):
                    clean_m = "https:" + clean_m
                if 'dummy' not in clean_m and not any(ad in clean_m.lower() for ad in ad_domains):
                    return self._result(
                        clean_m,
                        {"User-Agent": self.base_headers["User-Agent"], "Referer": curr_url},
                        mediaflow_endpoint="hls_proxy",
                    )

            # 3. Check for alieztv / emb.* pl.init
            m3u8_match = re.search(r'pl\.init\([\'\"](//[^\'\"]+\.m3u8[^\'\"]*)[\'\"]', curr_html)
            if not m3u8_match:
                m3u8_match = re.search(r'[\'\"](//[^\'\"]+\.m3u8[^\'\"]*)[\'\"]', curr_html)
            if m3u8_match:
                final_m3u8 = m3u8_match.group(1).replace("\n", "").replace("\r", "").strip()
                if final_m3u8.startswith("//"):
                    final_m3u8 = "https:" + final_m3u8
                return self._result(
                    final_m3u8,
                    {"User-Agent": self.base_headers["User-Agent"], "Referer": curr_url},
                    mediaflow_endpoint="hls_proxy",
                )

            # 4. Enqueue nested iframes
            iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', curr_html, re.I)
            for ifr in iframes:
                ifr_clean = ifr.replace("\n", "").replace("\r", "").strip()
                if ifr_clean.startswith("//"):
                    ifr_clean = "https:" + ifr_clean
                elif ifr_clean.startswith("/"):
                    ifr_clean = urljoin(curr_url, ifr_clean)
                if re.search(r'apl\d+\.online', ifr_clean):
                    ifr_clean = re.sub(r'apl\d+\.online', 'apl625.online', ifr_clean)
                if ifr_clean not in visited and not any(ad in ifr_clean.lower() for ad in ad_domains):
                    queue.append((ifr_clean, curr_url, depth + 1))

        raise ExtractorError("Could not extract stream from livetv.sx")

    async def _extract_player_api_base(self, html_content: str):
        """Extract player API base URL and method."""
        admin_ajax_pattern = r'"player_api"\s*:\s*"([^"]+)".*?"play_method"\s*:\s*"([^"]+)"'
        match = re.search(admin_ajax_pattern, html_content)
        if not match:
            return None, None
        url = match.group(1).replace("\\/", "/")
        method = match.group(2)
        if method == "wp_json":
            return url, method
        url = urljoin(url, "/wp-admin/admin-ajax.php")
        return url, method

    async def _get_player_options(self, html_content: str) -> list:
        """Extract player options from HTML content."""
        pattern = r'<li[^>]*class=["\']dooplay_player_option["\'][^>]*data-type=["\']([^"\']*)["\'][^>]*data-post=["\']([^"\']*)["\'][^>]*data-nume=["\']([^"\']*)["\'][^>]*>.*?<span class=["\']title["\']>([^<]*)</span>'
        matches = re.finditer(pattern, html_content, re.DOTALL)
        return [
            {"type": match.group(1), "post": match.group(2), "nume": match.group(3), "title": match.group(4).strip()}
            for match in matches
        ]

    async def _process_player_option(self, api_base: str, method: str, post: str, nume: str, type_: str) -> dict:
        """Process player option to get stream URL."""
        await self._get_session(api_base)
        
        if method == "wp_json":
            api_url = f"{api_base}{post}/{type_}/{nume}"
            resp = await self._make_request(api_url)
            data = resp.json
        else:
            form_data = FormData()
            form_data.add_field("action", "doo_player_ajax")
            form_data.add_field("post", post)
            form_data.add_field("nume", nume)
            form_data.add_field("type", type_)
            resp = await self._make_request(api_base, method="POST", data=form_data)
            data = resp.json

        # Get iframe URL from API response
        try:
            iframe_url = urljoin(api_base, data.get("embed_url", "").replace("\\/", "/"))

            # Get stream URL from iframe
            resp_iframe = await self._make_request(iframe_url)
            iframe_text = resp_iframe.text
            iframe_headers = resp_iframe.headers
            
            stream_data = await self._extract_stream_url(iframe_text, iframe_headers, iframe_url)
            return stream_data

        except Exception as e:
            raise ExtractorError(f"Failed to process player option: {str(e)}")

    async def _extract_stream_url(self, iframe_text: str, iframe_headers: dict, iframe_url: str) -> dict:
        """Extract final stream URL from iframe content."""
        try:
            # Parse URL components
            parsed_url = urlparse(iframe_url)
            query_params = dict(param.split("=") for param in parsed_url.query.split("&") if "=" in param)

            # Check if content is already a direct M3U8 stream
            content_types = ["application/x-mpegurl", "application/vnd.apple.mpegurl"]
            content_type = iframe_headers.get("content-type", "")

            if any(ext in content_type.lower() for ext in content_types):
                return {"url": iframe_url, "type": "m3u8"}

            stream_data = {}

            # Check for source parameter in URL
            if "source" in query_params:
                stream_data = {
                    "url": urljoin(iframe_url, unquote(query_params["source"])),
                    "type": "m3u8",
                }

            # Check for MPD stream with DRM
            elif "zy" in query_params and ".mpd``" in query_params["zy"]:
                data = query_params["zy"].split("``")
                url = data[0]
                key_id, key = data[1].split(":")
                stream_data = {"url": url, "type": "mpd", "drm_key_id": key_id, "drm_key": key}

            # Check for tamilultra specific format
            elif "tamilultra" in iframe_url:
                stream_data = {"url": urljoin(iframe_url, parsed_url.query), "type": "m3u8"}

            # Try pattern matching for stream URLs
            else:
                channel_id = query_params.get("id", "")
                stream_url = None

                if channel_id:
                    # Try channel ID specific pattern
                    pattern = rf'{re.escape(channel_id)}["\']:\s*{{\s*["\']?url["\']?\s*:\s*["\']([^"\']+)["\']'
                    match = re.search(pattern, iframe_text)
                    if match:
                        stream_url = match.group(1)

                # Try fallback patterns if channel ID pattern fails
                if not stream_url:
                    for pattern in [self.fallback_pattern, self.any_m3u8_pattern]:
                        match = pattern.search(iframe_text)
                        if match:
                            stream_url = match.group(1)
                            break

                if stream_url:
                    stream_data = {"url": stream_url, "type": "m3u8"}

                    # Check for MPD stream and extract DRM keys
                    if stream_url.endswith(".mpd"):
                        stream_data["type"] = "mpd"
                        drm_data = await self._extract_drm_keys(iframe_text, channel_id)
                        if drm_data:
                            stream_data.update(drm_data)

            # If no stream data found, raise error
            if not stream_data:
                raise ExtractorError("No valid stream URL found")

            # Update stream type based on URL if not already set
            if stream_data.get("type") == "m3u8":
                if stream_data["url"].endswith(".mpd"):
                    stream_data["type"] = "mpd"
                elif not any(ext in stream_data["url"] for ext in [".m3u8", ".m3u"]):
                    stream_data["type"] = "m3u8"

            return stream_data

        except Exception as e:
            raise ExtractorError(f"Failed to extract stream URL: {str(e)}")

    async def _extract_drm_keys(self, html_content: str, channel_id: str) -> dict:
        """Extract DRM keys for MPD streams."""
        try:
            # Pattern for channel entry
            channel_pattern = rf'"{re.escape(channel_id)}":\s*{{[^}}]+}}'
            channel_match = re.search(channel_pattern, html_content)

            if channel_match:
                channel_data = channel_match.group(0)

                # Try clearkeys pattern first
                clearkey_pattern = r'["\']?clearkeys["\']?\s*:\s*{\s*["\'](.+?)["\']:\s*["\'](.+?)["\']'
                clearkey_match = re.search(clearkey_pattern, channel_data)

                # Try k1/k2 pattern if clearkeys not found
                if not clearkey_match:
                    k1k2_pattern = r'["\']?k1["\']?\s*:\s*["\'](.+?)["\'],\s*["\']?k2["\']?\s*:\s*["\'](.+?)["\']'
                    k1k2_match = re.search(k1k2_pattern, channel_data)

                    if k1k2_match:
                        return {"drm_key_id": k1k2_match.group(1), "drm_key": k1k2_match.group(2)}
                else:
                    return {"drm_key_id": clearkey_match.group(1), "drm_key": clearkey_match.group(2)}

            return {}

        except Exception:
            return {}
