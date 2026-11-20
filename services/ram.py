"""Effective RAM for the process, respecting container cgroup limits."""
import os
from pathlib import Path

_TOO_BIG = 1 << 60
_CGROUP_V2_LIMIT = "/sys/fs/cgroup/memory.max"
_CGROUP_V2_USAGE = "/sys/fs/cgroup/memory.current"
_CGROUP_V2_STAT = "/sys/fs/cgroup/memory.stat"
_CGROUP_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_CGROUP_V1_STAT = "/sys/fs/cgroup/memory/memory.stat"


def _read_int(path):
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return 0
    if raw.isdigit() and int(raw) < _TOO_BIG:
        return int(raw)
    return 0


def _cgroup_reclaimable(path, key="inactive_file"):
    """Bytes the kernel can reclaim (file cache) from a memory.stat file."""
    try:
        text = Path(path).read_text()
    except OSError:
        return 0
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == key and value.strip().isdigit():
            return int(value.strip())
    return 0


def total_bytes():
    for path in (_CGROUP_V2_LIMIT, _CGROUP_V1_LIMIT):
        value = _read_int(path)
        if value:
            return value
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        return 0


def available_bytes():
    for limit_path, usage_path, stat_path, key in (
        (_CGROUP_V2_LIMIT, _CGROUP_V2_USAGE, _CGROUP_V2_STAT, "inactive_file"),
        (_CGROUP_V1_LIMIT, _CGROUP_V1_USAGE, _CGROUP_V1_STAT, "total_inactive_file"),
    ):
        limit = _read_int(limit_path)
        if limit:
            usage = _read_int(usage_path)
            reclaimable = _cgroup_reclaimable(stat_path, key)
            return max(0, limit - max(0, usage - reclaimable))
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return 0
