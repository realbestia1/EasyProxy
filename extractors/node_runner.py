"""Shared runner for the extractor-side Node.js helpers in ``scripts/*.mjs``.

Each runner takes its arguments on argv, reads its proxy from an environment
variable and prints JSON lines; the last object is the result (an ``error``
key means it failed).
"""

import asyncio
import json
import os
import shutil

from extractors.base import ExtractorError


def node_bin() -> str | None:
    return shutil.which("node")


async def run_node_runner(runner_path: str, args: list, env: dict | None = None,
                          timeout: float = 60.0, label: str = "node",
                          node_flags: tuple = ()) -> dict:
    """Run one of ``scripts/*.mjs`` and return the last JSON object it printed."""
    node = node_bin()
    if not node:
        raise ExtractorError(f"{label}: Node.js is required for this extractor")
    if not os.path.exists(runner_path):
        raise ExtractorError(f"{label}: runner script not found at {runner_path}")

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            node, *node_flags, runner_path, *[str(a) for a in args],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env if env is not None else dict(os.environ),
            start_new_session=os.name != "nt",
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError as exc:
        raise ExtractorError(f"{label}: failed to start Node.js: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise ExtractorError(
            f"{label}: resolver timed out after {timeout:.0f} seconds") from exc
    finally:
        if proc and proc.returncode is None:
            try:
                if os.name != "nt":
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except BaseException:
                pass

    raw = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
    detail = stderr.decode("utf-8", errors="replace")[-400:] if stderr else raw[-400:]
    for line in reversed(raw.splitlines()):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            if candidate.get("error"):
                raise ExtractorError(f"{label}: {candidate['error']}")
            return candidate
    raise ExtractorError(f"{label}: runner produced no result ({detail})")
