import json
import logging
import os
from aiohttp import web

from config import check_password

logger = logging.getLogger(__name__)


def setup_recording_routes(app, recording_manager):
    """Setup all recording-related routes."""

    async def handle_recordings_page(request):
        """Serve the recordings UI page."""
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'recordings.html'
        )
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                return web.Response(text=f.read(), content_type='text/html')
        except FileNotFoundError:
            return web.Response(text="Recordings template not found",
                                status=404)

    async def handle_list_recordings(request):
        """GET /api/recordings - List all recordings."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        status = request.query.get('status')
        recordings = recording_manager.get_all_recordings(status=status)

        return web.json_response({
            "recordings": recordings,
            "active_count": len([r for r in recordings if r.get('is_active')])
        })

    async def handle_get_recording(request):
        """GET /api/recordings/{id} - Get a specific recording."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recording_id = request.match_info['id']
        recording = recording_manager.get_recording(recording_id)

        if not recording:
            return web.json_response({"error": "Recording not found"},
                                     status=404)

        return web.json_response(recording)

    async def handle_start_recording(request):
        """POST /api/recordings/start - Start a new recording."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        url = data.get('url')
        if not url:
            return web.json_response({"error": "URL is required"}, status=400)

        name = data.get('name')
        duration = data.get('duration')

        if duration:
            try:
                duration = int(duration)
            except ValueError:
                return web.json_response(
                    {"error": "Duration must be a number"}, status=400)

        recording = await recording_manager.start_recording(
            url=url,
            name=name,
            duration=duration
        )

        if recording:
            return web.json_response(recording, status=201)
        else:
            return web.json_response(
                {"error": "Failed to start recording"}, status=500)

    async def handle_stop_recording(request):
        """POST /api/recordings/{id}/stop - Stop an active recording."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recording_id = request.match_info['id']
        success = await recording_manager.stop_recording(recording_id)

        if success:
            recording = recording_manager.get_recording(recording_id)
            return web.json_response(recording)
        else:
            return web.json_response(
                {"error": "Recording not found or already stopped"},
                status=404)

    async def handle_delete_recording(request):
        """DELETE /api/recordings/{id} - Delete a recording."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recording_id = request.match_info['id']
        success = await recording_manager.delete_recording(recording_id)

        if success:
            return web.json_response({"success": True})
        else:
            return web.json_response({"error": "Recording not found"},
                                     status=404)

    async def handle_delete_recording_get(request):
        """GET /api/recordings/{id}/delete - Delete a recording via GET (for Stremio).

        Returns a simple video placeholder or redirect after deletion.
        """
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recording_id = request.match_info['id']
        success = await recording_manager.delete_recording(recording_id)

        if success:
            logger.debug("Recording %s deleted via GET request", recording_id)
            # Return a simple message - Stremio will show "playback failed" but recording is deleted
            return web.Response(
                text="Recording deleted successfully. Close this and refresh the catalog.",
                content_type="text/plain",
                status=200
            )
        else:
            return web.json_response({"error": "Recording not found"}, status=404)

    async def handle_delete_all_recordings(request):
        """DELETE /api/recordings - Delete all recordings."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recordings = recording_manager.get_all_recordings()
        deleted = 0
        for rec in recordings:
            try:
                await recording_manager.delete_recording(rec['id'])
                deleted += 1
            except Exception as e:
                logger.warning("Failed to delete recording %s: %s", rec['id'], e)

        return web.json_response({"success": True, "deleted": deleted})

    async def handle_download_recording(request):
        """GET /api/recordings/{id}/download - Download a recording file."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

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
        if not file_abs.startswith(recordings_dir):
            return web.json_response({"error": "Access denied"}, status=403)

        filename = os.path.basename(file_path)

        # Determine content type based on extension
        content_type = "video/MP2T"
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

    async def handle_stream_recording(request):
        """GET /api/recordings/{id}/stream - Stream a recording file.

        For completed recordings: uses efficient FileResponse.
        For active recordings: streams the growing file with chunked transfer,
        allowing users to watch while recording continues.
        """
        import asyncio

        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

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
        if not file_abs.startswith(recordings_dir):
            return web.json_response({"error": "Access denied"}, status=403)

        # Determine content type based on extension
        content_type = "video/MP2T"
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

        logger.debug("Starting live stream of active recording %s", recording_id)

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
                            logger.debug("Recording %s finished, ending stream", recording_id)
                            break
                        # Wait for more data from FFmpeg
                        await asyncio.sleep(0.5)
        except ConnectionResetError:
            logger.debug("Client disconnected from recording %s stream", recording_id)
        except Exception as e:
            logger.warning("Error streaming recording %s: %s", recording_id, e)

        await response.write_eof()
        return response

    async def handle_active_recordings(request):
        """GET /api/recordings/active - Get only active recordings."""
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recordings = recording_manager.get_active_recordings()
        return web.json_response({"recordings": recordings})

    async def handle_record_via_get(request):
        """GET /record - Start recording and return a playable stream.

        This endpoint starts recording in the background and returns an HLS
        master playlist that points to the live stream. The user watches
        live TV while recording happens in the background.

        Query parameters:
            url: Stream URL to record (required, URL-encoded)
            name: Recording name (optional)
            duration: Duration in seconds (optional)

        Example:
            /record?url=https%3A%2F%2Fvavoo.to%2Fplay%2F...&name=Sky%20Sport&duration=3600
        """
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        url = request.query.get('url')
        if not url:
            return web.json_response({"error": "URL is required"}, status=400)

        name = request.query.get('name')
        duration = request.query.get('duration')

        # ClearKey parameters for DRM-protected streams
        key_id = request.query.get('key_id')
        key = request.query.get('key')
        clearkey = None
        if key_id and key:
            clearkey = f"{key_id}:{key}"

        if duration:
            try:
                duration = int(duration)
            except ValueError:
                return web.json_response(
                    {"error": "Duration must be a number"}, status=400)

        # Start recording in the background
        recording = await recording_manager.start_recording(
            url=url,
            name=name,
            duration=duration,
            clearkey=clearkey
        )

        if not recording:
            # Check if there's a pending entry (starting or recording) for this URL
            pending = recording_manager.get_pending_recording_by_url(url)
            if pending:
                if pending.get('is_active'):
                    logger.debug("Already recording URL: %s", url)
                else:
                    # Stuck 'starting' entry - clean it up and try again
                    logger.warning("Cleaning up stuck entry for URL: %s", url)
                    await recording_manager.delete_recording(pending['id'])
                    # Try starting again
                    recording = await recording_manager.start_recording(
                        url=url,
                        name=name,
                        duration=duration,
                        clearkey=clearkey
                    )
                    if not recording:
                        logger.error("Failed to start recording after cleanup: %s", url)
            # Even if recording failed, still redirect to live stream
            # so user can watch while we figure out what went wrong
            logger.debug("Recording may have failed, but redirecting to live stream anyway")

        # Build proxy URL to watch the live stream while recording
        from urllib.parse import urlencode

        api_password = request.query.get('api_password', '')

        proxy_params = {'d': url}
        if api_password:
            proxy_params['api_password'] = api_password
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

    async def handle_stop_and_stream(request):
        """GET /record/stop/{id} - Stop an active recording and redirect to stream.

        This endpoint is designed for Stremio integration: when clicked,
        it stops the recording and immediately redirects to play the recorded content.
        """
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        recording_id = request.match_info['id']
        recording = recording_manager.get_recording(recording_id)

        if not recording:
            return web.json_response({"error": "Recording not found"}, status=404)

        # Stop the recording if it's active
        if recording.get('is_active'):
            await recording_manager.stop_recording(recording_id)
            # Refresh recording data after stop
            recording = recording_manager.get_recording(recording_id)

        # Check if file exists and has content
        file_path = recording.get('file_path')
        if not file_path or not os.path.exists(file_path):
            return web.json_response({"error": "Recording file not available yet"}, status=404)

        # Redirect to the stream endpoint (absolute URL for Stremio)
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
        host = request.headers.get('X-Forwarded-Host', request.host)
        base_url = f"{scheme}://{host}"

        api_password = request.query.get('api_password', '')
        stream_url = f"{base_url}/api/recordings/{recording_id}/stream"
        if api_password:
            stream_url += f"?api_password={api_password}"

        raise web.HTTPFound(stream_url)

    # Register routes
    app.router.add_get('/recordings', handle_recordings_page)
    app.router.add_get('/record', handle_record_via_get)  # GET endpoint for StreamVix
    app.router.add_get('/record/stop/{id}', handle_stop_and_stream)  # Stop recording and stream
    app.router.add_get('/api/recordings', handle_list_recordings)
    app.router.add_get('/api/recordings/active', handle_active_recordings)
    app.router.add_post('/api/recordings/start', handle_start_recording)
    app.router.add_delete('/api/recordings/all', handle_delete_all_recordings)
    app.router.add_get('/api/recordings/{id}', handle_get_recording)
    app.router.add_post('/api/recordings/{id}/stop', handle_stop_recording)
    app.router.add_delete('/api/recordings/{id}', handle_delete_recording)
    app.router.add_get('/api/recordings/{id}/delete', handle_delete_recording_get)
    app.router.add_get('/api/recordings/{id}/download', handle_download_recording)
    app.router.add_get('/api/recordings/{id}/stream', handle_stream_recording)

    logger.debug("Recording routes registered")
