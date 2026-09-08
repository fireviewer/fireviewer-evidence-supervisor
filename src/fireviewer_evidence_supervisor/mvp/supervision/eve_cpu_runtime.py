"""Run Eve and its loopback Python supervision API in one CPU container."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def _wait_for_loopback(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("the point supervision API stopped during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("the point supervision API did not become ready")


def main() -> int:
    eve_root = Path(os.getenv("FIREVIEWER_EVE_ROOT", "/opt/eve-point-supervisor"))
    public_port = int(os.getenv("PORT", "8080"))
    supervision_port = int(os.getenv("FIREVIEWER_SUPERVISION_PORT", "8091"))
    node_executable = Path(
        os.getenv("FIREVIEWER_NODE_EXECUTABLE", "/usr/local/bin/node")
    )
    eve_entrypoint = Path(
        os.getenv(
            "FIREVIEWER_EVE_ENTRYPOINT",
            "/opt/eve-point-supervisor/.output/server/index.mjs",
        )
    )
    if not node_executable.is_absolute():
        raise ValueError("FIREVIEWER_NODE_EXECUTABLE must be an absolute path")
    if not eve_entrypoint.is_absolute():
        raise ValueError("FIREVIEWER_EVE_ENTRYPOINT must be an absolute path")
    supervision = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "fireviewer_evidence_supervisor.mvp.supervision.point_supervisor_cpu_service",
        ],
        cwd=eve_root,
    )
    eve: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: object) -> None:
        for process in (eve, supervision):
            if process is not None and process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        _wait_for_loopback(supervision_port, supervision)
        eve_environment = os.environ.copy()
        eve_environment["HOST"] = "0.0.0.0"  # noqa: S104
        eve_environment["PORT"] = str(public_port)
        eve = subprocess.Popen(  # noqa: S603
            [
                str(node_executable),
                str(eve_entrypoint),
            ],
            cwd=eve_root,
            env=eve_environment,
        )
        while True:
            if eve.poll() is not None:
                return int(eve.returncode or 0)
            if supervision.poll() is not None:
                return int(supervision.returncode or 1)
            time.sleep(0.5)
    finally:
        stop(signal.SIGTERM, None)
        for process in (eve, supervision):
            if process is not None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
