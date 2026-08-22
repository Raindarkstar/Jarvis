#!/usr/bin/env python3
"""Jarvis local environment diagnostics.

This module intentionally avoids importing the application modules. A missing
GTK, PortAudio, or wake-word dependency should produce a useful report rather
than preventing the diagnostic command from starting.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str
    optional: bool = False


def _result(name: str, status: str, detail: str, optional: bool = False) -> CheckResult:
    return CheckResult(name=name, status=status, detail=detail, optional=optional)


def _read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without echoing secrets."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def _usable_api_key(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return bool(normalized) and normalized not in {
        "your_dashscope_api_key",
        "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "changeme",
    }


def _check_python() -> CheckResult:
    current = sys.version_info
    version_text = f"{current.major}.{current.minor}.{current.micro}"
    if current < (3, 12):
        return _result("Python", "error", f"当前为 {version_text}，需要 Python 3.12 或更高版本")
    return _result("Python", "ok", f"{version_text}（满足 >= 3.12）")


def _check_import(module_name: str, display_name: str | None = None) -> CheckResult:
    label = display_name or module_name
    try:
        importlib.import_module(module_name)
    except Exception as exc:  # ImportError and native extension loading errors.
        return _result(label, "error", f"导入失败：{exc}")
    return _result(label, "ok", "已安装且可以导入")


def _check_gui() -> list[CheckResult]:
    try:
        # setup-python and some virtual environments do not inherit Debian's
        # system dist-packages path, even though python3-gi is installed there.
        for candidate in (
            Path(f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/dist-packages"),
            Path("/usr/lib/python3/dist-packages"),
        ):
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.append(str(candidate))
        gi = importlib.import_module("gi")
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        importlib.import_module("cairo")
        importlib.import_module("gi.repository.Gtk")
        importlib.import_module("gi.repository.WebKit2")
    except Exception as exc:
        return [_result("GTK/WebKitGTK/Cairo", "error", f"桌面依赖不可用：{exc}")]
    return [_result("GTK/WebKitGTK/Cairo", "ok", "GTK 3、WebKitGTK 4.1 与 Cairo 可用")]


def _check_audio() -> CheckResult:
    try:
        sounddevice = importlib.import_module("sounddevice")
        devices = sounddevice.query_devices()
        inputs = sum(1 for device in devices if device.get("max_input_channels", 0) > 0)
        outputs = sum(1 for device in devices if device.get("max_output_channels", 0) > 0)
    except Exception as exc:
        return _result("音频设备", "warn", f"无法读取 PortAudio 设备：{exc}", optional=True)
    if inputs == 0 or outputs == 0:
        return _result(
            "音频设备",
            "warn",
            f"检测到 {inputs} 个输入、{outputs} 个输出设备；请连接麦克风和扬声器",
            optional=True,
        )
    return _result("音频设备", "ok", f"检测到 {inputs} 个输入、{outputs} 个输出设备")


def _check_command(
    name: str,
    command_lookup: Callable[[str], str | None],
    optional: bool = False,
) -> CheckResult:
    if command_lookup(name):
        return _result(name, "ok", "已找到", optional=optional)
    return _result(
        name,
        "warn" if optional else "error",
        "未找到，请安装对应系统依赖" if optional else "未找到，这是启动所需的系统命令",
        optional=optional,
    )


def _check_aec(project_root: Path, command_lookup: Callable[[str], str | None]) -> CheckResult:
    library = project_root / "libaec.so"
    source = project_root / "aec_engine.c"
    if library.is_file():
        return _result("软件 AEC", "ok", f"已找到 {library.name}")
    if source.is_file() and command_lookup("gcc"):
        return _result("软件 AEC", "warn", "尚未编译 libaec.so；运行 install.sh 可自动编译", optional=True)
    return _result("软件 AEC", "warn", "未找到可用的 fallback AEC，PipeWire AEC 仍可独立工作", optional=True)


def _check_pipewire_aec(command_lookup: Callable[[str], str | None]) -> CheckResult:
    library_candidates = (
        Path("/usr/lib/x86_64-linux-gnu/spa-0.2/aec/libspa-aec-webrtc.so"),
        Path("/usr/lib/aarch64-linux-gnu/spa-0.2/aec/libspa-aec-webrtc.so"),
        Path("/usr/lib/spa-0.2/aec/libspa-aec-webrtc.so"),
    )
    missing_commands = [name for name in ("pw-cli", "pw-cat", "pw-dump") if not command_lookup(name)]
    library = next((path for path in library_candidates if path.is_file()), None)
    if not missing_commands and library:
        return _result("PipeWire AEC", "ok", f"已找到 {library}")
    if missing_commands:
        detail = f"缺少命令：{', '.join(missing_commands)}"
    else:
        detail = "未找到 libspa-aec-webrtc.so"
    return _result("PipeWire AEC", "warn", f"{detail}，将使用其他音频链路", optional=True)


def collect_checks(
    project_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    command_lookup: Callable[[str], str | None] = shutil.which,
) -> list[CheckResult]:
    root = Path(project_root or PROJECT_ROOT)
    env = dict(os.environ if environ is None else environ)
    env_file = _read_env_file(root / ".env")
    api_key = env.get("DASHSCOPE_API_KEY") or env_file.get("DASHSCOPE_API_KEY")
    session_type = env.get("XDG_SESSION_TYPE", "").strip().lower()

    results = [_check_python()]
    if platform.system() != "Linux":
        results.append(_result("操作系统", "error", f"当前为 {platform.system()}，Jarvis 目前只支持 Linux"))
    else:
        results.append(_result("操作系统", "ok", f"Linux {platform.release()}"))

    if _usable_api_key(api_key):
        results.append(_result("DashScope API Key", "ok", "已配置（密钥内容不会显示）"))
    else:
        results.append(_result("DashScope API Key", "error", "未配置，请在 .env 中填写 DASHSCOPE_API_KEY"))

    for module_name, display_name in (
        ("dashscope", "DashScope SDK"),
        ("numpy", "NumPy"),
        ("onnxruntime", "ONNX Runtime"),
        ("openwakeword", "OpenWakeWord"),
        ("PIL", "Pillow"),
        ("dotenv", "python-dotenv"),
        ("sounddevice", "sounddevice"),
        ("webrtcvad", "WebRTC VAD"),
    ):
        results.append(_check_import(module_name, display_name))

    results.extend(_check_gui())
    results.append(_check_audio())
    results.append(_check_command("xdg-open", command_lookup))
    results.append(_check_command("gcc", command_lookup, optional=True))
    results.append(_check_aec(root, command_lookup))
    results.append(_check_pipewire_aec(command_lookup))

    if session_type == "wayland":
        if command_lookup("ydotool") or command_lookup("wlrctl"):
            results.append(_result("Wayland 输入", "ok", "检测到 Wayland 与兼容输入工具"))
        else:
            results.append(_result("Wayland 输入", "warn", "未找到 ydotool/wlrctl，GUI 模拟操作可能受限", optional=True))
    elif session_type == "x11":
        if command_lookup("xdotool"):
            results.append(_result("X11 输入", "ok", "检测到 xdotool"))
        else:
            results.append(_result("X11 输入", "warn", "未找到 xdotool，GUI 模拟操作可能受限", optional=True))
    else:
        results.append(_result("桌面会话", "warn", "无法识别 X11/Wayland，会影响 GUI 模拟诊断", optional=True))

    return results


def exit_code(results: Sequence[CheckResult], strict: bool = False) -> int:
    if any(item.status == "error" for item in results):
        return 1
    if strict and any(item.status == "warn" for item in results):
        return 1
    return 0


def _print_human(results: Sequence[CheckResult]) -> None:
    labels = {"ok": "PASS", "warn": "WARN", "error": "FAIL"}
    print("Jarvis 环境诊断")
    print("=" * 24)
    for item in results:
        suffix = "（可选）" if item.optional else ""
        print(f"[{labels[item.status]}] {item.name}{suffix}: {item.detail}")
    code = exit_code(results)
    print("=" * 24)
    print("诊断结果：" + ("通过" if code == 0 else "存在需要处理的问题"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis doctor", description="诊断 Jarvis 的 Linux 运行环境")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--strict", action="store_true", help="将警告也视为失败")
    args = parser.parse_args(argv)
    results = collect_checks()
    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        _print_human(results)
    return exit_code(results, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
