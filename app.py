import logging
import sys
import os
import asyncio
import aiohttp
from aiohttp import web

# Configura logging PRIMA di qualsiasi import che possa emettere log
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)

# Aggiungi path corrente per import moduli
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.proxy import HLSProxy
from config import PORT, RECORDINGS_DIR, APP_VERSION
from services.recording_manager import RecordingManager
from routes.recordings import setup_recording_routes

logger = logging.getLogger(__name__)

def _read_file(path):
    """Helper for async file reading via run_in_executor."""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

# --- Logica di Avvio ---
def create_app():
    """Crea e configura l'applicazione aiohttp."""
    proxy = HLSProxy()

    app = web.Application()
    app['proxy'] = proxy

    # Initialize recording manager for DVR functionality
    recording_manager = RecordingManager(
        recordings_dir=RECORDINGS_DIR
    )
    app['recording_manager'] = recording_manager
    
    # Registra le route
    app.router.add_get('/', proxy.handle_root)
    app.router.add_get('/docs', proxy.handle_docs)
    app.router.add_get('/redoc', proxy.handle_redoc)
    app.router.add_get('/openapi.json', proxy.handle_openapi)
    app.router.add_get('/favicon.ico', proxy.handle_favicon) # ✅ Route Favicon
    
    # ✅ Route Static Files (con path assoluto e creazione automatica)
    static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    if not os.path.exists(static_path):
        os.makedirs(static_path)
    app.router.add_static('/static', static_path)
    
    app.router.add_get('/builder', proxy.handle_builder)
    app.router.add_get('/playlist/builder', proxy.handle_builder)
    app.router.add_get('/url-generator', proxy.handle_url_generator)
    app.router.add_get('/info', proxy.handle_info_page)
    app.router.add_get('/api/info', proxy.handle_api_info)
    app.router.add_get('/key', proxy.handle_key_request)
    app.router.add_get('/proxy/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/manifest.mpd', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/segment/{session_id}/{tail:.*}', proxy.handle_dash_segment)
    # ✅ NUOVO: Endpoint generico per stream (compatibilità MFP)
    app.router.add_get('/proxy/stream', proxy.handle_proxy_request)
    app.router.add_get('/extractor', proxy.handle_extractor_request)
    # ✅ NUOVO: Endpoint compatibilità MFP per estrazione
    app.router.add_get('/extractor/video', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.m3u8', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mp4', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mpd', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.ts', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.m4s', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.vtt', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.aac', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.m4a', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.webm', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mkv', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.avi', proxy.handle_extractor_request)
    app.router.add_get('/extractor/video.mov', proxy.handle_extractor_request)
    
    # ✅ NUOVO: Route per segmenti con estensioni corrette per compatibilità player
    app.router.add_get('/proxy/hls/segment.ts', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/segment.m4s', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/segment.mp4', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/segment.vtt', proxy.handle_proxy_request)
    
    app.router.add_get('/playlist', proxy.handle_playlist_request)
    app.router.add_get('/segment/{segment}', proxy.handle_ts_segment)
    app.router.add_get('/decrypt/segment.mp4', proxy.handle_decrypt_segment)  # ClearKey decryption for legacy mode
    app.router.add_get('/decrypt/segment.ts', proxy.handle_decrypt_segment)   # TS variant for legacy mode
    
    # ✅ NUOVO: Route per licenze DRM (GET e POST)
    app.router.add_get('/license', proxy.handle_license_request)
    app.router.add_post('/license', proxy.handle_license_request)

    # WittyTV opaque playback sessions. Only session creation/status require
    # the API password; playback uses an unguessable, expiring bearer token.
    app.router.add_get('/witty/status', proxy.handle_witty_status)
    app.router.add_post('/witty/session', proxy.handle_witty_create_session)
    app.router.add_get('/witty/play/{token}/manifest.mpd', proxy.handle_witty_manifest)
    app.router.add_get('/witty/play/{token}/segment/{tail:.*}', proxy.handle_witty_segment)
    # Generic Mediaset Infinity/WittyTV routes. Legacy /witty routes remain
    # available for already configured addon instances.
    app.router.add_get('/mediaset/status', proxy.handle_witty_status)
    app.router.add_post('/mediaset/session', proxy.handle_witty_create_session)
    app.router.add_get('/mediaset/play/{token}/manifest.mpd', proxy.handle_witty_manifest)
    app.router.add_get('/mediaset/play/{token}/segment/{tail:.*}', proxy.handle_witty_segment)

    # RaiPlay opaque Widevine sessions. Clear HLS remains direct; only protected
    # DASH content traverses EasyProxy.
    app.router.add_get('/raiplay/status', proxy.handle_raiplay_status)
    app.router.add_post('/raiplay/session', proxy.handle_raiplay_create_session)
    app.router.add_get('/raiplay/play/{token}/manifest.mpd', proxy.handle_raiplay_manifest)
    app.router.add_get('/raiplay/play/{token}/segment/{tail:.*}', proxy.handle_raiplay_segment)
    
    # ✅ NUOVO: Endpoint per generazione URL (compatibilità MFP)
    app.router.add_post('/generate_urls', proxy.handle_generate_urls)

    # ✅ NUOVO: Endpoint per ottenere l'IP pubblico
    app.router.add_get('/proxy/ip', proxy.handle_proxy_ip)
    # ✅ Health check endpoint
    app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "version": APP_VERSION}))

    # Admin Panel
    app.router.add_get('/admin', proxy.handle_admin)
    app.router.add_get('/admin/login', proxy.handle_admin_login)
    app.router.add_post('/api/admin/login', proxy.handle_admin_api_login)
    app.router.add_get('/admin/logout', proxy.handle_admin_logout)
    app.router.add_get('/api/admin/config', proxy.handle_admin_api_get)
    app.router.add_post('/api/admin/config', proxy.handle_admin_api_update)
    app.router.add_get('/api/admin/config/download', proxy.handle_admin_api_download)
    app.router.add_post('/api/admin/config/upload', proxy.handle_admin_api_upload)
    app.router.add_post('/api/admin/warp/toggle', proxy.handle_admin_api_warp_toggle)
    app.router.add_post('/api/admin/warp/reconnect', proxy.handle_admin_api_warp_reconnect)
    app.router.add_post('/api/admin/extractor/proxy', proxy.handle_admin_api_extractor_proxy)
    app.router.add_post('/api/admin/speedtest', proxy.handle_admin_api_speedtest)
    # Setup recording/DVR routes
    setup_recording_routes(app, recording_manager)
    
    # Gestore OPTIONS generico per CORS
    app.router.add_route('OPTIONS', '/{tail:.*}', proxy.handle_options)
    
    async def cleanup_handler(app):
        await proxy.cleanup()
    app.on_cleanup.append(cleanup_handler)
    
    async def on_startup(app):
        asyncio.create_task(proxy.start_tasks())
        asyncio.create_task(recording_manager.cleanup_loop())
    app.on_startup.append(on_startup)

    async def on_shutdown(app):
        await recording_manager.shutdown()
    app.on_shutdown.append(on_shutdown)
    
    return app

# Crea l'istanza "privata" dell'applicazione aiohttp.
app = create_app()

def main():
    """Funzione principale per avviare il server."""
    # Workaround per il bug di asyncio su Windows con ConnectionResetError
    if sys.platform == 'win32':
        # Silenzia il logger di asyncio per evitare spam di ConnectionResetError
        logging.getLogger('asyncio').setLevel(logging.CRITICAL)

    logger.info("🚀 Starting HLS Proxy Server...")
    logger.info("📡 Server available at: http://localhost:%s", PORT)
    logger.info("📡 Or: http://server-ip:%s", PORT)
    logger.debug("🔗 Endpoints:")
    logger.debug("   • / - Main page")
    logger.debug("   • /builder - Web interface for playlist builder")
    logger.debug("   • /info - Server information page")
    logger.debug("   • /recordings - DVR/Recording interface")
    logger.debug("   • /proxy/manifest.m3u8?url=<URL> - Main stream proxy")
    logger.debug("   • /playlist?url=<definitions> - Playlist generator")
    logger.debug("%s", "=" * 50)
    
    web.run_app(
        app, # Usa l'istanza aiohttp originale per il runner integrato
        host='0.0.0.0',
        port=PORT
    )

if __name__ == '__main__':
    main()
