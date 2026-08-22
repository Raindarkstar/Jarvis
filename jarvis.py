#!/usr/bin/env python3
"""Start Jarvis as a wake-word-driven background voice assistant.

When launched from a clone, this script automatically switches to the virtual
environment created by ``install.ps1`` or ``install.sh``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def _venv_python() -> Path:
    if os.name == "nt":
        return PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return PROJECT_ROOT / ".venv" / "bin" / "python"


def main() -> int:
    project_python = _venv_python()
    if project_python.is_file() and Path(sys.executable).resolve() != project_python.resolve():
        os.execv(str(project_python), [str(project_python), str(Path(__file__).resolve()), *sys.argv[1:]])

    from jarvis_cli import voice_main

    return voice_main()


if __name__ == "__main__":
    raise SystemExit(main())
