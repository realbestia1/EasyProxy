import xml.etree.ElementTree as ET
import urllib.parse
from urllib.parse import urljoin
import logging
import os
import re
from fractions import Fraction
import math
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class MPDToHLSConverter:
    """Converte manifest MPD (DASH) in playlist HLS (m3u8) on-the-fly."""
    _timeline_sequences = {}
    _live_sequence_clocks = {}

    @staticmethod
    def _duration_seconds(value):
        match = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?', value or '')
        if not match:
            raise ValueError('Unsupported MPD duration')
        return sum(float(item or 0) * factor for item, factor in zip(match.groups(), (86400, 3600, 60, 1)))

    def _period_length_seconds(self, root, period):
        """Playlist span of one Period, derived the way DASH defines it.

        ``Period@duration`` wins when present.  Otherwise a Period ends where
        the next one starts, and the final Period ends at
        ``mediaPresentationDuration``.  A single-Period VOD manifest only ever
        declares the last of those, so consulting ``Period@duration`` alone
        reported no duration at all for those streams.
        """
        if period is not None:
            declared = period.get('duration')
            if declared:
                try:
                    return max(0.0, self._duration_seconds(declared))
                except ValueError:
                    pass

        start_sec = 0.0
        end_sec = 0.0
        periods = root.findall('.//mpd:Period', self.ns)
        if period is not None and period in periods:
            try:
                start_sec = self._duration_seconds(period.get('start', 'PT0S'))
            except ValueError:
                start_sec = 0.0
            following = periods[periods.index(period) + 1:]
            if following:
                try:
                    end_sec = self._duration_seconds(following[0].get('start', 'PT0S'))
                except ValueError:
                    end_sec = 0.0

        if end_sec <= start_sec:
            presentation = root.get('mediaPresentationDuration')
            if presentation:
                try:
                    end_sec = self._duration_seconds(presentation)
                except ValueError:
                    end_sec = 0.0

        return max(0.0, end_sec - start_sec)

    def _sequence_for_window(self, key, segments, first_timestamp):
        """Keep overlapping DASH segments at the same HLS sequence on reload."""
        previous = self._timeline_sequences.get(key, {})
        base = None
        for index, segment in enumerate(segments):
            if segment['time'] in previous:
                base = previous[segment['time']] - index
                break
        if base is None:
            # Initial numbering is arbitrary; subsequent overlapping windows
            # are numbered by segment identity, never by variable duration.
            duration = self._nominal_segment_duration_units(segments)
            base = int(segments[0]['time'] // duration)
            if previous:
                base = max(base, max(previous.values()) + 1)
        mapping = {segment['time']: base + index for index, segment in enumerate(segments)}
        self._timeline_sequences[key] = mapping
        while len(self._timeline_sequences) > 512:
            self._timeline_sequences.pop(next(iter(self._timeline_sequences)))
        return mapping[first_timestamp]

    def _sequence_for_live_window(self, key, first_timestamp_sec, common_duration_sec):
        """Map a no-startNumber DASH window to one shared HLS clock.

        Several live DASH origins omit ``startNumber`` and use independent
        audio/video timelines. Their segment timestamps can differ by a few
        milliseconds, and their durations can drift slightly. Numbering each
        representation from its own timestamp would then produce different
        HLS media sequences (or a fresh sequence for every reload). A stable
        per-playback origin plus the common nominal duration keeps alternate
        renditions aligned while allowing the window to advance one segment.
        """
        duration = max(float(common_duration_sec or 0.0), 0.001)
        clock = self._live_sequence_clocks.get(key)
        if clock is None:
            clock = {
                'origin': float(first_timestamp_sec),
                'duration': duration,
            }
            self._live_sequence_clocks[key] = clock
            while len(self._live_sequence_clocks) > 512:
                self._live_sequence_clocks.pop(next(iter(self._live_sequence_clocks)))
        else:
            # A source restart can move its media timeline backwards. Do not
            # let an old playback's origin produce negative/huge sequences.
            if first_timestamp_sec < clock['origin'] - max(clock['duration'] * 8, 60.0):
                clock['origin'] = float(first_timestamp_sec)
                clock['duration'] = duration
            elif abs(clock['duration'] - duration) > max(clock['duration'] * 0.25, 0.5):
                # Keep the original clock for normal audio/video drift, but
                # re-anchor if the source clearly changes segment cadence.
                clock['origin'] = float(first_timestamp_sec)
                clock['duration'] = duration

        sequence = round((float(first_timestamp_sec) - clock['origin']) / clock['duration'])
        return max(0, int(sequence))

    @staticmethod
    def _sequence_key(original_url, params):
        """Scope fallback sequence state to one playback, not one origin URL."""
        stream_key = next(
            (
                urllib.parse.unquote(item.split("=", 1)[1])
                for item in (params or "").split("&")
                if item.startswith("stream_key=")
            ),
            "",
        )
        return f"{original_url.split('?', 1)[0]}|{stream_key}"
    
    def __init__(self):
        self.ns = {
            'mpd': 'urn:mpeg:dash:schema:mpd:2011',
            'cenc': 'urn:mpeg:cenc:2013'
        }

    @staticmethod
    def _expand_segment_template(template: str, rep_id: str, bandwidth: str, number: int = None, timestamp: int = None) -> str:
        """Expand DASH template identifiers, including zero-padded $Number%05d$."""
        value = template.replace('$RepresentationID$', str(rep_id))
        value = value.replace('$Bandwidth$', str(bandwidth))
        if number is not None:
            value = re.sub(
                r'\$Number%0(\d+)d\$',
                lambda match: str(number).zfill(int(match.group(1))),
                value,
            )
            value = value.replace('$Number$', str(number))
        if timestamp is not None:
            value = value.replace('$Time$', str(timestamp))
        return value

    @staticmethod
    def _hls_codec(codec: str) -> str:
        """Normalize DASH codec names for HLS/fMP4 player selection."""
        if not codec:
            return codec
        # HLS clients (notably VLC) commonly advertise HEVC as hvc1,
        # while DASH manifests use the equivalent hev1 sample entry.
        return re.sub(r"^hev1(?=\.|$)", "hvc1", codec)

    @staticmethod
    def _nominal_segment_duration_units(segments):
        """Return a stable duration for live MEDIA-SEQUENCE calculations.

        Live DASH audio timelines can alternate a few milliseconds around the
        nominal duration.  Using the first segment's duration makes the HLS
        sequence jump when the rolling window starts on a different sample.
        The median is stable across those small variations.
        """
        durations = sorted(
            int(segment.get("d", 0))
            for segment in segments
            if int(segment.get("d", 0)) > 0
        )
        if not durations:
            return 1
        return max(1, durations[len(durations) // 2])
    
    def _extract_header_params(self, params: str) -> str:
        """Estrae solo i parametri necessari dalla query string originale.
        
        Estrae:
        - h_* (headers personalizzati)
        - api_password (autenticazione)
        - clearkey (chiavi DRM)
        
        Questo evita di passare parametri di controllo duplicati (d=, rep_id=, format=, etc.)
        che possono causare problemi di parsing degli URL.
        """
        if not params:
            return ""
        
        header_params = []
        for param in params.split('&'):
            if (
                param.startswith('h_')
                or param.startswith('api_password=')
                or param.startswith('drm_token=')
                or param.startswith('clearkey=')
                or param.startswith('ext=')
                or param.startswith('warp=')
                or param.startswith('proxy=')
                or param.startswith('extractor_key=')
                or param.startswith('stream_key=')
                or param.startswith('orig_url=')
                or param.startswith('direct=')
                or param.startswith('disable_ssl=')
                or param.startswith('max_res=')
            ):
                header_params.append(param)
        
        if header_params:
            return '&' + '&'.join(header_params)
        return ""

    def convert_master_playlist(self, manifest_content: str, proxy_base: str, original_url: str, params: str) -> str:
        """Genera la Master Playlist HLS dagli AdaptationSet del MPD."""
        try:
            if 'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace('<MPD', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
            
            root = ET.fromstring(manifest_content)
            lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            
            # Trova AdaptationSet Video e Audio
            video_sets = []
            audio_sets = []
            
            for adaptation_set in root.findall('.//mpd:AdaptationSet', self.ns):
                mime_type = adaptation_set.get('mimeType', '')
                content_type = adaptation_set.get('contentType', '')
                
                if 'video' in mime_type or 'video' in content_type:
                    video_sets.append(adaptation_set)
                elif 'audio' in mime_type or 'audio' in content_type:
                    audio_sets.append(adaptation_set)
            
            # Fallback per detection a livello Representation.
            # Va eseguito indipendentemente per video e audio: un MPD può
            # dichiarare l'audio su AdaptationSet e il video solo sui figli.
            def representation_matches(rep, kind):
                rep_type = ' '.join(
                    (
                        rep.get('mimeType', ''),
                        rep.get('contentType', ''),
                    )
                ).lower()
                if kind in rep_type:
                    return True

                codecs = rep.get('codecs', '').lower()
                if kind == 'video':
                    return bool(
                        rep.get('width')
                        or rep.get('height')
                        or any(codec in codecs for codec in (
                            'avc', 'hev', 'hvc', 'vp8', 'vp9', 'av01'
                        ))
                    )
                return any(codec in codecs for codec in (
                    'mp4a', 'aac', 'ac-3', 'ec-3', 'opus', 'vorbis'
                ))

            for adaptation_set in root.findall('.//mpd:AdaptationSet', self.ns):
                representations = adaptation_set.findall('mpd:Representation', self.ns)
                if (
                    adaptation_set not in video_sets
                    and any(representation_matches(rep, 'video') for rep in representations)
                ):
                    video_sets.append(adaptation_set)
                if (
                    adaptation_set not in audio_sets
                    and any(representation_matches(rep, 'audio') for rep in representations)
                ):
                    audio_sets.append(adaptation_set)

            logger.debug(
                "MPD master tracks detected: video=%d audio=%d",
                len(video_sets),
                len(audio_sets),
            )

            # --- GESTIONE AUDIO (EXT-X-MEDIA) ---
            audio_group_id = 'audio'
            has_audio = False
            
            # Raccogli e ordina le rappresentazioni audio per dare priorità a AAC (mp4a) rispetto a Dolby Digital Plus (ec3)
            audio_reps = []
            for adaptation_set in audio_sets:
                for representation in adaptation_set.findall('mpd:Representation', self.ns):
                    audio_reps.append((adaptation_set, representation))
            
            def sort_audio_func(item):
                adaptation = item[0]
                rep = item[1]
                rep_id = rep.get('id', '').lower()
                codecs = (rep.get('codecs') or adaptation.get('codecs', '')).lower()
                if 'mp4a' in rep_id or 'aac' in rep_id or 'mp4a' in codecs or 'aac' in codecs:
                    return 0
                return 1
                
            audio_reps.sort(key=sort_audio_func)

            audio_codecs_list = []
            for adaptation_set, representation in audio_reps:
                acodec = self._hls_codec(representation.get('codecs') or adaptation_set.get('codecs'))
                if acodec and acodec not in audio_codecs_list:
                    audio_codecs_list.append(acodec)

            for adaptation_set, representation in audio_reps:
                rep_id = representation.get('id')
                bandwidth = representation.get('bandwidth', '128000') # Default fallback
                
                # Costruisci URL Media Playlist Audio
                encoded_url = urllib.parse.quote(original_url, safe='')
                encoded_rep_id = urllib.parse.quote(str(rep_id or ''), safe='')
                header_params = self._extract_header_params(params)
                media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={encoded_rep_id}{header_params}"
                
                # Usa GROUP-ID 'audio' e NAME basato su ID o lingua
                lang = adaptation_set.get('lang', 'und')
                name = f"Audio {lang} ({bandwidth})"
                
                # EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="...",DEFAULT=YES,AUTOSELECT=YES,URI="..."
                # Impostiamo DEFAULT=YES solo per il primo (che ora sarà AAC se disponibile)
                default_attr = "YES" if not has_audio else "NO"
                
                media_line = f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{audio_group_id}",NAME="{name}",LANGUAGE="{lang}",DEFAULT={default_attr},AUTOSELECT=YES,URI="{media_url}"'
                lines.append(media_line)
                has_audio = True

            if has_audio or "format=hls" in params:
                lines[1] = '#EXT-X-VERSION:6'

            # --- GESTIONE VIDEO (EXT-X-STREAM-INF) ---
            # Default: mantieni tutte le rappresentazioni (ABR, come prima).
            # Con max_res=true serviamo solo la variante col bandwidth più alto,
            # coerente con il rewriter HLS quando richiede la qualità massima.
            max_res = "max_res=true" in (params or "")
            video_candidates = []
            for adaptation_set in video_sets:
                for representation in adaptation_set.findall('mpd:Representation', self.ns):
                    rep_id = representation.get('id', '')
                    if 'iframe' in rep_id.lower() or 'i-frame' in rep_id.lower():
                        continue
                    video_candidates.append((adaptation_set, representation))

            def _video_bandwidth(candidate):
                try:
                    return int(candidate[1].get('bandwidth') or 0)
                except (TypeError, ValueError):
                    return 0

            if max_res and video_candidates:
                video_candidates = [max(video_candidates, key=_video_bandwidth)]

            for adaptation_set, representation in video_candidates:
                    rep_id = representation.get('id')
                    bandwidth = representation.get('bandwidth')
                    width = representation.get('width')
                    height = representation.get('height')
                    frame_rate = representation.get('frameRate') or adaptation_set.get('frameRate')
                    codecs = self._hls_codec(representation.get('codecs') or adaptation_set.get('codecs'))
                    
                    encoded_url = urllib.parse.quote(original_url, safe='')
                    encoded_rep_id = urllib.parse.quote(str(rep_id or ''), safe='')
                    header_params = self._extract_header_params(params)
                    media_url = f"{proxy_base}/proxy/hls/manifest.m3u8?d={encoded_url}&format=hls&rep_id={encoded_rep_id}{header_params}"
                    
                    # Determine codecs (must combine video and audio codecs for HLS spec compliance)
                    combined_codecs = []
                    if codecs:
                        combined_codecs.append(codecs)
                    if has_audio:
                        combined_codecs.extend(audio_codecs_list)

                    audio_bandwidth = max((int(rep.get('bandwidth', '0')) for _, rep in audio_reps), default=0)
                    inf = f'#EXT-X-STREAM-INF:BANDWIDTH={int(bandwidth) + audio_bandwidth}'
                    if width and height:
                        inf += f',RESOLUTION={width}x{height}'
                    if frame_rate:
                        inf += f',FRAME-RATE={float(Fraction(frame_rate)):.3f}'
                    if combined_codecs:
                        inf += f',CODECS="{",".join(combined_codecs)}"'
                    
                    # Collega il gruppo audio se presente
                    if has_audio:
                        inf += f',AUDIO="{audio_group_id}"'
                    
                    lines.append(inf)
                    lines.append(media_url)
            
            return '\n'.join(lines)
        except Exception as e:
            logging.error(f"Error converting Master Playlist: {e}")
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)

    def _convert_segment_list_playlist(
        self,
        root,
        representation,
        adaptation_set,
        segment_list,
        proxy_base,
        original_url,
        params,
        clearkey_param,
        server_side_decryption,
        decryption_params,
        media_type_param,
        ext_param,
        is_live,
    ):
        """Convert explicit DASH byte ranges to HLS relay URLs.

        SegmentBase is expanded to SegmentList by the request layer.  Each
        SegmentURL keeps its mediaRange; the relay then fetches/decrypts that
        exact range instead of pretending it is a normal whole-file segment.
        """
        parents = {child: parent for parent in root.iter() for child in parent}
        ancestry = []
        node = representation
        while node is not None:
            ancestry.append(node)
            node = parents.get(node)
        base_url = original_url
        for ancestor in reversed(ancestry):
            base = ancestor.find('mpd:BaseURL', self.ns)
            if base is not None and base.text:
                base_url = urljoin(base_url, base.text.strip())

        timescale = int(segment_list.get('timescale', '1'))
        if timescale <= 0:
            raise ValueError('Invalid SegmentList timescale')
        segment_urls = segment_list.findall('mpd:SegmentURL', self.ns)
        if not segment_urls:
            raise ValueError('SegmentList contains no SegmentURL entries')

        durations = []
        timeline = segment_list.find('mpd:SegmentTimeline', self.ns)
        if timeline is not None:
            current_time = 0
            for entry in timeline.findall('mpd:S', self.ns):
                if entry.get('t') is not None:
                    current_time = int(entry.get('t'))
                duration = int(entry.get('d', '0'))
                repeat = int(entry.get('r', '0'))
                if duration <= 0 or repeat < 0:
                    raise ValueError('Unsupported SegmentList timeline')
                durations.extend([duration / timescale] * (repeat + 1))
                current_time += duration * (repeat + 1)

        fallback_duration = float(segment_list.get('duration', '0') or 0) / timescale
        if fallback_duration <= 0:
            fallback_duration = 1.0
        if len(durations) < len(segment_urls):
            durations.extend([fallback_duration] * (len(segment_urls) - len(durations)))
        durations = durations[:len(segment_urls)]

        init = segment_list.find('mpd:Initialization', self.ns)
        init_url = None
        init_range = None
        if init is not None:
            init_url = urljoin(base_url, init.get('sourceURL', '') or '')
            init_range = init.get('range')
        if server_side_decryption and not init_url:
            raise ValueError('ClearKey SegmentList requires initialization metadata')

        header_params = self._extract_header_params(params)
        lines = ['#EXTM3U', '#EXT-X-VERSION:6']
        if not is_live:
            lines.append('#EXT-X-PLAYLIST-TYPE:VOD')
        lines.append(f'#EXT-X-TARGETDURATION:{max(1, int(max(durations)) + 1)}')
        lines.append('#EXT-X-MEDIA-SEQUENCE:0')

        if init_url:
            encoded_init = urllib.parse.quote(init_url, safe='')
            if server_side_decryption:
                init_uri = (
                    f'{proxy_base}/decrypt/segment.mp4?url={encoded_init}&is_init=1'
                    f'{("&init_range=" + urllib.parse.quote(init_range, safe="")) if init_range else ""}'
                    f'{decryption_params}{media_type_param}{header_params}'
                )
            else:
                init_uri = (
                    f'{proxy_base}/segment/init.mp4?base_url={encoded_init}'
                    f'{("&range=" + urllib.parse.quote(init_range, safe="")) if init_range else ""}'
                    f'{media_type_param}{header_params}'
                )
            lines.append(f'#EXT-X-MAP:URI="{init_uri}"')

        for index, segment in enumerate(segment_urls):
            duration = durations[index]
            media_url = urljoin(base_url, segment.get('media', '') or '')
            media_range = segment.get('mediaRange') or segment.get('range')
            encoded_media = urllib.parse.quote(media_url, safe='')
            if server_side_decryption:
                if not init_url:
                    raise ValueError('ClearKey SegmentList media has no initialization URL')
                encoded_init = urllib.parse.quote(init_url, safe='')
                seg_url = (
                    f'{proxy_base}/decrypt/segment.mp4?url={encoded_media}'
                    f'&init_url={encoded_init}&skip_init=1'
                    f'{("&media_range=" + urllib.parse.quote(media_range, safe="")) if media_range else ""}'
                    f'{("&init_range=" + urllib.parse.quote(init_range, safe="")) if init_range else ""}'
                    f'{decryption_params}{media_type_param}{header_params}'
                )
            else:
                name = os.path.basename(urllib.parse.urlsplit(media_url).path) or f'segment_{index}.mp4'
                seg_url = (
                    f'{proxy_base}/segment/{urllib.parse.quote(name, safe="")}'
                    f'?base_url={encoded_media}'
                    f'{("&range=" + urllib.parse.quote(media_range, safe="")) if media_range else ""}'
                    f'{media_type_param}{header_params}'
                )
            lines.append(f'#EXTINF:{duration:.3f},')
            lines.append(seg_url)

        if not is_live:
            lines.append('#EXT-X-ENDLIST')
        return '\n'.join(lines)

    def convert_media_playlist(self, manifest_content: str, rep_id: str, proxy_base: str, original_url: str, params: str, clearkey_param: str = None) -> str:
        """Genera la Media Playlist HLS per una specifica Representation."""
        try:
            if 'xmlns' not in manifest_content:
                manifest_content = manifest_content.replace('<MPD', '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', 1)
                
            root = ET.fromstring(manifest_content)
            
            # --- RILEVAMENTO LIVE vs VOD ---
            mpd_type = root.get('type', 'static')
            is_live = mpd_type.lower() == 'dynamic'
            
            # Trova la Representation specifica
            representation = None
            adaptation_set = None
            
            # Cerca in tutti gli AdaptationSet
            for aset in root.findall('.//mpd:AdaptationSet', self.ns):
                rep = aset.find(f'mpd:Representation[@id="{rep_id}"]', self.ns)
                if rep is not None:
                    representation = rep
                    adaptation_set = aset
                    break
            
            if representation is None:
                logger.error(f"❌ Representation {rep_id} not found in manifest.")
                return "#EXTM3U\n#EXT-X-ERROR: Representation not found"

            # Keep the track kind on generated segment requests.  DASH commonly
            # uses `.m4s` for both audio and video; iOS uses the response MIME to
            # attach the fMP4 stream to the correct rendition.
            adaptation_kind = (
                adaptation_set.get('contentType', '')
                or adaptation_set.get('mimeType', '')
                or representation.get('mimeType', '')
            ).lower()
            media_type_param = '&media_type=audio' if 'audio' in adaptation_kind else ''

            # fMP4 richiede HLS versione 6 o 7, ma per .ts output usiamo v3 per compatibilità
            # Per LIVE: non usare VOD e non aggiungere ENDLIST
            if is_live:
                lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-INDEPENDENT-SEGMENTS']
            else:
                # Target duration is emitted once below from the actual segment
                # durations.  A duplicate tag makes some HLS.js versions treat
                # this VOD playlist as invalid/live and reload it forever.
                lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-PLAYLIST-TYPE:VOD']
            
            # --- GESTIONE DRM (ClearKey) ---
            # Decrittazione lato server con mp4decrypt
            server_side_decryption = False
            decryption_params = ""
            
            if clearkey_param:
                try:
                    # Supporta formato multi-key: "KID1:KEY1,KID2:KEY2"
                    # O "KID:KEY" (legacy simple)
                    kids = []
                    keys = []
                    
                    # Split by comma first to handle multiple pairs
                    pairs = clearkey_param.split(',')
                    for pair in pairs:
                        if ':' in pair:
                            k_id, k_val = pair.split(':')
                            kids.append(k_id.strip())
                            keys.append(k_val.strip())
                    
                    if not kids or not keys:
                        raise ValueError(f"Invalid clearkey format: {clearkey_param}")
                        
                    kid_hex = ",".join(kids)
                    key_hex = ",".join(keys)
                    
                    # Rileva chiave nulla (placeholder) - se TUTTE le chiavi sono tutti zeri
                    is_null_key = all(k.replace('0', '') == '' for k in kids + keys)
                    
                    if is_null_key:
                        # Chiave nulla: usa comunque l'endpoint decrypt per il remux a TS
                        # ma aggiungi flag per saltare la decrittazione vera e propria
                        logger.debug("🔓 Null key detected - using remux endpoint without decryption")
                        server_side_decryption = True
                        drm_token = next(
                            (
                                item.split("=", 1)[1]
                                for item in params.split("&")
                                if item.startswith("drm_token=")
                            ),
                            "",
                        )
                        decryption_params = (
                            f"&drm_token={drm_token}&skip_decrypt=1"
                            if drm_token
                            else f"&key={key_hex}&key_id={kid_hex}&skip_decrypt=1"
                        )
                    else:
                        server_side_decryption = True
                        drm_token = next(
                            (
                                item.split("=", 1)[1]
                                for item in params.split("&")
                                if item.startswith("drm_token=")
                            ),
                            "",
                        )
                        decryption_params = (
                            f"&drm_token={drm_token}"
                            if drm_token
                            else f"&key={key_hex}&key_id={kid_hex}"
                        )
                        key_count = len(kids)
                        logger.debug(f"🔐 ClearKey enabled - {key_count} key pair(s) for server-side decryption")
                except Exception as e:
                    logger.error(f"Error parsing clearkey_param: {e}")

            # --- Check for forced extension ---
            ext_param = "mp4" # Default to MP4/fMP4 since remuxing is disabled
            if "ext=ts" in params:
                 ext_param = "ts"
            
            if ext_param == "ts" and not server_side_decryption:
                 logger.debug("🔄 Concatenation requested (ext=ts)")
                 server_side_decryption = True
                 # Use dummy key/id to satisfy the endpoint requirement, and set skip_decrypt=1
                 decryption_params = "&key=00000000000000000000000000000000&key_id=00000000000000000000000000000000&skip_decrypt=1"

            # --- GESTIONE SEGMENTI ---
            # SegmentTemplate è il caso più comune per lo streaming live/vod moderno
            segment_template = representation.find('mpd:SegmentTemplate', self.ns)
            if segment_template is None:
                # Fallback: cerca nell'AdaptationSet
                segment_template = adaptation_set.find('mpd:SegmentTemplate', self.ns)

            segment_list = None
            if segment_template is None:
                segment_list = representation.find('mpd:SegmentList', self.ns)
                if segment_list is None:
                    segment_list = adaptation_set.find('mpd:SegmentList', self.ns)

            if segment_list is not None:
                return self._convert_segment_list_playlist(
                    root, representation, adaptation_set, segment_list,
                    proxy_base, original_url, params, clearkey_param,
                    server_side_decryption, decryption_params,
                    media_type_param, ext_param, is_live,
                )
            
            if segment_template is not None:
                timescale = int(segment_template.get('timescale', '1'))
                presentation_time_offset = int(segment_template.get('presentationTimeOffset', '0'))
                initialization = segment_template.get('initialization')
                media = segment_template.get('media')
                start_number = int(segment_template.get('startNumber', '1'))
                has_explicit_start_number = segment_template.get('startNumber') is not None
                
                # Risolvi URL base
                parents = {child: parent for parent in root.iter() for child in parent}
                ancestry = []
                node = representation
                while node is not None:
                    ancestry.append(node)
                    node = parents.get(node)
                base_url = original_url
                for node in reversed(ancestry):
                    base = node.find('mpd:BaseURL', self.ns)
                    if base is not None and base.text:
                        base_url = urljoin(base_url, base.text.strip())

                # --- INITIALIZATION SEGMENT (EXT-X-MAP) ---
                encoded_init_url = ""
                # Get bandwidth from representation
                bandwidth = representation.get('bandwidth', '')
                
                if initialization:
                    # Processing initialization segment
                    init_url = self._expand_segment_template(
                        initialization, rep_id, bandwidth
                    )
                    full_init_url = urljoin(base_url, init_url)
                    encoded_init_url = urllib.parse.quote(full_init_url, safe='')
                    
                    header_params = self._extract_header_params(params)
                    if server_side_decryption:
                        proxy_init_url = f"{proxy_base}/decrypt/segment.{ext_param}?url={encoded_init_url}&is_init=1{decryption_params}{media_type_param}{header_params}"
                    else:
                        proxy_init_url = f"{proxy_base}/segment/init.mp4?base_url={encoded_init_url}{media_type_param}{header_params}"
                    lines.append(f'#EXT-X-MAP:URI="{proxy_init_url}"')
                    lines[1] = '#EXT-X-VERSION:6'

                # --- SEGMENT TIMELINE ---
                segment_timeline = segment_template.find('mpd:SegmentTimeline', self.ns)
                if segment_timeline is not None:
                    # Prima raccogli tutti i segmenti
                    all_segments = []
                    current_time = 0
                    segment_number = start_number
                    
                    timeline_entries = segment_timeline.findall('mpd:S', self.ns)
                    for entry_index, s in enumerate(timeline_entries):
                        t = s.get('t')
                        if t: current_time = int(t)
                        d = int(s.get('d'))
                        r = int(s.get('r', '0'))
                        if d <= 0:
                            raise ValueError('MPD segment duration must be positive')
                        if r < 0:
                            next_time = next((int(item.get('t')) for item in timeline_entries[entry_index + 1:] if item.get('t') is not None), None)
                            if next_time is None:
                                period = next((p for p in root.findall('mpd:Period', self.ns) if adaptation_set in list(p)), None)
                                period_start = self._duration_seconds(period.get('start', 'PT0S')) if period is not None else 0.0
                                if period is not None and period.get('duration'):
                                    end_seconds = self._duration_seconds(period.get('duration'))
                                elif root.get('mediaPresentationDuration'):
                                    end_seconds = self._duration_seconds(root.get('mediaPresentationDuration')) - period_start
                                elif is_live and root.get('availabilityStartTime'):
                                    start = datetime.fromisoformat(root.get('availabilityStartTime').replace('Z', '+00:00'))
                                    published = root.get('publishTime')
                                    end = datetime.fromisoformat(published.replace('Z', '+00:00')) if published else datetime.now(timezone.utc)
                                    end_seconds = (end - start).total_seconds() - period_start
                                else:
                                    raise ValueError('Cannot bound negative MPD repeat')
                                next_time = end_seconds * timescale + presentation_time_offset
                            r = max(0, math.ceil((next_time - current_time) / d)) - 1
                        
                        duration_sec = d / timescale
                        
                        # Ripeti per r + 1 volte
                        for _ in range(r + 1):
                            all_segments.append({
                                'time': current_time,
                                'number': segment_number,
                                'duration': duration_sec,
                                'd': d
                            })
                            current_time += d
                            segment_number += 1
                    
                    # Per LIVE: FILTRA solo gli ultimi N segmenti per forzare partenza dal live edge
                    # Questo è necessario perché molti player (Stremio, ExoPlayer) ignorano EXT-X-START
                    # Per VOD: prendi tutti normalmente
                    segments_to_use = all_segments
                    
                    if is_live and len(all_segments) > 0:
                        # Keep the live-window calculation identical to the
                        # known-good legacy converter.  Each MPD refresh is
                        # evaluated independently; no snapshot or delay cache
                        # is shared between audio/video child playlists.
                        global_last_time_sec = 0.0
                        global_first_time_sec = 0.0
                        duration_samples_sec = []
                        for period in root.findall('.//mpd:Period', self.ns):
                            for aset in period.findall('mpd:AdaptationSet', self.ns):
                                mime = aset.get('mimeType', '')
                                if not mime:
                                    rep = aset.find('mpd:Representation', self.ns)
                                    if rep is not None:
                                        mime = rep.get('mimeType', '')
                                if 'video' in mime or 'audio' in mime:
                                    template = aset.find('mpd:SegmentTemplate', self.ns)
                                    for r in aset.findall('mpd:Representation', self.ns):
                                        r_template = r.find('mpd:SegmentTemplate', self.ns) or template
                                        if r_template is not None:
                                            r_timescale = int(r_template.get('timescale', '1'))
                                            timeline = r_template.find('mpd:SegmentTimeline', self.ns)
                                            if timeline is not None:
                                                first_t = None
                                                last_t = None
                                                last_d = 0
                                                for s in timeline.findall('mpd:S', self.ns):
                                                    t = s.get('t')
                                                    if t:
                                                        temp_t = int(t)
                                                        if first_t is None:
                                                            first_t = temp_t
                                                        last_t = temp_t
                                                    d = int(s.get('d'))
                                                    r_rep = int(s.get('r', '0'))
                                                    if d > 0:
                                                        duration_samples_sec.append(d / r_timescale)
                                                    if last_t is not None:
                                                        last_t += d * r_rep
                                                        last_d = d
                                                if first_t is not None:
                                                    first_seg_time_sec = first_t / r_timescale
                                                    if first_seg_time_sec > global_first_time_sec:
                                                        global_first_time_sec = first_seg_time_sec
                                                if last_t is not None:
                                                    last_seg_time_sec = (last_t + last_d) / r_timescale
                                                    if last_seg_time_sec > global_last_time_sec:
                                                        global_last_time_sec = last_seg_time_sec

                        if global_last_time_sec == 0.0:
                            global_last_time_sec = all_segments[-1]['time'] / timescale
                        if global_first_time_sec == 0.0:
                            global_first_time_sec = all_segments[0]['time'] / timescale

                        if duration_samples_sec:
                            duration_samples_sec.sort()
                            common_duration_sec = duration_samples_sec[len(duration_samples_sec) // 2]
                        else:
                            common_duration_sec = max(seg['duration'] for seg in all_segments)

                        window_start_sec = max(global_last_time_sec - 30.0, global_first_time_sec)
                        segments_to_use = [
                            seg for seg in all_segments
                            if seg['time'] / timescale >= window_start_sec - 1.0
                        ]
                        if not segments_to_use:
                            segments_to_use = [all_segments[-1]]

                        max_duration = max(seg['duration'] for seg in segments_to_use)
                        if segments_to_use:
                            if has_explicit_start_number:
                                # DASH already gives us the authoritative
                                # sequence.  Deriving HLS sequence from media
                                # timestamps is wrong for 3.84s/variable live
                                # segments and makes one reload look like two.
                                media_sequence = int(segments_to_use[0]['number'])
                            else:
                                # Some MPDs omit startNumber and expose audio
                                # and video timelines with slightly different
                                # timestamps/durations. Use one shared clock;
                                # per-representation timestamp numbering makes
                                # iOS alternate playlists drift apart.
                                media_sequence = self._sequence_for_live_window(
                                    self._sequence_key(original_url, params),
                                    segments_to_use[0]['time'] / timescale,
                                    common_duration_sec,
                                )
                            logger.debug(
                                f"📐 [Window] rep={rep_id} edge={global_last_time_sec:.1f} "
                                f"first={global_first_time_sec:.1f} win={window_start_sec:.1f} "
                                f"segs={len(segments_to_use)} "
                                f"start_ts={segments_to_use[0]['time']/timescale:.1f} "
                                f"seq={media_sequence}"
                            )
                            lines.append(f'#EXT-X-TARGETDURATION:{int(max_duration) + 1}')
                            lines.append(f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}')
                    else:
                        # VOD: inizia da 0
                        # logger.info(f"🔵 VOD Mode: {len(segments_to_use)} segments")
                        if segments_to_use:
                            max_duration = max(seg['duration'] for seg in segments_to_use)
                            target_dur = int(max_duration) + 1
                        else:
                            target_dur = 10
                            
                        lines.append(f'#EXT-X-TARGETDURATION:{target_dur}')
                        lines.append('#EXT-X-MEDIA-SEQUENCE:0')
                    
                    for seg in segments_to_use:
                        # Costruisci URL segmento
                        seg_name = self._expand_segment_template(
                            media,
                            rep_id,
                            bandwidth,
                            number=seg['number'],
                            timestamp=seg['time'],
                        )
                        
                        full_seg_url = urljoin(base_url, seg_name)
                        encoded_seg_url = urllib.parse.quote(full_seg_url, safe='')
                        
                        # Estrai solo il nome del file (senza query string) per il path del proxy
                        # Questo evita URL con doppio ? (es: /segment/file.mp4?z32=...?base_url=...)
                        seg_filename = seg_name.split('?')[0] if '?' in seg_name else seg_name
                        
                        lines.append(f'#EXTINF:{seg["duration"]:.3f},')
                        
                        # Estrai solo i parametri header dalla query string originale
                        header_params = self._extract_header_params(params)
                        
                        if server_side_decryption:
                            decrypt_url = f"{proxy_base}/decrypt/segment.{ext_param}?url={encoded_seg_url}&init_url={encoded_init_url}&skip_init=1{decryption_params}{media_type_param}{header_params}"
                            lines.append(decrypt_url)
                        else:
                            proxy_seg_url = f"{proxy_base}/segment/{seg_filename}?base_url={encoded_seg_url}{media_type_param}{header_params}"
                            lines.append(proxy_seg_url)
                
                # --- SEGMENT TEMPLATE (DURATION) ---
                else:
                    duration = int(segment_template.get('duration', '0'))
                    duration_sec = duration / timescale if duration > 0 else 0.0
                    if duration_sec <= 0:
                        raise ValueError('SegmentTemplate needs SegmentTimeline or @duration')
                    if not media:
                        raise ValueError('SegmentTemplate is missing @media')

                    # The representation can live in any Period, not only the
                    # first one, so resolve the Period that actually contains it.
                    period = next(
                        (candidate for candidate in root.findall('.//mpd:Period', self.ns)
                         if adaptation_set in list(candidate)),
                        None,
                    )
                    if period is None:
                        period = root.find('mpd:Period', self.ns)

                    if is_live:
                        # A dynamic MPD with @duration carries no SegmentTimeline,
                        # so nothing in it lists which segments still exist.  The
                        # origin numbers segments continuously from
                        # availabilityStartTime and the CDN keeps only the tail of
                        # that range: emitting a fixed 1..N list hands the player
                        # URLs that expired long ago, and every request 404s.
                        availability_start_raw = root.get('availabilityStartTime')
                        if not availability_start_raw:
                            raise ValueError('Live SegmentTemplate requires availabilityStartTime')

                        try:
                            availability_start = datetime.fromisoformat(
                                availability_start_raw.replace('Z', '+00:00')
                            )
                        except ValueError:
                            raise ValueError(f'Invalid availabilityStartTime: {availability_start_raw}')

                        published_raw = root.get('publishTime')
                        if published_raw:
                            try:
                                reference_time = datetime.fromisoformat(published_raw.replace('Z', '+00:00'))
                            except ValueError:
                                reference_time = datetime.now(timezone.utc)
                        else:
                            # No publishTime: the local clock is the only other
                            # sanctioned anchor for a live window.
                            reference_time = datetime.now(timezone.utc)

                        period_start_sec = (
                            self._duration_seconds(period.get('start', 'PT0S'))
                            if period is not None else 0.0
                        )
                        elapsed_sec = max(
                            0.0,
                            (reference_time - availability_start).total_seconds() - period_start_sec,
                        )
                        live_edge_number = start_number + int(elapsed_sec / duration_sec)

                        # Serve the declared timeshift buffer, floored at a few
                        # segments and capped so a playlist never grows unbounded.
                        window_sec = duration_sec * 5
                        time_shift_raw = root.get('timeShiftBufferDepth')
                        if time_shift_raw:
                            try:
                                declared_window = self._duration_seconds(time_shift_raw)
                            except ValueError:
                                declared_window = 0.0
                            if declared_window > 0:
                                window_sec = declared_window
                        # Cap the playlist, but always keep a few segments so the
                        # player has something to start from even when a single
                        # segment is longer than the declared timeshift buffer.
                        window_sec = min(window_sec, 60.0)
                        window_count = max(3, int(math.ceil(window_sec / duration_sec)))
                        first_number = max(start_number, live_edge_number - window_count + 1)
                        segment_numbers = list(range(first_number, live_edge_number + 1))

                        if has_explicit_start_number:
                            # DASH already supplies the authoritative numbering,
                            # so the sequence moves exactly one per segment.
                            media_sequence = first_number
                        else:
                            media_sequence = self._sequence_for_live_window(
                                self._sequence_key(original_url, params),
                                (first_number - start_number) * duration_sec,
                                duration_sec,
                            )
                        logger.debug(
                            f"📐 [Duration window] rep={rep_id} edge={live_edge_number} "
                            f"first={first_number} segs={len(segment_numbers)} "
                            f"seq={media_sequence}"
                        )
                    else:
                        # VOD: the list has to span the whole asset.  A
                        # single-Period manifest declares that only as
                        # mediaPresentationDuration on the MPD root, so reading
                        # Period@duration alone found nothing and fell back to a
                        # fixed 100 segments: a 634s film at 4s/segment needs 159,
                        # so players reported 400s and the final third of the
                        # asset was never listed.
                        period_length_sec = self._period_length_seconds(root, period)
                        total_segments = (
                            max(1, int(math.ceil(period_length_sec / duration_sec)))
                            if period_length_sec > 0 else 100
                        )
                        segment_numbers = [start_number + i for i in range(total_segments)]
                        media_sequence = 0

                    lines.append(f'#EXT-X-TARGETDURATION:{int(duration_sec) + 1}')
                    lines.append(f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}')

                    for seg_num in segment_numbers:
                        # $Time$ carries the segment's presentation time in
                        # timescale units, not its number, so derive it from the
                        # template's own duration and offset.
                        seg_name = self._expand_segment_template(
                            media,
                            rep_id,
                            bandwidth,
                            number=seg_num,
                            timestamp=presentation_time_offset + (seg_num - start_number) * duration,
                        )

                        full_seg_url = urljoin(base_url, seg_name)
                        encoded_seg_url = urllib.parse.quote(full_seg_url, safe='')
                        header_params = self._extract_header_params(params)
                        orig_ext = os.path.splitext(seg_name.split('?')[0])[1] or '.m4s'
                        if server_side_decryption:
                            decrypt_url = f"{proxy_base}/decrypt/segment.{ext_param}?url={encoded_seg_url}&init_url={encoded_init_url}&skip_init=1{decryption_params}{media_type_param}{header_params}"
                            seg_url = decrypt_url
                        else:
                            seg_url = f"{proxy_base}/segment/seg_{seg_num}{orig_ext}?base_url={encoded_seg_url}{media_type_param}{header_params}"

                        lines.append(f'#EXTINF:{duration_sec:.6f},')
                        lines.append(seg_url)

            # Per VOD aggiungi ENDLIST, per LIVE no (indica stream in corso)
            if not is_live:
                lines.append('#EXT-X-ENDLIST')
            
            # Unisci le righe
            playlist_content = '\n'.join(lines)
            # logger.info(f"📜 Generated playlist for rep_id={rep_id} (first 15 lines):\n{chr(10).join(lines[:15])}")
            # logger.info(f"📊 Total lines: {len(lines)}, Total segments: {len([l for l in lines if l.startswith('#EXTINF')])}")
            
            return playlist_content

        except Exception as e:
            logging.error(f"Error converting Media Playlist: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return "#EXTM3U\n#EXT-X-ERROR: " + str(e)
