import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# Conditional import for DLHD detection
# (Rimosso perche non serve piu logica speciale per DLHD nel rewriter)
try:
    DLHDExtractor = None  # Placeholder per compatibilita se servisse in futuro
except ImportError:
    pass


class ManifestRewriter:
    @staticmethod
    def rewrite_mpd_manifest(
        manifest_content: str,
        base_url: str,
        proxy_base: str,
        stream_headers: dict,
        clearkey_param: str = None,
        api_password: str = None,
        bypass_warp: bool = False,
        disable_ssl: bool = False,
    ) -> str:
        """Riscrive i manifest MPD (DASH) per passare attraverso il proxy."""
        try:
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
            
            if bypass_warp:
                header_params += "&warp=off"

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
                    license_url = f"{proxy_base}/license?clearkey={clearkey_param}"
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
        disable_ssl: bool = False,
        selected_proxy: str = None,
    ) -> str:
        """Riscrive gli URL nei manifest HLS per passare attraverso il proxy."""
        lines = manifest_content.split("\n")
        rewritten_lines = []

        # Determina se e VixSrc (logica speciale per quality selection)
        is_vixsrc_stream = False

        try:
            if get_extractor_func:
                original_request_url = (
                    stream_headers.get("referer")
                    or stream_headers.get("Referer")
                    or base_url
                )
                extractor = await get_extractor_func(original_request_url, {})

                if hasattr(extractor, "is_vixsrc") and extractor.is_vixsrc:
                    is_vixsrc_stream = True
                    logger.debug("Detected VixSrc stream.")
        except Exception as e:
            logger.error(f"Error in extractor detection: {e}")

        # no_bypass e mantenuto per compatibilita, ma il rewriter ora proxa sempre.
        _ = no_bypass

        # VixSrc used to have a custom master-playlist rewrite here.
        # ExoPlayer is stricter than VLC about HLS master/media relationships,
        # so we now let VixSrc fall through to the generic HLS rewriting path.
        _ = is_vixsrc_stream

        # Generic master-playlist optimization: keep only the highest-bandwidth
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
            
            if disable_ssl:
                header_params += "&disable_ssl=1"
            
            if selected_proxy:
                # Usiamo un formato pulito per evitare double-encoding
                header_params += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"

            absolute_variant_url = urljoin(base_url, highest_quality_stream["url"])
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
            for line in lines:
                if not line.startswith("#EXT-X-MEDIA:") or 'URI="' not in line:
                    continue

                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)
                if uri_start <= 4 or uri_end <= uri_start:
                    proxied_media_lines.append(line.strip())
                    continue

                media_url = urljoin(base_url, line[uri_start:uri_end])
                if shorten_url_func:
                    url_id = await shorten_url_func(media_url)
                    proxy_media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?hls_url_id={url_id}{header_params}"
                else:
                    encoded_media_url = urllib.parse.quote(media_url, safe="")
                    proxy_media_url = (
                        f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_media_url}{header_params}"
                    )
                proxied_media_lines.append(line[:uri_start] + proxy_media_url + line[uri_end:])

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
            rewritten_lines.append(highest_quality_stream["inf"])
            rewritten_lines.append(proxy_variant_url)

            return "\n".join(rewritten_lines)

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
        
        if disable_ssl:
            header_params += "&disable_ssl=1"
        
        if selected_proxy:
            header_params += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"

        # Estrai query params dal base_url per ereditarli se necessario
        base_parsed = urllib.parse.urlparse(base_url)
        base_query = base_parsed.query

        for line in lines:
            line = line.strip()

            # 1. GESTIONE CHIAVI AES-128
            if line.startswith("#EXT-X-KEY:") and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_key_url = line[uri_start:uri_end]
                    absolute_key_url = urljoin(base_url, original_key_url)

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
                    if disable_ssl:
                        proxy_key_url += "&disable_ssl=1"
                    if selected_proxy:
                        proxy_key_url += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"

                    new_line = line[:uri_start] + proxy_key_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 2. GESTIONE MEDIA (Sottotitoli, Audio secondario)
            elif line.startswith("#EXT-X-MEDIA:") and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_media_url = line[uri_start:uri_end]
                    absolute_media_url = urljoin(base_url, original_media_url)
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
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_iframe_url = line[uri_start:uri_end]
                    absolute_iframe_url = urljoin(base_url, original_iframe_url)
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
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_key_url = line[uri_start:uri_end]
                    absolute_key_url = urljoin(base_url, original_key_url)
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
                    if disable_ssl:
                        proxy_key_url += "&disable_ssl=1"

                    new_line = line[:uri_start] + proxy_key_url + line[uri_end:]
                    rewritten_lines.append(new_line)
                else:
                    rewritten_lines.append(line)

            # 3. GESTIONE MAP (Init Segment per fMP4)
            elif line.startswith("#EXT-X-MAP:") and 'URI=' in line:
                uri_start = line.find('URI="') + 5
                uri_end = line.find('"', uri_start)

                if uri_start > 4 and uri_end > uri_start:
                    original_map_url = line[uri_start:uri_end]
                    absolute_map_url = urljoin(base_url, original_map_url)
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
                if base_query and "?" not in absolute_url:
                    absolute_url += f"?{base_query}"

                encoded_url = urllib.parse.quote(absolute_url, safe="")

                # Se e .m3u8 usa /proxy/hls/manifest.m3u8, altrimenti determina estensione
                if ".m3u8" in absolute_url:
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
                # Tutti gli altri tag (es. #EXTINF, #EXT-X-ENDLIST)
                rewritten_lines.append(line)

        return "\n".join(rewritten_lines)
