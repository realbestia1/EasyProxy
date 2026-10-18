import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from services.secure_state import seal_state
from utils.hls_master import attributes, master_entries, replace_uri, tag_uri

logger = logging.getLogger(__name__)


def _keep_best_representation(root, namespace: str) -> None:
    """Keep only the highest-bandwidth Representation of every AdaptationSet.

    The HLS rewriter already serves just the max variant, so pruning here
    keeps DASH output consistent instead of letting ABR start lower.
    """
    for adaptation_set in root.iter(namespace + "AdaptationSet"):
        representations = adaptation_set.findall(namespace + "Representation")
        if len(representations) < 2:
            continue

        def _bandwidth(representation):
            try:
                return int(representation.get("bandwidth") or 0)
            except (TypeError, ValueError):
                return 0

        best = max(representations, key=_bandwidth)
        for representation in representations:
            if representation is not best:
                adaptation_set.remove(representation)


class ManifestRewriter:
    @staticmethod
    def _inherit_query_if_missing(absolute_url: str, base_query: str) -> str:
        if not base_query:
            return absolute_url
        parsed_url = urllib.parse.urlparse(absolute_url)
        if parsed_url.query:
            return absolute_url
        # okcdn (ok.ru) firma il token nel path (/expires/.../video/): aggiungere la
        # query del manifest padre fa rispondere il master (o uno stub di errore).
        hostname = (parsed_url.hostname or "").lower()
        if hostname == "okcdn.ru" or hostname.endswith(".okcdn.ru"):
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
        for line in lines:
            stripped = line.strip()
            if stripped == "#EXTM3U":
                new_lines.append(line)
                new_lines.append(f"#EXT-X-VERSION:{version}")
                continue
            if stripped.startswith("#EXT-X-VERSION:"):
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
        session_id: str = None,
        forced_proxy: str = None,
        bypass_proxies: bool = False,
        extractor_key: str = None,
        stream_key: str = None,
        max_res: bool = False,
    ) -> str:
        """Riscrive il manifest MPD per DASH nativo (senza conversione HLS)."""
        import copy
        from services.proxy_dash import _encode_dash_state

        root = ET.fromstring(manifest_content)
        namespace = root.tag.split("}")[0] + "}" if "}" in root.tag else ""
        if max_res:
            _keep_best_representation(root, namespace)

        def relay(absolute, init_url=None, init_range=None):
            parsed = urllib.parse.urlsplit(absolute)
            path = parsed.path or "/"
            if "/" in path:
                directory, tail = path.rsplit("/", 1)
            else:
                directory, tail = "", path
            base_dir = (directory + "/") if not directory.endswith("/") else directory
            base_directory = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_dir, "", ""))
            if parsed.query:
                tail += "?" + parsed.query
            token = _encode_dash_state(base_directory, stream_headers, clearkey_param,
                resource_url=absolute, init_url=init_url, init_range=init_range,
                proxy=forced_proxy, bypass_warp=bypass_warp,
                bypass_proxies=bypass_proxies, extractor_key=extractor_key,
                stream_key=stream_key)
            # Keep the token path opaque: source query parameters belong in
            # authenticated state, not in the public relay URL query string.
            safe_tail = urllib.parse.quote(tail or "segment.mp4", safe="._-~$")
            return f"{proxy_base}/proxy/mpd/segment/{token}/{safe_tail}"

        def walk(node, base, inherited=None):
            bases = node.findall(namespace + "BaseURL")
            if bases:
                base = urljoin(base, bases[0].text or "")
            template = node.find(namespace + "SegmentTemplate")
            if template is None:
                template = node.find(namespace + "SegmentList")
            if template is None:
                template = node.find(namespace + "SegmentBase")
            effective = copy.deepcopy(inherited) if inherited is not None else None
            if template is not None:
                if effective is None or effective.tag != template.tag:
                    effective = copy.deepcopy(template)
                else:
                    effective.attrib.update(template.attrib)
                    if len(template):
                        effective[:] = copy.deepcopy(list(template))
            for child in list(node):
                if child.tag in (namespace + "Period", namespace + "AdaptationSet", namespace + "Representation"):
                    walk(child, base, effective)
            if node.tag == namespace + "Representation":
                if effective is None:
                    if clearkey_param:
                        raise ValueError("ClearKey DASH requires initialization metadata")
                    ET.SubElement(node, namespace + "BaseURL").text = relay(base)
                elif effective.tag != namespace + "SegmentTemplate":
                    init = effective.find(namespace + "Initialization")
                    init_url = urljoin(base, init.get("sourceURL", "") or "") if init is not None else None
                    if clearkey_param and not init_url:
                        init_url = base
                    init_range = init.get("range") if init is not None else None
                    if clearkey_param and init is None:
                        raise ValueError("ClearKey DASH requires initialization metadata")
                    if init is not None and init.get("sourceURL"):
                        init.set("sourceURL", relay(init_url, init_url, init_range))
                    for segment in effective.findall(namespace + "SegmentURL"):
                        for attr in ("media", "index"):
                            if segment.get(attr):
                                segment.set(attr, relay(urljoin(base, segment.get(attr)), init_url, init_range))
                    if effective.tag == namespace + "SegmentBase" or (init is not None and not init.get("sourceURL")):
                        ET.SubElement(node, namespace + "BaseURL").text = relay(base, init_url, init_range)
                    node.append(effective)
                else:
                    rewrite_template(node, effective, base)
            for child in list(node):
                if child in bases or child is template:
                    node.remove(child)
                elif clearkey_param and child.tag.rsplit("}", 1)[-1] in ("ContentProtection", "pssh"):
                    node.remove(child)

        def rewrite_template(node, effective, base):
                def expand(value):
                    return value.replace("$RepresentationID$", node.get("id", "")).replace(
                        "$Bandwidth$", node.get("bandwidth", ""))
                init = effective.get("initialization")
                init_url = urljoin(base, expand(init)) if init else None
                init_range = effective.get("initializationRange") or effective.get("range")
                if clearkey_param and not init_url:
                    raise ValueError("ClearKey DASH requires explicit initialization")
                for attr in ("media", "initialization"):
                    if not effective.get(attr):
                        continue
                    absolute = urljoin(base, expand(effective.get(attr)))
                    effective.set(attr, relay(absolute, init_url, init_range))
                node.append(effective)

        walk(root, mpd_url)
        return ET.tostring(root, encoding="unicode")

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
        extractor_key: str = None,
        stream_key: str = None,
        max_res: bool = False,
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
            if max_res:
                _keep_best_representation(
                    root, root.tag.split("}")[0] + "}" if "}" in root.tag else ""
                )
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
            if extractor_key:
                header_params += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
            if stream_key:
                header_params += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"
            if max_res:
                header_params += "&max_res=true"

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
    def _order_shared_audio_variants(lines: list) -> list:
        """Sort variants highest-bandwidth-first when an audio group is shared.

        FFmpeg assigns a shared rendition (``#EXT-X-MEDIA`` with URI) to the
        first program referencing it, while players such as mpv default to the
        highest-bandwidth variant. In the usual ascending master the audio then
        lives in program 0 while the video plays program N, and mpv hides the
        audio tracks completely (RaiPlay multiaudio). Keeping the highest
        variant first makes the shared audio belong to the program players pick
        by default, with ABR left intact.
        """
        audio_groups = set()
        variant_entries = []
        for entry in master_entries(lines):
            if entry.kind == "variant":
                if entry.url:
                    variant_entries.append(entry)
            elif entry.type == "AUDIO" and entry.url and entry.attributes.get("GROUP-ID"):
                audio_groups.add(entry.attributes["GROUP-ID"])
        if len(variant_entries) < 2 or not audio_groups:
            return lines

        variants = []
        group_uses = {}
        for entry in variant_entries:
            group = entry.attributes.get("AUDIO", "")
            if group in audio_groups:
                group_uses[group] = group_uses.get(group, 0) + 1
            variants.append(
                {
                    "inf": lines[entry.line_index],
                    "url": lines[entry.uri_index],
                    "bandwidth": entry.bandwidth,
                }
            )
        if not any(count > 1 for count in group_uses.values()):
            return lines

        order = sorted(range(len(variants)), key=lambda n: variants[n]["bandwidth"], reverse=True)
        if order == list(range(len(variants))):
            return lines

        variant_indexes = [entry.line_index for entry in variant_entries]
        position = {index: n for n, index in enumerate(variant_indexes)}
        ordered = [variants[n] for n in order]
        reordered = []
        for i, line in enumerate(lines):
            if i in position:
                item = ordered[position[i]]
                reordered.append(item["inf"])
                reordered.append(item["url"])
            elif i - 1 in position:
                continue  # URI line already emitted with its STREAM-INF
            else:
                reordered.append(line)
        logger.debug("HLS: reordered %s variants for shared audio group", len(variants))
        return reordered

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
        max_res: bool = False,
        dual_video_track: int = 0,
    ) -> str:
        """Riscrive gli URL nei manifest HLS per passare attraverso il proxy."""
        lines = ManifestRewriter._order_shared_audio_variants(manifest_content.split("\n"))
        rewritten_lines = []

        # no_bypass e mantenuto per compatibilita, ma il rewriter ora proxa sempre.
        _ = no_bypass

        # max_res=true: keep only the highest-bandwidth video variant.
        # Default: keep every variant so the player can adapt (ABR).
        generic_streams = []
        for entry in master_entries(lines):
            if entry.kind == "variant" and entry.url:
                generic_streams.append(
                    {
                        "index": entry.line_index,
                        "bandwidth": entry.bandwidth,
                        "inf": lines[entry.line_index],
                        "url": lines[entry.uri_index],
                        "entry": entry,
                    }
                )

        if generic_streams and max_res:
            highest_quality_stream = max(generic_streams, key=lambda x: x["bandwidth"])
            logger.debug(
                "Generic HLS: selected max bandwidth %s.",
                highest_quality_stream["bandwidth"],
            )
            # Warn if the CDN is serving an audio-only stream. Consider video proven either
            # by an explicit CODECS= video entry or by a RESOLUTION= attribute (many CDNs
            # omit CODECS while still serving video, so RESOLUTION is enough evidence).
            _selected_inf = highest_quality_stream["inf"]
            if not highest_quality_stream["entry"].has_video_codec:
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

            if dual_video_track:
                header_params += f"&dual_video_track={int(dual_video_track)}"
            if api_password:
                header_params += f"&api_password={api_password}"
            
            if bypass_warp:
                header_params += "&warp=off"
            if bypass_proxies:
                header_params += "&proxy=off"
            
            if disable_ssl:
                header_params += "&disable_ssl=1"
            
            if selected_proxy and not bypass_proxies:
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
            if max_res:
                header_params += "&max_res=true"

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
            
            if selected_proxy and not bypass_proxies and "&proxy=" not in proxy_variant_url:
                proxy_variant_url += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"

            proxied_media_lines = []
            is_dlstreams_or_premium = "dlstreams" in base_url.lower() or "dlhd" in base_url.lower() or "/premium" in base_url.lower()
            # Track which group-ids survive filtering (have at least one proxied media line)
            surviving_group_ids = set()
            for line in lines:
                if not line.startswith("#EXT-X-MEDIA:"):
                    continue
                original_uri = tag_uri(line)
                # Filter out unsigned/broken media tracks for DLStreams/premium streams.
                # Tracks without explicit query parameters (e.g. ?md5=...) always return 403 Forbidden.
                if original_uri and is_dlstreams_or_premium and "?" not in original_uri:
                    continue
                group_id = attributes(line).get("GROUP-ID")
                if group_id:
                    surviving_group_ids.add(group_id)
                if not original_uri:
                    # Media without URI (e.g. closed-captions=NONE): keep as-is.
                    proxied_media_lines.append(line.strip())
                    continue

                media_url = ManifestRewriter._inherit_query_if_missing(
                    urljoin(base_url, original_uri),
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
                proxied_media_lines.append(replace_uri(line, proxy_media_url))

            def _strip_empty_group_refs(inf_line: str, surviving: set) -> str:
                """Remove SUBTITLES/AUDIO/CLOSED-CAPTIONS attributes that reference
                group-ids which were completely filtered out. This prevents player
                confusion (e.g. PotPlayer refusing video) when a STREAM-INF references
                a group that has no matching EXT-X-MEDIA entries."""
                for attr, group in attributes(inf_line).items():
                    if attr not in ("SUBTITLES", "AUDIO", "CLOSED-CAPTIONS") or group in surviving:
                        continue
                    # Remove the attribute and any surrounding comma
                    inf_line = re.sub(
                        rf',?\s*{attr}="[^"]+"', "", inf_line
                    ).strip().rstrip(",")
                    logger.debug(
                        "Stripped dangling %s group ref '%s' from STREAM-INF",
                        attr, group,
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
        
        if selected_proxy and not bypass_proxies:
            header_params += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"
        if force_direct:
            header_params += "&direct=1"
        if original_channel_url:
            header_params += f"&orig_url={urllib.parse.quote(original_channel_url, safe='')}"
        if extractor_key:
            header_params += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
        if stream_key:
            header_params += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"
        if dual_video_track:
            header_params += f"&dual_video_track={int(dual_video_track)}"
        if max_res:
            header_params += "&max_res=true"

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
                original_key_url = tag_uri(line)

                if original_key_url:
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
                    if selected_proxy and not bypass_proxies:
                        proxy_key_url += f"&proxy={urllib.parse.quote(selected_proxy, safe='')}"
                    if force_direct:
                        proxy_key_url += "&direct=1"
                    if extractor_key:
                        proxy_key_url += f"&extractor_key={urllib.parse.quote(extractor_key, safe='')}"
                    if stream_key:
                        proxy_key_url += f"&stream_key={urllib.parse.quote(stream_key, safe='')}"

                    rewritten_lines.append(replace_uri(line, proxy_key_url))
                else:
                    rewritten_lines.append(line)

            # 2. GESTIONE MEDIA (Sottotitoli, Audio secondario)
            elif line.startswith("#EXT-X-MEDIA:") and 'URI=' in line:
                next_uri_is_manifest = False
                original_media_url = tag_uri(line)

                if original_media_url:
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
                    rewritten_lines.append(replace_uri(line, proxy_media_url))
                else:
                    rewritten_lines.append(line)

            # 2b. GESTIONE I-FRAME STREAMS
            elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:") and 'URI=' in line:
                next_uri_is_manifest = False
                original_iframe_url = tag_uri(line)

                if original_iframe_url:
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
                    rewritten_lines.append(replace_uri(line, proxy_iframe_url))
                else:
                    rewritten_lines.append(line)

            # 2c. GESTIONE SESSION-KEY
            elif line.startswith("#EXT-X-SESSION-KEY:") and 'URI=' in line:
                next_uri_is_manifest = False
                original_key_url = tag_uri(line)

                if original_key_url:
                    absolute_key_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_key_url),
                        base_query,
                    )
                    encoded_key_url = urllib.parse.quote(absolute_key_url, safe="")
                    
                    # Proxy KEY URL (come per #EXT-X-KEY)
                    proxy_key_url = f"{proxy_base}/key?key_url={encoded_key_url}"
                    if "&h_referer=" not in header_params.lower():
                        proxy_key_url += f"&h_Referer={urllib.parse.quote(base_url, safe='')}"
                    proxy_key_url += header_params

                    rewritten_lines.append(replace_uri(line, proxy_key_url))
                else:
                    rewritten_lines.append(line)

            # 3. GESTIONE MAP (Init Segment per fMP4)
            elif line.startswith("#EXT-X-MAP:") and 'URI=' in line:
                next_uri_is_manifest = False
                original_map_url = tag_uri(line)

                if original_map_url:
                    absolute_map_url = ManifestRewriter._inherit_query_if_missing(
                        urljoin(base_url, original_map_url),
                        base_query,
                    )
                    encoded_map_url = urllib.parse.quote(absolute_map_url, safe="")

                    # Usa endpoint segment.mp4
                    proxy_map_url = (
                        f"{proxy_base}/proxy/hls/segment.mp4?d={encoded_map_url}{header_params}"
                    )

                    rewritten_lines.append(replace_uri(line, proxy_map_url))
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

                # Variant URIs after #EXT-X-STREAM-INF are playlists even when
                # providers like VixSrc expose them as extensionless /playlist URLs.
                is_manifest_uri = next_uri_is_manifest or ".m3u8" in absolute_url
                next_uri_is_manifest = False

                if not is_manifest_uri and (extractor_key == "fctv33" or ("_ctump=" in absolute_url and "_ctuph=" in absolute_url)):
                    try:
                        from extractors.fctv33 import resolve_fctv33_segment_url
                        absolute_url = resolve_fctv33_segment_url(absolute_url)
                    except Exception as _fctv_err:
                        logger.debug("Failed to resolve FCTV33 segment in manifest: %s", _fctv_err)

                encoded_url = urllib.parse.quote(absolute_url, safe="")

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
                    path = urllib.parse.urlparse(absolute_url).path.lower()
                    ext = ".ts"
                    if path.endswith(".vtt") or path.endswith(".webvtt"):
                        ext = ".vtt"
                    elif (
                        path.endswith(".m4s")
                        or path.endswith(".mp4")
                        or path.endswith(".m4v")
                    ):
                        ext = ".mp4"
                    elif path.endswith((".aac", ".mp3", ".m4a", ".ac3", ".ec3")):
                        # Keep the real audio extension: ffmpeg's extension_picky
                        # rejects a .ts URL carrying raw AAC/MP3/AC-3 and aborts
                        # the whole HLS input (no audio, endless buffering).
                        ext = path[path.rfind("."):]

                    proxy_url = (
                        f"{proxy_base}/proxy/hls/segment{ext}?d={encoded_url}{header_params}"
                    )

                rewritten_lines.append(proxy_url)

            else:
                next_uri_is_manifest = False
                # Tutti gli altri tag (es. #EXTINF, #EXT-X-ENDLIST)
                rewritten_lines.append(line)

        return ManifestRewriter._ensure_hls_version("\n".join(rewritten_lines))
