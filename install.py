#!/usr/bin/env python3
"""Install workspaces-venv on this Windows user (run from repo root after git clone).

Steps:
  1. git clone https://github.com/amirdeadline/workspaces-venv.git
  2. cd workspaces-venv
  3. copy scripts\\venv.config.json.example scripts\\venv.config.json
  4. Edit scripts\\venv.config.json
  5. python install.py

Forwards to scripts/install.py and defaults --path to this repository root.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
INSTALLER = REPO_ROOT / "scripts" / "install.py"


def main() -> int:
    if not INSTALLER.is_file():
        raise SystemExit(f"Missing installer: {INSTALLER}")
    argv = list(sys.argv[1:])
    has_path = "--path" in argv or any(arg.startswith("--path=") for arg in argv)
    if not has_path:
        argv = ["--path", str(REPO_ROOT), *argv]
    proc = subprocess.run([sys.executable, str(INSTALLER), *argv], check=False)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
