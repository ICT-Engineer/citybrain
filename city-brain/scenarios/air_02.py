"""Test Book scenario air-02. One command, one log in logs/ (R2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[1:1] = ["--scenario", "air-02"]
    sys.exit(main())
