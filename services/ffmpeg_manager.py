
import asyncio
import hashlib
import logging
import os
import shutil
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class FFmpegManager:
    def __init__(self, temp_dir: str = "temp_hls"):
        self.temp_dir = temp_dir
        self.processes: Dict[str, asyncio.subprocess.Process] = {}
        self.access_times: Dict[str, float] = {}
        self.active_streams: Dict[str, str] = {} # url_hash -> full_url
        
        # Ensure temp directory exists
        os.makedirs(self.temp_dir, exist_ok=True)
            
        # Start cleanup task (needs to be scheduled in loop)
        # We'll do lazy cleanup on access for simplicity or rely on external loop calling cleanup()

    def _get_stream_hash(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()

    async def get_stream(self, url: str, headers: dict = None, clearkey: str = None) -> Optional[str]:
        """
        Starts (or returns existing) FFmpeg stream for the URL.
        Returns the relative path to the m3u8 file (stream_id/index.m3u8).
        """
        # Include clearkey in hash to invalidate cache if key changes
        unique_str = f"{url}|{clearkey}" if clearkey else url
        stream_id = hashlib.md5(unique_str.encode()).hexdigest()
        
        stream_dir = os.path.join(self.temp_dir, stream_id)
        playlist_path = os.path.join(stream_dir, "index.m3u8")
        
        self.access_times[stream_id] = time.time()
        
        if stream_id in self.processes:
            # Check if process is still running
            proc = self.processes[stream_id]
            if proc.returncode is None:
                # Process is running, check if playlist exists
                if os.path.exists(playlist_path):
                    return f"{stream_id}/index.m3u8"
                
                # File not found but process triggers. It might be initializing.
                # Wait for it.
                logger.info(f"Stream {stream_id} is initializing. Waiting for playlist...")
                for _ in range(100): # Wait up to 10s
                    if os.path.exists(playlist_path):
                        return f"{stream_id}/index.m3u8"
                    if proc.returncode is not None:
                         # Process died while waiting
                         logger.warning(f"Process {stream_id} died while waiting.")
                         del self.processes[stream_id]
                         break
                    await asyncio.sleep(0.1)
                
                # If still (running) and no file -> Stale/Stuck?
                if proc.returncode is None and not os.path.exists(playlist_path):
                     logger.warning(f"Stream {stream_id} timed out initializing. Restarting.")
                     try:
                        proc.kill()
                     except: pass
                     del self.processes[stream_id]

            else:
                logger.warning(f"FFmpeg process for {stream_id} exitted with {proc.returncode}. Restarting.")
                del self.processes[stream_id]
        
        # Start new stream
        return await self._start_ffmpeg(url, headers, stream_id, clearkey)

    async def _start_ffmpeg(self, url: str, headers: dict, stream_id: str, clearkey: str = None) -> str:
        stream_dir = os.path.join(self.temp_dir, stream_id)
        
        # Clean existing dir if any
        if os.path.exists(stream_dir):
            try:
                shutil.rmtree(stream_dir)
            except Exception as e:
                logger.error(f"Error cleaning stream dir {stream_dir}: {e}")
        
        os.makedirs(stream_dir, exist_ok=True)
        
        playlist_path = os.path.join(stream_dir, "index.m3u8")
        
        # Build command
        headers_str = ""
        if headers:
            valid_headers = {k: v for k, v in headers.items() if k.lower() not in ['host', 'connection', 'accept-encoding']}
            headers_str = "\r\n".join([f"{k}: {v}" for k, v in valid_headers.items()])
        
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",  # Changed to warning to catch issues
            # --- CRITICAL: Timestamp and sync fixes ---
            "-fflags", "+genpts+discardcorrupt+igndts",  # Regenerate PTS, discard corrupt, ignore DTS
            "-analyzeduration", "10000000",  # 10s analyze for proper stream detection
            "-probesize", "10000000",  # 10MB probe for better format detection
            # --- Network resilience ---
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-headers", headers_str,
        ]
        
        # Decryption Key handling - supports multi-key format "KID1:KEY1,KID2:KEY2"
        if clearkey:
            try:
                # Expected format: KID1:KEY1,KID2:KEY2
                # Or just KEY (if single key without KID, legacy)
                keys_to_use = []
                
                if ':' in clearkey:
                     pairs = clearkey.split(',')
                     for pair in pairs:
                         if ':' in pair:
                             _, key = pair.split(':')
                             keys_to_use.append(key.strip())
                         else:
                             # Fallback specific weird cases?
                             pass
                else:
                    keys_to_use.append(clearkey)
                
                for key in keys_to_use:
                    cmd.extend(["-cenc_decryption_key", key])
                
                if keys_to_use:
                    logger.info(f"Added {len(keys_to_use)} decryption key(s) to FFmpeg command")
            except Exception as e:
                logger.error(f"Error parsing clearkey: {e}")

        cmd.extend([
            "-i", url,
            # --- 1080p TRANSCODE for high quality ---
            "-threads", "0",  # Use all CPU cores
            "-vf", "scale=-2:1080",  # Scale to 1080p max height, keep aspect ratio
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-crf", "24",
            "-g", "60",
            "-profile:v", "main",
            # --- AUDIO ---
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-ar", "44100",
            "-bsf:v", "h264_mp4toannexb",
            # --- Timestamp fixes ---
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "2048",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "15",
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_segment_filename", os.path.join(stream_dir, "segment_%03d.ts"),
            playlist_path
        ])
        
        logger.info(f"Starting FFmpeg for {stream_id} with key: {clearkey}")
        logger.info(f"Command: {cmd}")
        
        log_file = open(os.path.join(stream_dir, "ffmpeg.log"), "w")
        log_file.write(f"Command: {cmd}\n\n")
        log_file.flush()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            self.processes[stream_id] = process
            self.active_streams[stream_id] = url
            
            # Wait for the playlist to appear (up to 30 seconds)
            for _ in range(300):  # Wait up to 30 seconds
                if os.path.exists(playlist_path):
                    log_file.close()
                    break
                # Check if process died
                if process.returncode is not None:
                    stdout, stderr = await process.communicate()
                    log_file.write(f"STDERR: {stderr.decode()}\n")
                    log_file.write(f"STDOUT: {stdout.decode()}\n")
                    log_file.close()
                    logger.error(f"FFmpeg process died. Stderr: {stderr.decode()[:500]}")
                    return None
                await asyncio.sleep(0.1)
            else:
                log_file.close()
            
            if not os.path.exists(playlist_path):
                 logger.error("Timeout waiting for playlist generation")
                 # Kill process?
                 try:
                     process.terminate()
                 except: pass
                 return None
                
            return f"{stream_id}/index.m3u8"
            
        except Exception as e:
            logger.error(f"Failed to start FFmpeg: {e}")
            return None

    async def cleanup_loop(self):
        """Periodically checks and terminates idle streams."""
        while True:
            try:
                now = time.time()
                to_remove = []
                
                for stream_id, last_access in list(self.access_times.items()):
                    # Timeout after 2 minutes of inactivity
                    if now - last_access > 120:
                        logger.info(f"Stream {stream_id} idle for 120s. Terminating.")
                        to_remove.append(stream_id)
                
                for stream_id in to_remove:
                    await self._stop_stream(stream_id)
                    
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")
            
            await asyncio.sleep(10)

    async def _stop_stream(self, stream_id: str):
        if stream_id in self.processes:
            proc = self.processes[stream_id]
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
            except Exception as e:
                logger.error(f"Error killing process {stream_id}: {e}")
            del self.processes[stream_id]
            
        if stream_id in self.access_times:
            del self.access_times[stream_id]
        if stream_id in self.active_streams:
            del self.active_streams[stream_id]
            
        # Clean disk
        stream_dir = os.path.join(self.temp_dir, stream_id)
        if os.path.exists(stream_dir):
            try:
                shutil.rmtree(stream_dir)
            except Exception as e:
                logger.error(f"Error removing stream dir {stream_dir}: {e}")

    def touch_stream(self, stream_id: str):
        """Updates last access time for a stream."""
        if stream_id in self.access_times:
            self.access_times[stream_id] = time.time()
