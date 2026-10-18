import json
import logging
import os
from aiohttp import web
import functools

import config
from config import check_password, APP_VERSION
import config_store
from services.proxy_shared import admin_guard as admin
from services.proxy_shared import get_public_base_url, read_template

logger = logging.getLogger(__name__)


def setup_recording_routes(app, recording_manager):
    """Setup all recording-related routes."""

    def dvr_required(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            if not config_store.get("dvr_enabled", False):
                return web.json_response({"error": "DVR is disabled"}, status=404)
            return await handler(*args, **kwargs)
        return wrapper

    async def handle_recordings_page(request):
        """Serve the recordings UI page."""
        if not check_password(request):
            raise web.HTTPFound('/admin/login')
        try:
            html_content = read_template('recordings.html')
            proxy = app.get('proxy')
            latest_version = getattr(proxy, 'latest_version', 'Unknown') if proxy else 'Unknown'
            warp_status = getattr(proxy, 'warp_status', 'Unknown') if proxy else 'Unknown'
            is_outdated = latest_version not in ["Checking...", "Unknown", "Error", APP_VERSION]
            version_status_class = "outdated" if is_outdated else ""
            html_content = html_content.replace("{{LATEST_VERSION}}", latest_version)
            html_content = html_content.replace("{{VERSION_STATUS_CLASS}}", version_status_class)
            html_content = html_content.replace("{{WARP_STATUS}}", warp_status)
            return web.Response(text=html_content, content_type='text/html')
        except FileNotFoundError:
            return web.Response(text="Recordings template not found",
                               status=404)

    @admin
    async def handle_list_recordings(request):
        """GET /api/recordings - List all recordings."""

        status = request.query.get('status')
        recordings = recording_manager.get_all_recordings(status=status)
        system_stats = config.get_system_stats()

        return web.json_response({
            "recordings": recordings,
            "active_count": len([r for r in recordings if r.get('is_active')]),
            "system_stats": system_stats
        })

    @admin
    async def handle_get_recording(request):
        """GET /api/recordings/{id} - Get a specific recording."""

        recording_id = request.match_info['id']
        recording = recording_manager.get_recording(recording_id)

        if not recording:
            return web.json_response({"error": "Recording not found"},
                                    status=404)

        return web.json_response(recording)

    @admin
    async def handle_start_recording(request):
        """POST /api/recordings/start - Start a new recording."""

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        url = data.get('url')
        if not url:
            return web.json_response({"error": "URL is required"}, status=400)

        name = data.get('name')
        duration = data.get('duration')
        warp = data.get('warp')
        proxy = data.get('proxy')
        disable_ssl = data.get('disable_ssl')
        extractor = (data.get('extractor') or '').strip() or None
        max_res = str(data.get('max_res') or '').strip().lower() in ('1', 'true', 'yes', 'on')

        # Append configuration parameters as query params to the URL
        from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
        parsed = urlparse(url)
        qsl = parse_qsl(parsed.query)
        if warp == 'off':
            qsl.append(('warp', 'off'))
        if proxy and proxy != 'on':
            qsl.append(('proxy', str(proxy).strip()))
        if disable_ssl == '1':
            qsl.append(('disable_ssl', '1'))
        url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(qsl), parsed.fragment))

        if duration is not None and duration != "":
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "Duration must be a number"}, status=400)
            if duration <= 0:
                return web.json_response(
                    {"error": "Duration must be positive"}, status=400)
        else:
            duration = None

        recording = await recording_manager.start_recording(
            url=url,
            name=name,
            duration=duration,
            extractor=extractor,
            max_res=max_res
        )

        if recording:
            return web.json_response(recording, status=201)
        else:
            return web.json_response(
                {"error": "Failed to start recording"}, status=500)

    @admin
    async def handle_stop_recording(request):
        """POST /api/recordings/{id}/stop - Stop an active recording."""

        recording_id = request.match_info['id']
        success = await recording_manager.stop_recording(recording_id)

        if success:
            recording = recording_manager.get_recording(recording_id)
            return web.json_response(recording)
        else:
            return web.json_response(
                {"error": "Recording not found or already stopped"},
                status=404)

    @admin
    async def handle_delete_recording(request):
        """DELETE /api/recordings/{id} - Delete a recording."""

        recording_id = request.match_info['id']
        success = await recording_manager.delete_recording(recording_id)

        if success:
            return web.json_response({"success": True})
        else:
            return web.json_response({"error": "Recording not found"},
                                    status=404)

    @admin
    async def handle_delete_recording_get(request):
        """GET /api/recordings/{id}/delete - Delete a recording via GET (for Stremio).

        Returns a simple video placeholder or redirect after deletion.
        """

        recording_id = request.match_info['id']
        success = await recording_manager.delete_recording(recording_id)

        if success:
            logger.debug(f"Recording {recording_id} deleted via GET request")
            # Return a simple message - Stremio will show "playback failed" but recording is deleted
            return web.Response(
                text="Recording deleted successfully. Close this and refresh the catalog.",
                content_type="text/plain",
                status=200
            )
        else:
            return web.json_response({"error": "Recording not found"}, status=404)

    @admin
    async def handle_delete_all_recordings(request):
        """DELETE /api/recordings - Delete all recordings."""

        recordings = recording_manager.get_all_recordings()
        deleted = 0
        for rec in recordings:
            try:
                await recording_manager.delete_recording(rec['id'])
                deleted += 1
            except Exception as e:
                logger.warning(f"Failed to delete recording {rec['id']}: {e}")

        return web.json_response({"success": True, "deleted": deleted})

    @admin
    async def handle_download_recording(request):
        """GET /api/recordings/{id}/download - Download a recording file."""

        recording_id = request.match_info['id']
        recording = recording_manager.get_recording(recording_id)

        if not recording:
            return web.json_response({"error": "Recording not found"},
                                    status=404)

        file_path = recording.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return web.json_response({"error": "Recording file not found"},
                                    status=404)

        # Security check
        recordings_dir = os.path.abspath(recording_manager.recordings_dir)
        file_abs = os.path.abspath(file_path)
        try:
            in_recordings_dir = os.path.commonpath([recordings_dir, file_abs]) == recordings_dir
        except ValueError:
            in_recordings_dir = False
        if not in_recordings_dir:
            return web.json_response({"error": "Access denied"}, status=403)

        filename = os.path.basename(file_path)

        # Determine content type based on extension
        content_type = "video/mp2t"
        if filename.endswith('.mp4'):
            content_type = "video/mp4"
        elif filename.endswith('.mkv'):
            content_type = "video/x-matroska"

        return web.FileResponse(
            file_path,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": content_type
            }
        )

    @admin
    async def handle_stream_recording(request):
        """GET /api/recordings/{id}/stream - Stream a recording file.

        For completed recordings: uses efficient FileResponse.
        For active recordings: streams the growing file with chunked transfer,
        allowing users to watch while recording continues.
        """
        import asyncio


        recording_id = request.match_info['id']
        recording = recording_manager.get_recording(recording_id)

        if not recording:
            return web.json_response({"error": "Recording not found"},
                                    status=404)

        file_path = recording.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return web.json_response({"error": "Recording file not found"},
                                    status=404)

        # Security check
        recordings_dir = os.path.abspath(recording_manager.recordings_dir)
        file_abs = os.path.abspath(file_path)
        try:
            in_recordings_dir = os.path.commonpath([recordings_dir, file_abs]) == recordings_dir
        except ValueError:
            in_recordings_dir = False
        if not in_recordings_dir:
            return web.json_response({"error": "Access denied"}, status=403)

        # Determine content type based on extension
        content_type = "video/mp2t"
        if file_path.endswith('.mp4'):
            content_type = "video/mp4"
        elif file_path.endswith('.mkv'):
            content_type = "video/x-matroska"

        # For completed recordings: use efficient FileResponse
        if not recording.get('is_active'):
            return web.FileResponse(
                file_path,
                headers={
                    "Content-Type": content_type,
                    "Access-Control-Allow-Origin": "*"
                }
            )

        # For active recordings: stream growing file with chunked transfer
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": content_type,
                "Transfer-Encoding": "chunked",
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-cache"
            }
        )
        await response.prepare(request)

        logger.debug(f"Starting live stream of active recording {recording_id}")

        try:
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)  # 64KB chunks
                    if chunk:
                        await response.write(chunk)
                    else:
                        # Check if recording is still active
                        rec = recording_manager.get_recording(recording_id)
                        if not rec or not rec.get('is_active'):
                            logger.debug(f"Recording {recording_id} finished, ending stream")
                            break
                        # Wait for more data from recording process
                        await asyncio.sleep(0.5)
        except ConnectionResetError:
            logger.debug(f"Client disconnected from recording {recording_id} stream")
        except Exception as e:
            logger.warning(f"Error streaming recording {recording_id}: {e}")

        await response.write_eof()
        return response

    @admin
    async def handle_active_recordings(request):
        """GET /api/recordings/active - Get only active recordings."""

        recordings = recording_manager.get_active_recordings()
        return web.json_response({"recordings": recordings})

    @admin
    async def handle_record_via_get(request):
        """GET /record - Start recording and return a playable stream.

        This endpoint starts recording in the background and returns an HLS
        master playlist that points to the live stream. The user watches
        live TV while recording happens in the background.

        Query parameters:
            url: Stream URL to record (required, URL-encoded)
            name: Recording name (optional)
            duration: Duration in seconds (optional)
            extractor: Force a specific extractor instead of auto-detection (optional)
            max_res: Record only the highest video variant (optional)
            key_id / key: ClearKey for DRM-protected streams (optional)

        Example:
            /record?url=https%3A%2F%2Fvavoo.to%2Fplay%2F...&name=Sky%20Sport&duration=3600&extractor=vavoo&max_res=1
        """

        url = request.query.get('url')
        if not url:
            return web.json_response({"error": "URL is required"}, status=400)

        name = request.query.get('name')
        duration = request.query.get('duration')
        extractor = (request.query.get('extractor') or '').strip() or None
        max_res = request.query.get('max_res', '').strip().lower() in ('1', 'true', 'yes', 'on')
        warp = request.query.get('warp')
        proxy = request.query.get('proxy')

        # Promote routing flags onto the source URL so the recording manager
        # forwards them to its internal proxy request.
        if warp == 'off' or (proxy and proxy != 'on'):
            from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
            parsed = urlparse(url)
            qsl = parse_qsl(parsed.query)
            if warp == 'off':
                qsl.append(('warp', 'off'))
            if proxy and proxy != 'on':
                qsl.append(('proxy', str(proxy).strip()))
            url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(qsl), parsed.fragment))

        # ClearKey parameters for DRM-protected streams
        key_id = request.query.get('key_id')
        key = request.query.get('key')
        clearkey = None
        if key_id and key:
            clearkey = f"{key_id}:{key}"

        if duration is not None and duration != "":
            try:
                duration = int(duration)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": "Duration must be a number"}, status=400)
            if duration <= 0:
                return web.json_response(
                    {"error": "Duration must be positive"}, status=400)
        else:
            duration = None

        # Start recording in the background
        recording = await recording_manager.start_recording(
            url=url,
            name=name,
            duration=duration,
            clearkey=clearkey,
            extractor=extractor,
            max_res=max_res
        )

        if not recording:
            # Check if there's a pending entry (starting or recording) for this URL
            pending = recording_manager.get_pending_recording_by_url(url)
            if pending:
                if pending.get('is_active'):
                    logger.debug(f"Already recording URL: {url}")
                else:
                    # Stuck 'starting' entry - clean it up and try again
                    logger.warning(f"Cleaning up stuck entry for URL: {url}")
                    await recording_manager.delete_recording(pending['id'])
                    # Try starting again
                    recording = await recording_manager.start_recording(
                        url=url,
                        name=name,
                        duration=duration,
                        clearkey=clearkey,
                        extractor=extractor,
                        max_res=max_res
                    )
                    if not recording:
                        logger.error(f"Failed to start recording after cleanup: {url}")
            # Even if recording failed, still redirect to live stream
            # so user can watch while we figure out what went wrong
            logger.debug("Recording may have failed, but redirecting to live stream anyway")

        # Build proxy URL to watch the live stream while recording
        from urllib.parse import urlencode

        api_password = request.query.get('api_password', '')

        proxy_params = {'d': url}
        if api_password:
            proxy_params['api_password'] = api_password
        if extractor:
            proxy_params['host'] = extractor
        if max_res:
            proxy_params['max_res'] = 'true'
        if warp == 'off':
            proxy_params['warp'] = 'off'
        if proxy and proxy != 'on':
            proxy_params['proxy'] = str(proxy).strip()
        if key_id:
            proxy_params['key_id'] = key_id
        if key:
            proxy_params['key'] = key

        # Use correct endpoint based on stream type
        if '.mpd' in url.lower():
            endpoint = "/proxy/mpd/manifest.m3u8"
        else:
            endpoint = "/proxy/hls/manifest.m3u8"

        proxy_url = f"{endpoint}?{urlencode(proxy_params)}"

        # Redirect to the live stream proxy
        raise web.HTTPFound(proxy_url)

    @admin
    async def handle_stop_and_stream(request):
        """GET /record/stop/{id} - Stop an active recording and redirect to stream.

        This endpoint is designed for Stremio integration: when clicked,
        it stops the recording and immediately redirects to play the recorded content.
        """

        recording_id = request.match_info['id']
        recording = recording_manager.get_recording(recording_id)

        if not recording:
            return web.json_response({"error": "Recording not found"}, status=404)

        # Stop the recording if it's active
        if recording.get('is_active'):
            await recording_manager.stop_recording(recording_id)
            # Refresh recording data after stop
            recording = recording_manager.get_recording(recording_id)
            if not recording:
                return web.json_response({"error": "Recording not found"}, status=404)

        # Check if file exists and has content
        file_path = recording.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return web.json_response({"error": "Recording file not available yet"}, status=404)

        # Redirect to the stream endpoint (absolute URL for Stremio)
        base_url = get_public_base_url(request)

        api_password = request.query.get('api_password', '')
        stream_url = f"{base_url}/api/recordings/{recording_id}/stream"
        if api_password:
            stream_url += f"?api_password={api_password}"

        raise web.HTTPFound(stream_url)

    # Register routes
    app.router.add_get('/recordings', dvr_required(handle_recordings_page))
    app.router.add_get('/record', dvr_required(handle_record_via_get))  # GET endpoint for StreamVix
    app.router.add_get('/record/stop/{id}', dvr_required(handle_stop_and_stream))  # Stop recording and stream
    app.router.add_get('/api/recordings', dvr_required(handle_list_recordings))
    app.router.add_get('/api/recordings/active', dvr_required(handle_active_recordings))
    app.router.add_post('/api/recordings/start', dvr_required(handle_start_recording))
    app.router.add_delete('/api/recordings/all', dvr_required(handle_delete_all_recordings))
    app.router.add_get('/api/recordings/{id}', dvr_required(handle_get_recording))
    app.router.add_post('/api/recordings/{id}/stop', dvr_required(handle_stop_recording))
    app.router.add_delete('/api/recordings/{id}', dvr_required(handle_delete_recording))
    app.router.add_get('/api/recordings/{id}/delete', dvr_required(handle_delete_recording_get))
    app.router.add_get('/api/recordings/{id}/download', dvr_required(handle_download_recording))
    app.router.add_get('/api/recordings/{id}/stream', dvr_required(handle_stream_recording))

    logger.debug("Recording routes registered")
