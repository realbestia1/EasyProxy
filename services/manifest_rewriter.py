import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from services.secure_state import seal_state

logger = logging.getLogger(__name__)

# Conditional import for DLHD detection
# (Rimosso perche non serve piu logica speciale per DLHD nel rewriter)
try:
    DLHDExtractor = None  # Placeholder per compatibilita se servisse in futuro
except ImportError:
    pass


class ManifestRewriter:
    @staticmethod
    def _inherit_query_if_missing(absolute_url: str, base_query: str) -> str:
        if not base_query:
            return absolute_url
        parsed_url = urllib.parse.urlparse(absolute_url)
        if parsed_url.query:
            return absolute_url
        return urllib.parse.urlunparse(parsed_url._replace(query=base_query))

    @staticmethod
    def _required_hls_version(manifest_content: str) -> int:
        """Compute the minimum HLS version required by the tags in the manifest."""
        version = 3
        m = re.search(r'#EXT-X-VERSION:(\d+)', manifest_content)
        if m:
            version = max(version, int(m.group(1)))
        if '#EXT-X-MEDIA:' in manifest_content or \
           '#EXT-X-I-FRAME-STREAM-INF:' in manifest_content or \
           '#EXT-X-BYTERANGE' in manifest_content:
            version = max(version, 4)
        if '#EXT-X-MAP:' in manifest_content:
            version = max(version, 6)
        return version

    @staticmethod
    def _ensure_hls_version(manifest_content: str) -> str:
        """Make sure the manifest has a correct #EXT-X-VERSION line after #EXTM3U."""
        version = ManifestRewriter._required_hls_version(manifest_content)
        lines = manifest_content.split("\n")
        new_lines = []
        has_version = False
        for line in lines:
            stripped = line.strip()
            if stripped == "#EXTM3U":
                new_lines.append(line)
                new_lines.append(f"#EXT-X-VERSION:{version}")
                continue
            if stripped.startswith("#EXT-X-VERSION:"):
                has_version = True
                continue
            new_lines.append(line)
        if not new_lines:
            return f"#EXTM3U\n#EXT-X-VERSION:{version}"
        return "\n".join(new_lines)

    @staticmethod
    def rewrite_mpd_native(
        manifest_content: str,
        mpd_url: str,
        proxy_base: str,
        stream_headers: dict,
        clearkey_param: str = None,
        api_password: str = None,
        bypass_warp: bool = False,
        disable_ssl: bool = False,
        session_id: str = None
    ) -> str:
        """Riscrive il manifest MPD per DASH nativo (senza conversione HLS)."""
        try:
            # 1. Pulizia DRM e pssh (come nella versione Android)
            # Rimuove blocchi ContentProtection (sia autochiudenti che con body)
            mpd = manifest_content
            mpd = re.sub(r'<ContentProtection[\s\S]*?</ContentProtection>', '', mpd, flags=re.IGNORECASE)
            mpd = re.sub(r'<ContentProtection[^>]*/>', '', mpd, flags=re.IGNORECASE)
            
            # Rimuove cenc:pssh
            mpd = re.sub(r'<cenc:pssh>[\s\S]*?</cenc:pssh>', '', mpd, flags=re.IGNORECASE)
            mpd = re.sub(r'<cenc:pssh[^>]*/>', '', mpd, flags=re.IGNORECASE)

            # Rimuove BaseURL esistenti (tutti i livelli)
            mpd = re.sub(r'<BaseURL>[^<]*</BaseURL>\s*', '', mpd)

            # 2. Inserimento BaseURL che punta al nostro proxy
            # Il path sarà /proxy/mpd/segment/{sessionId}/
            proxy_segment_base = f"{proxy_base}/proxy/mpd/segment/{session_id}/"
            
            mpd_tag_match = re.search(r'(<MPD[^>]*>)', mpd, re.IGNORECASE)
            if mpd_tag_match:
                insert_pos = mpd_tag_match.end()
                mpd = mpd[:insert_pos] + f"\n  <BaseURL>{proxy_segment_base}</BaseURL>" + mpd[insert_pos:]

            return mpd
        except Exception as e:
            logger.error(f"Error during native MPD rewrite: {e}")
            return manifest_content

    @staticmethod
    def rewrite_mpd_manifest(
        manifest_content: str,
        base_url: str,
        proxy_base: str,
        stream_headers: dict,
        clearkey_param: str = None,
        api_password: str = None,
        bypass_warp: bool = False,
        bypass_proxies: bool = False,
        disable_ssl: bool = False,
        drm_token: str = None,
    ) -> str:
        """Riscrive i manifest MPD (DASH) per passare attraverso il proxy."""
        try:
            if clearkey_param and not drm_token:
                drm_token = seal_state(
                    {"clearkey": clearkey_param}, "clearkey"
                )
            # Aggiungiamo il namespace di default se non presente, per ET
            if "xmlns" not in manifest_content:
                manifest_content = manifest_content.replace(
                    "<MPD", '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1
                )

            root = ET.fromstring(manifest_content)
            ns = {
                "mpd": "urn:mpeg:dash:schema:mpd:2011",
                "cenc": "urn:mpeg:cenc:2013",
                "dashif": "http://dashif.org/guidelines/clearKey",
            }

            # Registra i namespace per evitare prefissi ns0
            ET.register_namespace("", ns["mpd"])
            ET.register_namespace("cenc", ns["cenc"])
            ET.register_namespace("dashif", ns["dashif"])

            # Includiamo tutti gli header rilevanti passati dall'estrattore
            header_params = "".join(
                [
                    f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}"
                    for key, value in stream_headers.items()
                ]
            )

            if api_password:
                header_params += f"&api_password={api_password}"
            if drm_token:
                header_params += (
                    f"&drm_token={urllib.parse.quote(drm_token, safe='')}"
                )
            
            if bypass_warp:
                header_params += "&warp=off"
            if bypass_proxies:
                header_params += "&proxy=off"

            if disable_ssl:
                header_params += "&disable_ssl=1"

            def create_proxy_url(relative_url):
                # Skip proxying if URL contains DASH template variables - player must resolve these
                if "$" in relative_url:
                    # Just make it absolute without proxying
                    return urljoin(base_url, relative_url)
                absolute_url = urljoin(base_url, relative_url)
                encoded_url = urllib.parse.quote(absolute_url, safe="")
                return f"{proxy_base}/proxy/mpd/manifest.m3u8?d={encoded_url}{header_params}"

            # --- GESTIONE CLEARKEY STATICA ---
            if clearkey_param:
                try:
                    # Support multiple keys separated by comma
                    # Format: KID1:KEY1,KID2:KEY2
                    key_pairs = clearkey_param.split(",")

                    # Usa il primo KID come default per cenc:default_KID se disponibile
                    first_kid_hex = None
                    if key_pairs:
                        first_pair = key_pairs[0]
                        if ":" in first_pair:
                            first_kid_hex = first_pair.split(":")[0]

                    # Crea l'elemento ContentProtection per ClearKey
                    cp_element = ET.Element("ContentProtection")
                    cp_element.set(
                        "schemeIdUri",
                        "urn:uuid:e2719d58-a985-b3c9-781a-007147f192ec",
                    )
                    cp_element.set("value", "ClearKey")

                    # Puntiamo al nostro endpoint /license
                    license_url = (
                        f"{proxy_base}/license?drm_token="
                        f"{urllib.parse.quote(drm_token, safe='')}"
                    )
                    if api_password:
                        license_url += f"&api_password={api_password}"

                    # 1. Laurl standard (namespace MPD)
                    laurl_element = ET.SubElement(
                        cp_element, "{urn:mpeg:dash:schema:mpd:2011}Laurl"
                    )
                    laurl_element.text = license_url

                    # 2. dashif:Laurl (namespace DashIF)
                    laurl_dashif = ET.SubElement(
                        cp_element, "{http://dashif.org/guidelines/clearKey}Laurl"
                    )
                    laurl_dashif.text = license_url

                    # 3. Aggiungi cenc:default_KID
                    if first_kid_hex and len(first_kid_hex) == 32:
                        kid_guid = (
                            f"{first_kid_hex[:8]}-{first_kid_hex[8:12]}-"
                            f"{first_kid_hex[12:16]}-{first_kid_hex[16:20]}-"
                            f"{first_kid_hex[20:]}"
                        )
                        cp_element.set(
                            "{urn:mpeg:cenc:2013}default_KID", kid_guid
                        )

                    # Inietta ContentProtection
                    adaptation_sets = root.findall(".//mpd:AdaptationSet", ns)
                    logger.debug(
                        f"Found {len(adaptation_sets)} AdaptationSet in manifest."
                    )

                    for adaptation_set in adaptation_sets:
                        # RIMUOVI altri ContentProtection (es. Widevine)
                        for cp in adaptation_set.findall("mpd:ContentProtection", ns):
                            scheme = cp.get("schemeIdUri", "").lower()
                            if "e2719d58-a985-b3c9-781a-007147f192ec" not in scheme:
                                adaptation_set.remove(cp)
                                logger.debug(
                                    f"Removed conflicting ContentProtection: {scheme}"
                                )

                        # Verifica se esiste gia ClearKey
                        existing_cp = False
                        for cp in adaptation_set.findall("mpd:ContentProtection", ns):
                            if (
                                cp.get("schemeIdUri")
                                == "urn:uuid:e2719d58-a985-b3c9-781a-007147f192ec"
                            ):
                                existing_cp = True
                                break

                        if not existing_cp:
                            adaptation_set.insert(0, cp_element)
                            logger.debug(
                                "Injected static ClearKey ContentProtection in AdaptationSet"
                            )

                except Exception as e:
                    logger.error(f"Error parsing clearkey parameter: {e}")

            # --- GESTIONE PROXY LICENZE ESISTENTI ---
            for cp in root.findall(".//mpd:ContentProtection", ns):
                for child in cp:
                    if "Laurl" in child.tag and child.text:
                        original_license_url = child.text
                        encoded_license_url = urllib.parse.quote(
                            original_license_url, safe=""
                        )
                        proxy_license_url = (
                            f"{proxy_base}/license?url={encoded_license_url}{header_params}"
                        )
                        child.text = proxy_license_url
                        logger.debug(
                            f"Redirected License URL: {original_license_url} -> {proxy_license_url}"
                        )

            # Riscrive gli attributi URL
            for template_tag in root.findall(".//mpd:SegmentTemplate", ns):
                for attr in ["media", "initialization"]:
                    if template_tag.get(attr):
                        template_tag.set(attr, create_proxy_url(template_tag.get(attr)))

            for seg_url_tag in root.findall(".//mpd:SegmentURL", ns):
                if seg_url_tag.get("media"):
                    seg_url_tag.set("media", create_proxy_url(seg_url_tag.get("media")))

            for base_url_tag in root.findall(".//mpd:BaseURL", ns):
                if base_url_tag.text:
                    base_url_tag.text = create_proxy_url(base_url_tag.text)

            return ET.tostring(root, encoding="unicode", method="xml")

        except Exception as e:
            logger.error(f"Error during MPD manifest rewrite: {e}")
            return manifest_content

    @staticmethod
    async def rewrite_manifest_urls(
        manifest_content: str,
        base_url: str,
        proxy_base: str,
        stream_headers: dict,
        original_channel_url: str = "",
        api_password: str = None,
        get_extractor_func=None,
        no_bypass: bool = False,
        shorten_url_func=None,
        bypass_warp: bool = False,
        bypass_proxies: bool = False,
        disable_ssl: bool = False,
        selected_proxy: str = None,
        force_direct: bool = False,
        extractor_key: str = None,
        stream_key: str = None,
    ) -> str:
        """Riscrive gli URL nei manifest HLS per passare attraverso il proxy."""
        lines = manifest_content.split("\n")
        rewritten_lines = []

        # no_bypass e mantenuto per compatibilita, ma il rewriter ora proxa sempre.
        _ = no_bypass

        # Master-playlist optimization: keep only the highest-bandwidth
        # video variant, while preserving audio/media tags and other metadata.
        generic_streams = []
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
                bandwidth_match = re.search(r"BANDWIDTH=(\d+)", line)
                bandwidth = int(bandwidth_match.group(1)) if bandwidth_match else 0
                generic_streams.append(
                    {
                        "index": i,
                        "bandwidth": bandwidth,
                        "inf": line,
                        "url": lines[i + 1],
                    }
                )

        if generic_streams:
            highest_quality_stream = max(generic_streams, key=lambda x: x["bandwidth"])
            logger.debug(
                "Generic HLS: selected max bandwidth %s.",
                highest_quality_stream["bandwidth"],
            )
            # Warn if the CDN is serving an audio-only stream. Consider video proven either
            # by an explicit CODECS= video entry or by a RESOLUTION= attribute (many CDNs
            # omit CODECS while still serving video, so RESOLUTION is enough evidence).
            _selected_inf = highest_quality_stream["inf"]
            _has_video_codec = any(
                vc in _selected_inf for vc in ("avc1", "hvc1", "dvh1", "hev1", "vp9", "av01")
            ) or re.search(r"RESOLUTION=\d+x\d+", _selected_inf) is not None
            if not _has_video_codec:
                logger.warning(
                    "HLS master manifest has no video codec in selected variant (CDN may be serving audio-only). "
                    "STREAM-INF: %s", _selected_inf
                )
            base_query = urllib.parse.urlparse(base_url).query

            header_params = "".join(
                [
                    f"&h_{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
                    for key, value in stream_headers.items()
                ]
            )

            if api_password:
                header_params += f"&api_password={api_password}"
            
            if bypass_warp:
                header_params += "&warp=off"
            if bypass_proxies:
                header_params += "&proxy=off"
            
            if disable_ssl:
                header_params += "&disable_ssl=1"
            
            if selected_proxy:
                # Usiamo un formato pulito per evitare double-encoding
                header_params += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"
            if force_direct:
                header_params += "&direct=1"
            if original_channel_url:
                header_params += f"&orig_url={urllib.parse.quote(original_channel_url, safe='')}"
            if extractor_key:
                header_params += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
            if stream_key:
                header_params += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"

            absolute_variant_url = ManifestRewriter._inherit_query_if_missing(
                urljoin(base_url, highest_quality_stream["url"]),
                base_query,
            )
            if shorten_url_func:
                url_id = await shorten_url_func(absolute_variant_url)
                proxy_variant_url = f"{proxy_base}/proxy/hls/manifest.m3u8?hls_url_id={url_id}{header_params}"
            else:
                encoded_variant_url = urllib.parse.quote(absolute_variant_url, safe="")
                proxy_variant_url = (
                    f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_variant_url}{header_params}"
                )
            
            if selected_proxy and "&proxy=" not in proxy_variant_url:
                proxy_variant_url += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"

            proxied_media_lines = []
            is_dlstreams_or_premium = "dlstreams" in base_url.lower() or "dlhd" in base_url.lower() or "/premium" in base_url.lower()
            # Track which group-ids survive filtering (have at least one proxied media line)
            surviving_group_ids = set()
            for line in lines:
                if not line.startswith("#EXT-X-MEDIA:"):
                    continue
                if 'URI="' not in line:
                    # Media without URI (e.g. closed-captions=NONE): keep as-is and track group
                    group_match = re.search(r'GROUP-ID="([^"]+)"', line)
                    if group_match:
                        surviving_group_ids.add(group_match.group(1))
                    proxied_media_lines.append(line.strip())
                    continue

                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)
                if uri_start <= 4 or uri_end <= uri_start:
                    group_match = re.search(r'GROUP-ID="([^"]+)"', line)
                    if group_match:
                        surviving_group_ids.add(group_match.group(1))
                    proxied_media_lines.append(line.strip())
                    continue
                # Filter out unsigned/broken media tracks for DLStreams/premium streams.
                # Tracks without explicit query parameters (e.g. ?md5=...) always return 403 Forbidden.
                original_uri = line[uri_start:uri_end]
                if is_dlstreams_or_premium and "?" not in original_uri:
                    continue

                media_url = ManifestRewriter._inherit_query_if_missing(
                    urljoin(base_url, line[uri_start:uri_end]),
                    base_query,
                )
                if shorten_url_func:
                    url_id = await shorten_url_func(media_url)
                    proxy_media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?hls_url_id={url_id}{header_params}"
                else:
                    encoded_media_url = urllib.parse.quote(media_url, safe="")
                    proxy_media_url = (
                        f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_media_url}{header_params}"
                    )
                proxied_media_lines.append(line[:uri_start] + proxy_media_url + line[uri_end:])
                group_match = re.search(r'GROUP-ID="([^"]+)"', line)
                if group_match:
                    surviving_group_ids.add(group_match.group(1))

            def _strip_empty_group_refs(inf_line: str, surviving: set) -> str:
                """Remove SUBTITLES/AUDIO/CLOSED-CAPTIONS attributes that reference
                group-ids which were completely filtered out. This prevents player
                confusion (e.g. PotPlayer refusing video) when a STREAM-INF references
                a group that has no matching EXT-X-MEDIA entries."""
                for attr in ("SUBTITLES", "AUDIO", "CLOSED-CAPTIONS"):
                    match = re.search(rf'{attr}="([^"]+)"', inf_line)
                    if match and match.group(1) not in surviving:
                        # Remove the attribute and any surrounding comma
                        inf_line = re.sub(
                            rf',?\s*{attr}="[^"]+"', "", inf_line
                        ).strip().rstrip(",")
                        logger.debug(
                            "Stripped dangling %s group ref '%s' from STREAM-INF",
                            attr, match.group(1),
                        )
                return inf_line

            rewritten_lines.append("#EXTM3U")
            skip_next_url = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if skip_next_url:
                    skip_next_url = False
                    continue
                if any(stream["index"] == i for stream in generic_streams):
                    if i == highest_quality_stream["index"]:
                        skip_next_url = True
                        continue
                    skip_next_url = True
                    continue
                if stripped.startswith("#EXT-X-MEDIA:"):
                    continue
                if stripped.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
                    continue
                if stripped == "#EXTM3U":
                    continue
                rewritten_lines.append(stripped)

            rewritten_lines.extend([line for line in proxied_media_lines if line])
            # Strip dangling group refs from STREAM-INF before appending
            cleaned_inf = _strip_empty_group_refs(highest_quality_stream["inf"], surviving_group_ids)
            rewritten_lines.append(cleaned_inf)
            rewritten_lines.append(proxy_variant_url)

            return ManifestRewriter._ensure_hls_version("\n".join(rewritten_lines))

        # --- Logica Standard ---
        header_params = "".join(
            [
                f"&h_{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
                for key, value in stream_headers.items()
            ]
        )

        if api_password:
            header_params += f"&api_password={api_password}"
        
        if bypass_warp:
            header_params += "&warp=off"
        if bypass_proxies:
            header_params += "&proxy=off"
        
        if disable_ssl:
            header_params += "&disable_ssl=1"
        
        if selected_proxy:
            header_params += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"
        if force_direct:
            header_params += "&direct=1"
        if original_channel_url:
            header_params += f"&orig_url={urllib.parse.quote(original_channel_url, safe='')}"
        if extractor_key:
            header_params += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
        if stream_key:
            header_params += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"

        # Estrai query params dal base_url per ereditarli se necessario
        base_parsed = urllib.parse.urlparse(base_url)
        base_query = base_parsed.query

        next_uri_is_manifest = False
        for line in lines:
            line = line.strip()

            if line.startswith("#EXT-X-STREAM-INF:"):
                rewritten_lines.append(line)
                next_uri_is_manifest = True
                continue

            # 1. GESTIONE CHIAVI AES-128
            if line.startswith("#EXT-X-KEY:") and 'URI=' in line:
                next_uri_is_manifest = False
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_key_url = line[uri_start:uri_end]
                    absolute_key_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_key_url),
                        base_query,
                    )

                    encoded_key_url = urllib.parse.quote(absolute_key_url, safe="")
                    encoded_original_channel_url = urllib.parse.quote(
                        original_channel_url, safe=""
                    )

                    # Proxy KEY URL
                    proxy_key_url = (
                        f"{proxy_base}/key?key_url={encoded_key_url}"
                        f"&original_channel_url={encoded_original_channel_url}"
                    )

                    # Aggiungi header
                    key_header_params = "".join(
                        [
                            f"&h_{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
                            for key, value in stream_headers.items()
                        ]
                    )
                    proxy_key_url += key_header_params

                    if api_password:
                        proxy_key_url += f"&api_password={api_password}"
                    if bypass_warp:
                        proxy_key_url += "&warp=off"
                    if bypass_proxies:
                        proxy_key_url += "&proxy=off"
                    if disable_ssl:
                        proxy_key_url += "&disable_ssl=1"
                    if selected_proxy:
                        proxy_key_url += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"
                    if force_direct:
                        proxy_key_url += "&direct=1"

                    new_line = line[:uri_start] + proxy_key_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 2. GESTIONE MEDIA (Sottotitoli, Audio secondario)
            elif line.startswith("#EXT-X-MEDIA:") and 'URI=' in line:
                next_uri_is_manifest = False
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_media_url = line[uri_start:uri_end]
                    absolute_media_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_media_url),
                        base_query,
                    )
                    encoded_media_url = urllib.parse.quote(absolute_media_url, safe="")

                    # Usa endpoint manifest
                    if shorten_url_func:
                        url_id = await shorten_url_func(absolute_media_url)
                        proxy_media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?hls_url_id={url_id}{header_params}"
                    else:
                        proxy_media_url = (
                            f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_media_url}{header_params}"
                        )
                    new_line = line[:uri_start] + proxy_media_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 2b. GESTIONE I-FRAME STREAMS
            elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:") and 'URI=' in line:
                next_uri_is_manifest = False
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_iframe_url = line[uri_start:uri_end]
                    absolute_iframe_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_iframe_url),
                        base_query,
                    )
                    encoded_iframe_url = urllib.parse.quote(absolute_iframe_url, safe="")

                    # Gli I-FRAME sono solitamente m3u8 o segmenti a sé stanti
                    if shorten_url_func:
                        url_id = await shorten_url_func(absolute_iframe_url)
                        proxy_iframe_url = f"{proxy_base}/proxy/hls/manifest.m3u8?hls_url_id={url_id}{header_params}"
                    else:
                        proxy_iframe_url = (
                            f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_iframe_url}{header_params}"
                        )
                    new_line = line[:uri_start] + proxy_iframe_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 2c. GESTIONE SESSION-KEY
            elif line.startswith("#EXT-X-SESSION-KEY:") and 'URI=' in line:
                next_uri_is_manifest = False
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_key_url = line[uri_start:uri_end]
                    absolute_key_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_key_url),
                        base_query,
                    )
                    encoded_key_url = urllib.parse.quote(absolute_key_url, safe="")
                    
                    # Proxy KEY URL (come per #EXT-X-KEY)
                    proxy_key_url = (
                        f"{proxy_base}/key?key_url={encoded_key_url}"
                        f"&h_Referer={urllib.parse.quote(base_url, safe='')}"
                    )
                    proxy_key_url += header_params
                    if api_password:
                        proxy_key_url += f"&api_password={api_password}"
                    if bypass_warp:
                        proxy_key_url += "&warp=off"
                    if bypass_proxies:
                        proxy_key_url += "&proxy=off"
                    if disable_ssl:
                        proxy_key_url += "&disable_ssl=1"

                    new_line = line[:uri_start] + proxy_key_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 3. GESTIONE MAP (Init Segment per fMP4)
            elif line.startswith("#EXT-X-MAP:") and 'URI=' in line:
                next_uri_is_manifest = False
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_map_url = line[uri_start:uri_end]
                    absolute_map_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_map_url),
                        base_query,
                    )
                    encoded_map_url = urllib.parse.quote(absolute_map_url, safe="")

                    # Usa endpoint segment.mp4
                    proxy_map_url = (
                        f"{proxy_base}/proxy/hls/segment.mp4?d={encoded_map_url}{header_params}"
                    )

                    new_line = line[:uri_start] + proxy_map_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 4. GESTIONE SEGMENTI E SUB-MANIFEST
            elif line and not line.startswith("#"):
                absolute_url = urljoin(base_url, line) if not line.startswith("http") else line

                # Eredita query params (es. token)
                absolute_url = ManifestRewriter._inherit_query_if_missing(
                    absolute_url,
                    base_query,
                )

                encoded_url = urllib.parse.quote(absolute_url, safe="")

                # Variant URIs after #EXT-X-STREAM-INF are playlists even when
                # providers like VixSrc expose them as extensionless /playlist URLs.
                is_manifest_uri = next_uri_is_manifest or ".m3u8" in absolute_url
                next_uri_is_manifest = False

                # Se e manifest usa /proxy/hls/manifest.m3u8, altrimenti determina estensione
                if is_manifest_uri:
                    if shorten_url_func:
                        url_id = await shorten_url_func(absolute_url)
                        proxy_url = f"{proxy_base}/proxy/hls/manifest.m3u8?hls_url_id={url_id}{header_params}"
                    else:
                        proxy_url = (
                            f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}{header_params}"
                        )
                else:
                    # Mantieni un endpoint coerente con il tipo reale del segmento
                    path = urllib.parse.urlparse(absolute_url).path
                    ext = ".ts"
                    if path.endswith(".vtt") or path.endswith(".webvtt"):
                        ext = ".vtt"
                    elif (
                        path.endswith(".m4s")
                        or path.endswith(".mp4")
                        or path.endswith(".m4v")
                    ):
                        ext = ".mp4"

                    proxy_url = (
                        f"{proxy_base}/proxy/hls/segment{ext}?d={encoded_url}{header_params}"
                    )

                rewritten_lines.append(proxy_url)

            else:
                next_uri_is_manifest = False
                # Tutti gli altri tag (es. #EXTINF, #EXT-X-ENDLIST)
                rewritten_lines.append(line)

        return ManifestRewriter._ensure_hls_version("\n".join(rewritten_lines))
