import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class RecordingDB:
    """SQLite database for managing recording metadata."""

    def __init__(self, recordings_dir: str):
        self.db_path = os.path.join(recordings_dir, "recordings.db")
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_db(self):
        """Initialize the database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    file_path TEXT,
                    status TEXT NOT NULL DEFAULT 'recording',
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    duration_seconds INTEGER,
                    file_size_bytes INTEGER,
                    error_message TEXT,
                    headers TEXT,
                    clearkey TEXT,
                    pid INTEGER
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_recordings_status
                ON recordings(status)
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_recordings_started_at
                ON recordings(started_at)
            """)

            # Unique index to prevent duplicate active recordings for the same URL
            # This covers 'starting' (extraction in progress) and 'recording' (actively recording)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recordings_active_url
                ON recordings(url) WHERE status IN ('starting', 'recording')
            """)

            logger.debug("Recording database initialized at %s", self.db_path)

    def create_starting_entry(self, recording_id: str, name: str, url: str) -> bool:
        """Create a 'starting' entry to claim the lock before extraction.

        Returns True if created, False if duplicate (another recording for this URL exists).
        Uses the unique index on (url) WHERE status IN ('starting', 'recording') to prevent duplicates.
        """
        started_at = datetime.utcnow().isoformat()

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO recordings (id, name, url, status, started_at)
                    VALUES (?, ?, ?, 'starting', ?)
                """, (recording_id, name, url, started_at))
            logger.debug("Created starting entry: %s for URL: %s...", recording_id, url[:80])
            return True
        except sqlite3.IntegrityError as e:
            # Unique constraint violation - another recording for this URL exists
            logger.debug("Duplicate recording attempt for URL: %s... - %s", url[:80], e)
            return False

    def update_to_recording(self, recording_id: str, file_path: str,
                            headers: str = None, pid: int = None) -> bool:
        """Update a 'starting' entry to 'recording' after extraction succeeds."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE recordings
                SET status = 'recording', file_path = ?, headers = ?, pid = ?
                WHERE id = ? AND status = 'starting'
            """, (file_path, headers, pid, recording_id))
            return cursor.rowcount > 0

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        """Get a recording by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM recordings WHERE id = ?",
                           (recording_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

    def get_all_recordings(self, status: str = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        """Get all recordings, optionally filtered by status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if status:
                cursor.execute("""
                    SELECT * FROM recordings
                    WHERE status = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                """, (status, limit))
            else:
                cursor.execute("""
                    SELECT * FROM recordings
                    ORDER BY started_at DESC
                    LIMIT ?
                """, (limit,))

            return [dict(row) for row in cursor.fetchall()]

    def get_active_recordings(self) -> List[Dict[str, Any]]:
        """Get all currently recording entries."""
        return self.get_all_recordings(status='recording')

    def update_recording_status(self, recording_id: str, status: str,
                                error_message: str = None) -> bool:
        """Update recording status."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if status in ('completed', 'failed', 'stopped'):
                stopped_at = datetime.utcnow().isoformat()
                cursor.execute("""
                    UPDATE recordings
                    SET status = ?, stopped_at = ?, error_message = ?
                    WHERE id = ?
                """, (status, stopped_at, error_message, recording_id))
            else:
                cursor.execute("""
                    UPDATE recordings
                    SET status = ?, error_message = ?
                    WHERE id = ?
                """, (status, error_message, recording_id))

            return cursor.rowcount > 0

    def update_recording_file_info(self, recording_id: str,
                                   duration_seconds: int = None,
                                   file_size_bytes: int = None) -> bool:
        """Update file information after recording completes."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE recordings
                SET duration_seconds = ?, file_size_bytes = ?
                WHERE id = ?
            """, (duration_seconds, file_size_bytes, recording_id))
            return cursor.rowcount > 0

    def delete_recording(self, recording_id: str) -> bool:
        """Delete a recording entry."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM recordings WHERE id = ?",
                           (recording_id,))
            deleted = cursor.rowcount > 0
            if deleted:
                logger.debug("Deleted recording entry: %s", recording_id)
            return deleted

    def get_old_recordings(self, days: int) -> List[Dict[str, Any]]:
        """Get recordings older than specified days."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM recordings
                WHERE started_at < ? AND status != 'recording'
            """, (cutoff,))
            return [dict(row) for row in cursor.fetchall()]

    def is_pid_running(self, pid: int) -> bool:
        """Check if a process with given PID is still running."""
        if not pid:
            return False
        try:
            import os
            os.kill(pid, 0)  # Signal 0 just checks if process exists
            return True
        except OSError:
            return False
