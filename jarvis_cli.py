#!/usr/bin/env python3
"""Jarvis command-line entry points.

The desktop and voice clients remain deliberately lazy-loaded: ``jarvis doctor``
must be usable even when a GUI or audio dependency is not installed yet.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import sysconfig
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Sequence


def _version() -> str:
    try:
        return version("jarvis-linux-assistant")
    except PackageNotFoundError:
        return "0.1.0"


def _run_module(module_name: str) -> int:
    """Run one of the existing applications as if invoked as a script."""
    project_root = str(Path(__file__).resolve().parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    os.environ["JARVIS_RESOURCE_DIR"] = str(_resource_dir())
    runpy.run_module(module_name, run_name="__main__")
    return 0


def _resource_dir() -> Path:
    """Locate source-tree resources or resources installed under share/jarvis."""
    configured = os.getenv("JARVIS_RESOURCE_DIR")
    if configured and (Path(configured) / "client_ui.html").is_file():
        return Path(configured)

    source_dir = Path(__file__).resolve().parent
    if (source_dir / "client_ui.html").is_file():
        return source_dir

    data_root = Path(sysconfig.get_path("data") or sys.prefix)
    installed_dir = data_root / "share" / "jarvis"
    if (installed_dir / "client_ui.html").is_file():
        return installed_dir
    return source_dir


def doctor_main(argv: Sequence[str] | None = None) -> int:
    from doctor import main as doctor_command

    return doctor_command(argv)


def voice_main() -> int:
    return _run_module("rain_ai")


def _desktop_module() -> str:
    return "windows_client" if os.name == "nt" else "client_app"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Jarvis 唤醒词语音助手命令行入口",
    )
    parser.add_argument("--version", action="version", version=_version())
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("desktop", help="启动可选的桌面客户端")
    subparsers.add_parser("voice", help="启动后台唤醒与实时语音服务（默认）")
    doctor_parser = subparsers.add_parser("doctor", help="诊断运行环境")
    doctor_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    doctor_parser.add_argument("--strict", action="store_true", help="将警告也视为失败")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "doctor":
        doctor_args = []
        if args.json:
            doctor_args.append("--json")
        if args.strict:
            doctor_args.append("--strict")
        return doctor_main(doctor_args)
    if args.command == "voice":
        return voice_main()
    if args.command == "desktop":
        return _run_module(_desktop_module())
    return voice_main()


if __name__ == "__main__":
    raise SystemExit(main())
