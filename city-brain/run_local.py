"""Start the City Brain alone: one program on http://127.0.0.1:8300

    python run_local.py              # then open http://127.0.0.1:8300/city

The Brain runs its own built-in Twin and Guardian simulators (at /sim/twin and /sim/guardian
on the same port). It does not connect to the real Twin or Guardian layers. To point it at
them later, see INTEGRATION.md (SIMULATOR=false, TWIN_URL, GUARDIAN_URL, tokens).
"""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"}
env.setdefault("SIMULATOR", "true")
env.setdefault("PORT", "8300")
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "city_brain.main:app", "--host", "127.0.0.1",
                         "--port", env["PORT"], "--log-level", "warning"], cwd=ROOT, env=env)
time.sleep(2.5)
print(f"\n  City Brain: http://127.0.0.1:{env['PORT']}/city")
print("  Standalone: built-in Twin and Guardian simulators, no connection to any other layer.")
print("  (close this window or press Ctrl+C to stop)\n", flush=True)
try:
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
