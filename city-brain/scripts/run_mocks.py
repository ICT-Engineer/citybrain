"""Run mock Twin and Guardian in parallel. Ctrl+C to stop both."""

from __future__ import annotations

import signal
import subprocess
import sys
import time


PROCS = [
    ("twin-mock",     ["uvicorn", "mocks.twin_mock:app",     "--port", "8002", "--log-level", "info"]),
    ("guardian-mock", ["uvicorn", "mocks.guardian_mock:app", "--port", "8001", "--log-level", "info"]),
]


def main() -> int:
    running: list[tuple[str, subprocess.Popen]] = []
    for name, cmd in PROCS:
        print(f"starting {name}: {' '.join(cmd)}")
        running.append((name, subprocess.Popen(cmd)))

    def shutdown(*_):
        print("\nshutting down...")
        for name, p in running:
            p.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while True:
            time.sleep(1)
            for name, p in running:
                if p.poll() is not None:
                    print(f"{name} exited with code {p.returncode}")
                    shutdown()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())