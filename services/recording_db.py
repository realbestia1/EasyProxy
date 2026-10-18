import sqlite3
import os
import threading
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class RecordingDB:
    """SQLite database for managing recording metadata with persistent connection."""

    def __init__(self, recordings_dir: str):
        self.db_path = os.path.join(recordings_dir, "recordings.db")
        self._local = threading.local()
        self._init_db()

    def _get_connection(self):
        """Get a persistent connection per thread."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=5000")
        return self._local.conn

    def _init_db(self):
        """Initialize the database schema."""
        conn = self._get_connection()
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

        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recordings_active_url
            ON recordings(url) WHERE status IN ('starting', 'recording')
        """)
        conn.commit()
        logger.debug(f"Recording database initialized at {self.db_path}")

    def _execute(self, sql: str, params=()):
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur

    def create_starting_entry(self, recording_id: str, name: str, url: str) -> bool:
        started_at = datetime.utcnow().isoformat()
        try:
            self._execute("""
                INSERT INTO recordings (id, name, url, status, started_at)
                VALUES (?, ?, ?, 'starting', ?)
            """, (recording_id, name, url, started_at))
            logger.debug(f"Created starting entry: {recording_id} for URL: {url[:80]}...")
            return True
        except sqlite3.IntegrityError:
            logger.debug(f"Duplicate recording attempt for URL: {url[:80]}...")
            return False

    def update_to_recording(self, recording_id: str, file_path: str,
                            headers: str = None, pid: int = None) -> bool:
        cur = self._execute("""
            UPDATE recordings
            SET status = 'recording', file_path = ?, headers = ?, pid = ?
            WHERE id = ? AND status = 'starting'
        """, (file_path, headers, pid, recording_id))
        return cur.rowcount > 0

    def get_recording(self, recording_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_recordings(self, status: str = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT * FROM recordings
                WHERE status = ?
                ORDER BY started_at DESC LIMIT ?
            """, (status, limit))
        else:
            cursor.execute("""
                SELECT * FROM recordings
                ORDER BY started_at DESC LIMIT ?
            """, (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def update_recording_status(self, recording_id: str, status: str,
                                 error_message: str = None) -> bool:
        if status in ('completed', 'failed', 'stopped'):
            stopped_at = datetime.utcnow().isoformat()
            cur = self._execute("""
                UPDATE recordings SET status = ?, stopped_at = ?, error_message = ?
                WHERE id = ?
            """, (status, stopped_at, error_message, recording_id))
        else:
            cur = self._execute("""
                UPDATE recordings SET status = ?, error_message = ?
                WHERE id = ?
            """, (status, error_message, recording_id))
        return cur.rowcount > 0

    def update_recording_file_info(self, recording_id: str,
                                    duration_seconds: int = None,
                                    file_size_bytes: int = None) -> bool:
        cur = self._execute("""
            UPDATE recordings SET duration_seconds = ?, file_size_bytes = ?
            WHERE id = ?
        """, (duration_seconds, file_size_bytes, recording_id))
        return cur.rowcount > 0

    def delete_recording(self, recording_id: str) -> bool:
        cur = self._execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        deleted = cur.rowcount > 0
        if deleted:
            logger.debug(f"Deleted recording entry: {recording_id}")
        return deleted

    def is_pid_running(self, pid: int) -> bool:
        if not pid or pid <= 0:
            return False
        try:
            import psutil
            return psutil.pid_exists(pid)
        except Exception:
            if os.name == "nt":
                return False
            try:
                os.kill(pid, 0)
                return True
            except PermissionError:
                return True
            except OSError:
                return False

    def get_old_recordings(self, days: int) -> List[Dict[str, Any]]:
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM recordings
            WHERE started_at < ? AND status != 'recording'
        """, (cutoff,))
        return [dict(row) for row in cursor.fetchall()]
