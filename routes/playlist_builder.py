import asyncio
import base64
import json
import logging
import urllib.parse
from aiohttp import ClientSession, ClientTimeout
from typing import Iterator, List, Dict

logger = logging.getLogger(__name__)


class PlaylistBuilder:
    """Builder per playlist M3U con supporto per multiple sorgenti"""

    def __init__(self):
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    def rewrite_m3u_links_streaming(self, m3u_lines_iterator: Iterator[str], base_url: str, api_password: str = None) -> \
    Iterator[str]:
        current_ext_headers: Dict[str, str] = {}
        current_clearkey = None  # Store clearkey from KODIPROP

        for line_with_newline in m3u_lines_iterator:
            line_content = line_with_newline.rstrip('\n')
            logical_line = line_content.strip()

            is_header_tag = False

            # Extract KODIPROP license_key and remove all KODIPROP tags
            if logical_line.startswith('#KODIPROP:'):
                is_header_tag = True

                # Extract clearkey from license_key tag
                if 'inputstream.adaptive.license_key' in logical_line:
                    try:
                        # Format: #KODIPROP:inputstream.adaptive.license_key=VALUE
                        value = logical_line.split('=', 1)[1].strip()

                        # Check if it's a JSON object (starts with {)
                        if value.startswith('{'):
                            try:
                                license_data = json.loads(value)
                                keys = license_data.get('keys', [])
                                clearkey_parts = []
                                for key_item in keys:
                                    kty = key_item.get('kty')
                                    k = key_item.get('k')
                                    kid = key_item.get('kid')

                                    if kty == 'oct' and k and kid:
                                        # Convert Base64 URL safe to Hex for internal use if needed, 
                                        # BUT example shows Hex in JSON? 
                                        # Wait, user example: "k":"8c4a...", "kid":"dc2a..." -> these look like HEX already?
                                        # Let's check user request again.
                                        # User request: "k":"8c4a62f998bd4b6911034bbd7b911b9a","kid":"dc2a18580acc80befd2505253ad69368"
                                        # Yes, they are HEX strings.
                                        clearkey_parts.append(f"{kid}:{k}")

                                if clearkey_parts:
                                    current_clearkey = ",".join(clearkey_parts)
                                    # logger.info("Parsed parsed multiple keys: %s", current_clearkey)
                            except json.JSONDecodeError:
                                logger.error("⚠️ Error decoding JSON license_key: %s", value)
                        else:
                            # Standard Format: KID:KEY (already in hex)
                            # Skip placeholder values like "0000"
                            if value and ':' in value and value != '0000':
                                current_clearkey = value
                    except Exception as e:
                        logger.error("⚠️ Error parsing KODIPROP license_key '%s': %s", logical_line, e)

                # Don't yield ANY KODIPROP line (remove all from output)
                continue

            if logical_line.startswith('#EXTVLCOPT:'):
                is_header_tag = True
                try:
                    option_str = logical_line.split(':', 1)[1]
                    if '=' in option_str:
                        key_vlc, value_vlc = option_str.split('=', 1)
                        key_vlc = key_vlc.strip()
                        value_vlc = value_vlc.strip()
                        if key_vlc == 'http-header' and ':' in value_vlc:
                            header_key, header_value = value_vlc.split(':', 1)
                            header_key = header_key.strip()
                            header_value = header_value.strip()
                            current_ext_headers[header_key] = header_value
                        elif key_vlc.startswith('http-'):
                            header_key = '-'.join(word.capitalize() for word in key_vlc[len('http-'):].split('-'))
                            current_ext_headers[header_key] = value_vlc
                except Exception as e:
                    logger.error("⚠️ Error parsing #EXTVLCOPT '%s': %s", logical_line, e)

            elif logical_line.startswith('#EXTHTTP:'):
                is_header_tag = True
                try:
                    json_str = logical_line.split(':', 1)[1]
                    current_ext_headers = json.loads(json_str)
                except Exception as e:
                    logger.error("⚠️ Error parsing #EXTHTTP '%s': %s", logical_line, e)
                    current_ext_headers = {}

            if is_header_tag:
                yield line_with_newline
                continue

            if logical_line and not logical_line.startswith('#') and \
                    ('http://' in logical_line or 'https://' in logical_line):

                processed_url_content = logical_line

                if 'pluto.tv' in logical_line:
                    processed_url_content = logical_line
                elif 'vavoo.to' in logical_line:
                    encoded_url = urllib.parse.quote(logical_line, safe='')
                    processed_url_content = f"{base_url}/proxy/manifest.m3u8?url={encoded_url}"
                elif '.m3u8' in logical_line:
                    encoded_url = urllib.parse.quote(logical_line, safe='')
                    processed_url_content = f"{base_url}/proxy/manifest.m3u8?url={encoded_url}"
                elif '.mpd' in logical_line:
                    encoded_url = urllib.parse.quote(logical_line, safe='')
                    processed_url_content = f"{base_url}/proxy/manifest.m3u8?url={encoded_url}"
                elif '.php' in logical_line:
                    encoded_url = urllib.parse.quote(logical_line, safe='')
                    processed_url_content = f"{base_url}/proxy/manifest.m3u8?url={encoded_url}"
                else:
                    encoded_url = urllib.parse.quote(logical_line, safe='')
                    processed_url_content = f"{base_url}/proxy/manifest.m3u8?url={encoded_url}"

                # Add clearkey parameter if available
                if current_clearkey:
                    processed_url_content += f"&clearkey={current_clearkey}"
                    current_clearkey = None  # Reset after use

                if current_ext_headers:
                    header_params_str = "".join(
                        [f"&h_{urllib.parse.quote(key)}={urllib.parse.quote(value)}" for key, value in
                         current_ext_headers.items()])
                    processed_url_content += header_params_str
                    current_ext_headers = {}

                # ✅ FIX: Aggiungi api_password se presente
                if api_password:
                    processed_url_content += f"&api_password={api_password}"

                yield processed_url_content + '\n'
            else:
                yield line_with_newline

    async def async_download_m3u_playlist(self, url: str) -> List[str]:
        headers = {
            'User-Agent': self.user_agent,
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        }
        lines = []
        try:
            timeout = ClientTimeout(total=30, connect=10)
            async with ClientSession(timeout=timeout) as client:
                async with client.get(url, headers=headers) as response:
                    response.raise_for_status()
                    content = await response.text()
                    lines = [line + '\n' if line else '' for line in content.split('\n')]
        except Exception as e:
            logger.error("Error downloading playlist (async): %s", str(e))
            raise
        return lines

    def parse_playlist_items(self, lines: List[str]) -> List[List[str]]:
        """Raggruppa le righe in elementi (canali)."""
        items = []
        current_item = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith('#EXTM3U') or stripped.startswith('#EXT-X-VERSION'):
                continue

            current_item.append(line)
            # Se la riga non è un commento/direttiva e non è vuota, è l'URL (fine item)
            if stripped and not stripped.startswith('#'):
                items.append(current_item)
                current_item = []

        # Gestione eventuali righe orfane alla fine
        if current_item:
            items.append(current_item)

        return items

    def get_item_name(self, item_lines: List[str]) -> str:
        """Estrae il nome del canale dagli attributi EXTINF."""
        for l in item_lines:
            if l.startswith('#EXTINF:'):
                # Il nome è solitamente dopo l'ultima virgola
                parts = l.rsplit(',', 1)
                if len(parts) > 1:
                    return parts[1].strip()
        return ""

    async def async_generate_combined_playlist(self, playlist_definitions: List[str], base_url: str,
                                               api_password: str = None):
        playlist_configs = []
        for definition in playlist_definitions:
            # Supporto vecchio formato con & (legacy) e nuovo formato con |
            if '|' in definition:
                parts = definition.split('|')
                url = parts[0]
                options = {}
                for part in parts[1:]:
                    if '=' in part:
                        k, v = part.split('=', 1)
                        options[k.lower()] = v.lower() == 'true'
                playlist_configs.append({'url': url, 'options': options})
            elif '&' in definition:
                # Legacy support
                parts = definition.split('&', 1)
                url = parts[1] if len(parts) > 1 else parts[0]
                playlist_configs.append({'url': url, 'options': {}})
            else:
                playlist_configs.append({'url': definition, 'options': {}})

        results = await asyncio.gather(*[self.async_download_m3u_playlist(cfg['url']) for cfg in playlist_configs],
                                       return_exceptions=True)

        first_playlist_header_handled = False

        # Buffer per gli elementi che devono essere ordinati insieme
        # Contiene tuple: (item_lines, noproxy_flag)
        sorted_items_buffer = []

        for idx, lines in enumerate(results):
            config = playlist_configs[idx]
            options = config['options']

            if isinstance(lines, Exception):
                yield f"# ERROR processing playlist {config['url']}: {str(lines)}\n"
                continue

            playlist_lines: List[str] = lines

            # Se è la prima playlist, gestiamo l'header #EXTM3U
            if not first_playlist_header_handled:
                # Cerca e yielda l'header dalla prima playlist valida
                for line in playlist_lines:
                    if line.strip().startswith('#EXTM3U'):
                        yield line
                        first_playlist_header_handled = True
                        break
                if not first_playlist_header_handled:
                    yield "#EXTM3U\n"
                    first_playlist_header_handled = True

            # Se il sort è attivo, accumuliamo nel buffer
            if options.get('sort'):
                items = self.parse_playlist_items(playlist_lines)
                for item in items:
                    sorted_items_buffer.append({
                        'lines': item,
                        'noproxy': options.get('noproxy', False)
                    })
            else:
                # Se abbiamo un buffer pendente di elementi da ordinare, processiamolo prima
                if sorted_items_buffer:
                    # Ordina
                    sorted_items_buffer.sort(key=lambda x: self.get_item_name(x['lines']).lower())

                    # Yielda elementi bufferizzati
                    for item_data in sorted_items_buffer:
                        item_lines = item_data['lines']
                        if item_data['noproxy']:
                            iterator = iter(item_lines)
                        else:
                            iterator = self.rewrite_m3u_links_streaming(iter(item_lines), base_url,
                                                                        api_password=api_password)

                        for line in iterator:
                            if not line.endswith('\n'): line += '\n'
                            yield line

                    sorted_items_buffer = []

                # Processa la playlist corrente (non ordinata)
                if options.get('noproxy'):
                    iterator = iter(playlist_lines)
                else:
                    iterator = self.rewrite_m3u_links_streaming(iter(playlist_lines), base_url,
                                                                api_password=api_password)

                for line in iterator:
                    # Salta headers globali se già gestiti
                    if line.strip().startswith('#EXTM3U') or line.strip().startswith('#EXT-X-VERSION'):
                        continue
                    if not line.endswith('\n'): line += '\n'
                    yield line

        # Alla fine, se c'è ancora roba nel buffer (es. ultime playlist erano tutte sort=True)
        if sorted_items_buffer:
            sorted_items_buffer.sort(key=lambda x: self.get_item_name(x['lines']).lower())

            for item_data in sorted_items_buffer:
                item_lines = item_data['lines']
                if item_data['noproxy']:
                    iterator = iter(item_lines)
                else:
                    iterator = self.rewrite_m3u_links_streaming(iter(item_lines), base_url, api_password=api_password)

                for line in iterator:
                    if not line.endswith('\n'): line += '\n'
                    yield line
