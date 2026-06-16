import asyncio
import uuid
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import urlencode

import aiohttp
import config_store
from services.recording_db import RecordingDB
from config import PORT, API_PASSWORD

import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class StreamType(Enum):
    """Stream type classification for recording."""
    MPD = "mpd"           # DASH/MPD streams (converted to HLS by proxy)
    VAVOO = "vavoo"       # Vavoo.to HLS streams
    FREESHOT = "freeshot" # Freeshot/popcdn HLS streams
    SPORTSONLINE = "sportsonline"  # SportsOnline HLS streams
    GENERIC = "generic"   # Unknown/generic HLS streams


@dataclass
class StreamConfig:
    """Configuration for recording a stream."""
    video_url: str
    audio_url: Optional[str] = None
    stream_type: StreamType = StreamType.GENERIC
    needs_reconnect: bool = False
    needs_extended_probe: bool = False


class RecordingManager:
    """Manages FFmpeg recording processes for DVR functionality."""

    # Stream types that benefit from reconnection (proxy handles token refresh)
    RECONNECT_TYPES = {StreamType.VAVOO, StreamType.FREESHOT,
                       StreamType.SPORTSONLINE, StreamType.MPD}

        self.db = RecordingDB(recordings_dir)
        self.processes: Dict[str, asyncio.subprocess.Process] = {}
        self.start_times: Dict[str, float] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                connector=aiohttp.TCPConnector(limit=10),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # =========================================================================
    # Stream Type Detection
    # =========================================================================

    @staticmethod
    def _detect_stream_type(url: str) -> StreamType:
        """Detect the stream type based on URL patterns."""
        url_lower = url.lower()

        if '.mpd' in url_lower:
            return StreamType.MPD
        elif 'vavoo.to' in url_lower:
            return StreamType.VAVOO
        elif 'popcdn.day' in url_lower or 'freeshot' in url_lower:
            return StreamType.FREESHOT
        elif any(d in url_lower for d in ['sportsonline', 'sportzonline']):
            return StreamType.SPORTSONLINE

        return StreamType.GENERIC

    # =========================================================================
    # Stream Configuration Preparation
    # =========================================================================

    async def _prepare_stream_config(
        self,
        url: str,
        clearkey: Optional[str] = None
    ) -> StreamConfig:
        """
        Prepare stream configuration based on stream type.

        This is the main dispatcher that routes to type-specific handlers.
        All streams go through the local proxy for token refresh and authentication.
        """
        stream_type = self._detect_stream_type(url)

        if stream_type == StreamType.MPD:
            return await self._prepare_mpd_config(url, clearkey)
        else:
            return self._prepare_hls_config(url, stream_type)

    async def _prepare_mpd_config(
        self,
        url: str,
        clearkey: Optional[str] = None
    ) -> StreamConfig:
        """
        Prepare configuration for MPD/DASH streams.

        MPD streams are converted to HLS by the proxy and may have:
        - ClearKey DRM requiring decryption parameters
        - Separate audio tracks requiring dual-input FFmpeg
        """
        proxy_params = self._build_proxy_params(url)

        # Add ClearKey parameters for DRM-protected streams
        if clearkey and ':' in clearkey:
            key_id, key = clearkey.split(':', 1)
            proxy_params['key_id'] = key_id
            proxy_params['key'] = key
            logger.debug("🔐 MPD Recording with ClearKey decryption enabled")
        else:
            logger.warning("⚠️ MPD Recording without ClearKey - content may be encrypted")

        master_url = f"http://127.0.0.1:{PORT}/proxy/mpd/manifest.m3u8?{urlencode(proxy_params)}"
        logger.info(f"Recording MPD stream: {url[:80]}...")

        # Parse master playlist to extract separate audio track
        video_url, audio_url = await self._parse_master_playlist(master_url)

        if video_url is None:
            logger.warning("Failed to parse master playlist, using master URL directly")
            video_url = master_url
            audio_url = None
        else:
            logger.debug(f"Parsed MPD master: video=present, audio={'present' if audio_url else 'embedded'}")

        return StreamConfig(
            video_url=video_url,
            audio_url=audio_url,
            stream_type=StreamType.MPD,
            needs_reconnect=True,
            needs_extended_probe=True
        )

    def _prepare_hls_config(self, url: str, stream_type: StreamType) -> StreamConfig:
        """
        Prepare configuration for HLS streams (Vavoo, Freeshot, etc.).

        HLS streams typically have audio muxed with video, so no separate
        audio URL is needed.
        """
        proxy_params = self._build_proxy_params(url)
        video_url = f"http://127.0.0.1:{PORT}/proxy/hls/manifest.m3u8?{urlencode(proxy_params)}"

        logger.info(f"Recording HLS stream ({stream_type.value}): {url[:80]}...")

        return StreamConfig(
            video_url=video_url,
            audio_url=None,
            stream_type=stream_type,
            needs_reconnect=stream_type in self.RECONNECT_TYPES,
            needs_extended_probe=False
        )

    def _build_proxy_params(self, url: str) -> Dict[str, str]:
        """Build common proxy parameters."""
        params = {'d': url, 'no_bypass': '1'}
        if API_PASSWORD:
            params['api_password'] = API_PASSWORD
        return params
        
    def __init__(self, recordings_dir: Optional[str] = None):
        default_dir = os.getenv("RECORDINGS_DIR", "/opt/render/project/src/storage/recordings")
        self.recordings_dir = str(Path(recordings_dir or default_dir))
        Path(self.recordings_dir).mkdir(parents=True, exist_ok=True)

        self.db = RecordingDB(self.recordings_dir)
        self.processes: Dict[str, asyncio.subprocess.Process] = {}
        self.start_times: Dict[str, float] = {}
        self._session: Optional[aiohttp.ClientSession] = None
    
    # =========================================================================
    # Master Playlist Parsing
    # =========================================================================

    async def _parse_master_playlist(
        self,
        master_url: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse HLS master playlist to extract video and audio playlist URLs.

        Returns:
            Tuple of (video_playlist_url, audio_playlist_url)
            audio_playlist_url may be None if audio is embedded in video
        """
        try:
            async with self.session.get(
                master_url,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to fetch master playlist: {resp.status}")
                        return None, None
                    content = await resp.text()

            video_url = None
            audio_url = None

            lines = content.strip().split('\n')
            for i, line in enumerate(lines):
                # Parse EXT-X-MEDIA for separate audio track
                if line.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in line:
                    uri_match = re.search(r'URI="([^"]+)"', line)
                    if uri_match and audio_url is None:
                        if 'DEFAULT=YES' in line or audio_url is None:
                            audio_url = uri_match.group(1)

                # Parse EXT-X-STREAM-INF for video variant
                elif line.startswith('#EXT-X-STREAM-INF:'):
                    if i + 1 < len(lines):
                        next_line = lines[i + 1].strip()
                        if next_line and not next_line.startswith('#'):
                            video_url = next_line

            return video_url, audio_url

        except Exception as e:
            logger.error(f"Error parsing master playlist: {e}")
            return None, None

    # =========================================================================
    # FFmpeg Command Building
    # =========================================================================

    def _build_ffmpeg_command(
        self,
        config: StreamConfig,
        output_path: str,
        duration: Optional[int] = None
    ) -> List[str]:
        """
        Build FFmpeg command for recording based on stream configuration.

        This is the unified command builder that handles all stream types.
        Stream-specific options are determined by the StreamConfig.
        """
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "info",
            "-y",
        ]

        # Probe settings based on stream type
        if config.needs_extended_probe:
            cmd.extend([
                "-err_detect", "ignore_err",
                "-fflags", "+genpts+discardcorrupt+igndts+nobuffer",
                "-analyzeduration", "20000000",
                "-probesize", "20000000",
            ])
        else:
            cmd.extend([
                "-fflags", "+genpts+discardcorrupt+igndts",
                "-analyzeduration", "10000000",
                "-probesize", "10000000",
            ])

        # Network options
        if config.video_url.startswith('http'):
            cmd.extend(["-rw_timeout", "30000000"])

            if config.needs_reconnect:
                cmd.extend([
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "2",
                ])

        # HLS-specific options
        if '.m3u8' in config.video_url.lower():
            cmd.extend(["-live_start_index", "-1"])

        # Duration limit
        if duration:
            cmd.extend(["-t", str(duration)])

        # Video input
        cmd.extend(["-i", config.video_url])

        # Separate audio input (for MPD with separate audio tracks)
        if config.audio_url:
            if config.audio_url.startswith('http'):
                cmd.extend(["-rw_timeout", "30000000"])
            cmd.extend(["-live_start_index", "-1"])
            cmd.extend(["-i", config.audio_url])

        # Stream mapping
        if config.audio_url:
            # Dual-input: video from input 0, audio from input 1
            cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
            logger.debug("Using dual-input mode: video + separate audio")
        else:
            # Single input: video and optional audio from same input
            cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

        # Copy streams without re-encoding, output to MPEG-TS
        cmd.extend(["-c", "copy"])
        cmd.append(output_path)

        return cmd

    # =========================================================================
    # Recording Lifecycle
    # =========================================================================

    async def start_recording(
        self,
        url: str,
        name: Optional[str] = None,
        duration: Optional[int] = None,
        clearkey: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Start recording a stream.

        All streams go through the local proxy for authentication and token refresh.

        Args:
            url: Stream URL to record
            name: Human-readable name for the recording
            duration: Recording duration in seconds (None = max_duration)
            clearkey: ClearKey in format "key_id:key" for DRM-protected streams

        Returns:
            Recording info dict or None if failed
        """
        if not name:
            name = f"Recording {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"

        recording_id = self._generate_recording_id()

        # Claim the recording (prevents duplicates)
        if not self.db.create_starting_entry(recording_id, name, url):
            logger.info(f"Recording already exists for URL: {url[:80]}...")
            return None

        filename = self._generate_filename(recording_id, name)
        recordings_dir = config_store.get("recordings_dir", self.recordings_dir)
        file_path = os.path.join(recordings_dir, filename)
        if not os.path.exists(recordings_dir):
            os.makedirs(recordings_dir)

        # Apply duration limits
        max_duration = config_store.get("max_recording_duration", 28800)
        if duration:
            duration = min(duration, max_duration)
        else:
            duration = max_duration

        # Prepare stream-specific configuration
        config = await self._prepare_stream_config(url, clearkey)

        # Build FFmpeg command
        cmd = self._build_ffmpeg_command(config, file_path, duration)

        logger.info(f"Starting recording {recording_id}: {name}")
        logger.debug(f"FFmpeg command: {' '.join(cmd)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            self.processes[recording_id] = process
            self.start_times[recording_id] = time.time()

            self.db.update_to_recording(
                recording_id=recording_id,
                file_path=file_path,
                headers=None,
                pid=process.pid
            )

            asyncio.create_task(self._monitor_recording(recording_id, process))

            return self.db.get_recording(recording_id)

        except Exception as e:
            logger.error(f"Failed to start recording {recording_id}: {e}")
            self.db.update_recording_status(recording_id, 'failed', str(e))
            return None

    async def stop_recording(self, recording_id: str) -> bool:
        """
        Stop an active recording.

        Supports multi-worker: if process not in local dict, use PID from DB.
        """
        recording = self.db.get_recording(recording_id)
        if not recording:
            logger.warning(f"Recording {recording_id} not found in database")
            return False

        if recording_id in self.processes:
            process = self.processes[recording_id]
            try:
                # Send 'q' to FFmpeg for graceful shutdown
                if process.stdin:
                    process.stdin.write(b'q')
                    await process.stdin.drain()
                    process.stdin.close()

                try:
                    await asyncio.wait_for(process.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Recording {recording_id} didn't stop gracefully, terminating")
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"Recording {recording_id} didn't terminate, killing")
                        process.kill()
            except Exception as e:
                logger.error(f"Error stopping process: {e}")
                try:
                    process.terminate()
                except Exception:
                    pass
            finally:
                self.processes.pop(recording_id, None)
                self.start_times.pop(recording_id, None)
        else:
            # Process in different worker - use PID from database
            pid = recording.get('pid')
            if pid and self.db.is_pid_running(pid):
                try:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                    await asyncio.sleep(2)
                    if self.db.is_pid_running(pid):
                        os.kill(pid, signal.SIGKILL)
                    logger.debug(f"Stopped recording {recording_id} via PID {pid}")
                except ProcessLookupError:
                    logger.debug(f"Process {pid} already terminated")
                except Exception as e:
                    logger.error(f"Error killing process {pid}: {e}")

        self.db.update_recording_status(recording_id, 'stopped')

        if recording.get('file_path'):
            file_path = recording['file_path']
            if os.path.exists(file_path):
                started_at = recording.get('started_at')
                rec_duration = self._calculate_elapsed(started_at) if started_at else 0
                file_size = os.path.getsize(file_path)
                self.db.update_recording_file_info(recording_id, rec_duration, file_size)

        logger.info(f"Recording {recording_id} stopped")
        return True

    async def _monitor_recording(
        self,
        recording_id: str,
        process: asyncio.subprocess.Process
    ):
        """Monitor a recording process and update status when complete."""
        try:
            _, stderr = await process.communicate()

            if recording_id not in self.processes:
                return

            stderr_text = stderr.decode() if stderr else ""

            if stderr_text:
                logger.debug(f"Recording {recording_id} FFmpeg output: {stderr_text[:1000]}")

            if process.returncode == 0:
                logger.info(f"Recording {recording_id} completed successfully")
                self.db.update_recording_status(recording_id, 'completed')
            else:
                error_msg = stderr_text[:500] if stderr_text else "Unknown error"
                logger.error(f"Recording {recording_id} failed with code {process.returncode}: {error_msg}")
                self.db.update_recording_status(recording_id, 'failed', error_msg)

            recording = self.db.get_recording(recording_id)
            if recording and recording.get('file_path'):
                file_path = recording['file_path']
                if os.path.exists(file_path):
                    rec_duration = int(time.time() - self.start_times.get(recording_id, time.time()))
                    file_size = os.path.getsize(file_path)
                    self.db.update_recording_file_info(recording_id, rec_duration, file_size)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error monitoring recording {recording_id}: {e}")
        finally:
            self.processes.pop(recording_id, None)
            self.start_times.pop(recording_id, None)

    async def delete_recording(self, recording_id: str) -> bool:
        """Delete a recording and its file."""
        if recording_id in self.processes:
            await self.stop_recording(recording_id)

        recording = self.db.get_recording(recording_id)
        if not recording:
            return False

        if recording.get('file_path') and os.path.exists(recording['file_path']):
            try:
                os.remove(recording['file_path'])
                logger.debug(f"Deleted recording file: {recording['file_path']}")
            except Exception as e:
                logger.error(f"Error deleting file: {e}")

        return self.db.delete_recording(recording_id)

    # =========================================================================
    # Recording Queries
    # =========================================================================

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Get recording info by ID."""
        recording = self.db.get_recording(recording_id)
        return self._enrich_recording(recording) if recording else None

    def get_all_recordings(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all recordings, optionally filtered by status."""
        recordings = self.db.get_all_recordings(status=status)
        return [self._enrich_recording(rec) for rec in recordings]

    def get_active_recordings(self) -> List[Dict[str, Any]]:
        """Get currently active recordings."""
        recordings = self.db.get_all_recordings(status='recording')
        return [self._enrich_recording(rec) for rec in recordings
                if self._is_recording_active(rec)]

    def get_active_recording_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Check if there's an active recording for the given URL."""
        for rec in self.get_active_recordings():
            if rec.get('url') == url:
                return rec
        return None

    def get_pending_recording_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Check if there's a pending (starting or recording) entry for the given URL.

        This includes 'starting' entries that may be stuck from failed attempts.
        """
        all_recordings = self.db.get_all_recordings()
        for rec in all_recordings:
            if rec.get('url') == url and rec.get('status') in ('starting', 'recording'):
                return self._enrich_recording(rec)
        return None

    # =========================================================================
    # Cleanup and Maintenance
    # =========================================================================

    async def cleanup_old_recordings(self):
        """Delete recordings older than retention period."""
        retention = config_store.get("recordings_retention_days", 7)
        old_recordings = self.db.get_old_recordings(retention)
        for recording in old_recordings:
            logger.info(f"Auto-deleting old recording: {recording['id']}")
            await self.delete_recording(recording['id'])

    async def cleanup_loop(self):
        """Periodically clean up old recordings."""
        while True:
            try:
                await self.cleanup_old_recordings()
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
            await asyncio.sleep(3600)

    async def shutdown(self):
        """Gracefully stop all recordings on shutdown."""
        logger.info("Shutting down RecordingManager...")
        for recording_id in list(self.processes.keys()):
            await self.stop_recording(recording_id)
        await self.close()

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _generate_recording_id(self) -> str:
        """Generate a unique recording ID."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        unique_suffix = uuid.uuid4().hex[:8]
        return f"{timestamp}_{unique_suffix}"

    def _generate_filename(self, recording_id: str, name: str) -> str:
        """Generate a safe filename for the recording (MPEG-TS format)."""
        safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()
        safe_name = safe_name.replace(' ', '_')[:50]
        if not safe_name:
            safe_name = "recording"
        return f"{recording_id}_{safe_name}.ts"

    def _is_recording_active(self, recording: Dict[str, Any]) -> bool:
        """Check if a recording is actively running using DB-stored PID."""
        status = recording.get('status')
        if status not in ('recording', 'starting'):
            return False

        pid = recording.get('pid')
        if pid:
            return self.db.is_pid_running(pid)

        if status == 'starting':
            return True

        return recording.get('id') in self.processes

    def _calculate_elapsed(self, started_at: str) -> int:
        """Calculate elapsed seconds from ISO timestamp.

        Note: Database stores naive UTC timestamps, so we use naive comparison.
        """
        try:
            start = datetime.fromisoformat(started_at)
            # Database stores naive UTC, so compare with naive UTC
            now = datetime.utcnow() if start.tzinfo is None else datetime.now(timezone.utc)
            return int((now - start).total_seconds())
        except Exception:
            return 0

    def _enrich_recording(self, recording: Dict[str, Any]) -> Dict[str, Any]:
        """Add computed fields (is_active, elapsed_seconds) to a recording."""
        recording['is_active'] = self._is_recording_active(recording)
        if recording['is_active'] and recording.get('started_at'):
            recording['elapsed_seconds'] = self._calculate_elapsed(recording['started_at'])
        return recording
