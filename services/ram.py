"""Effective RAM for the process, respecting container cgroup limits."""
import os
from pathlib import Path

_TOO_BIG = 1 << 60
_CGROUP_V2_LIMIT = "/sys/fs/cgroup/memory.max"
_CGROUP_V2_USAGE = "/sys/fs/cgroup/memory.current"
_CGROUP_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"


def _read_int(path):
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return 0
    if raw.isdigit() and int(raw) < _TOO_BIG:
        return int(raw)
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
    for limit_path, usage_path in ((_CGROUP_V2_LIMIT, _CGROUP_V2_USAGE),
                                   (_CGROUP_V1_LIMIT, _CGROUP_V1_USAGE)):
        limit = _read_int(limit_path)
        if limit:
            usage = _read_int(usage_path)
            return max(0, limit - usage)
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return 0
