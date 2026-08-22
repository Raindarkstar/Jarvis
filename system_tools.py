import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Union


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_SHELL_COMMANDS = _env_flag("RAIN_ALLOW_SHELL_COMMANDS")
ALLOW_DESTRUCTIVE_ACTIONS = _env_flag("RAIN_ALLOW_DESTRUCTIVE_ACTIONS")


# ==========================================================
# 1. 终端指令与脚本执行 (Shell Command Execution)
# ==========================================================

def execute_shell_command(command: str, timeout: int = 20, working_dir: str = None) -> str:
    """在电脑上执行任意 Linux Shell / Bash 命令并返回标准输出。"""
    if not ALLOW_SHELL_COMMANDS:
        return (
            "Shell 命令执行默认已禁用。若确实需要此能力，请在 .env 中设置 "
            "RAIN_ALLOW_SHELL_COMMANDS=1 后重启 Jarvis。"
        )
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        return f"无效的超时秒数: {timeout}"
    if timeout < 1 or timeout > 120:
        return "超时秒数必须在 1 到 120 之间"
    try:
        cwd = working_dir if working_dir and os.path.exists(working_dir) else os.path.expanduser("~")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            executable="/bin/bash",
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        output_parts = []
        if stdout:
            if len(stdout) > 2000:
                stdout = stdout[:2000] + "\n... (输出过长已截断)"
            output_parts.append(stdout)
        if stderr:
            if len(stderr) > 1000:
                stderr = stderr[:1000] + "\n... (错误输出过长已截断)"
            output_parts.append(f"错误输出: {stderr}")

        status = "成功" if result.returncode == 0 else "失败"
        if not output_parts:
            return f"命令执行{status} (退出码: {result.returncode}，无输出)"
        output_parts.append(f"退出码: {result.returncode}")
        return "\n".join(output_parts)
    except subprocess.TimeoutExpired:
        return f"命令执行超时 ({timeout} 秒已到)"
    except Exception as e:
        return f"命令执行失败: {str(e)}"


# ==========================================================
# 2. 应用程序与网页管理 (Application & Web Launcher)
# ==========================================================

APP_ALIASES = {
    "chrome": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
    "浏览器": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "firefox"],
    "browser": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "firefox"],
    "code": ["code", "cursor"],
    "vscode": ["code", "cursor"],
    "编辑器": ["code", "cursor", "gedit", "kate", "mousepad"],
    "terminal": ["gnome-terminal", "x-terminal-emulator", "konsole", "alacritty", "kitty", "xterm"],
    "终端": ["gnome-terminal", "x-terminal-emulator", "konsole", "alacritty", "kitty", "xterm"],
    "files": ["nautilus", "dolphin", "thunar", "nemo"],
    "explorer": ["nautilus", "dolphin", "thunar", "nemo"],
    "文件管理器": ["nautilus", "dolphin", "thunar", "nemo"],
    "calculator": ["gnome-calculator", "kcalc", "xcalc"],
    "计算器": ["gnome-calculator", "kcalc", "xcalc"],
    "settings": ["gnome-control-center", "systemsettings"],
    "设置": ["gnome-control-center", "systemsettings"],
    "music": ["rhythmbox", "spotify", "vlc"],
    "音乐": ["rhythmbox", "spotify", "vlc"],
    "text_editor": ["gedit", "kate", "mousepad", "code"],
}

WEBSITE_ALIASES = {
    "百度": "https://www.baidu.com",
    "baidu": "https://www.baidu.com",
    "必应": "https://www.bing.com",
    "bing": "https://www.bing.com",
    "谷歌": "https://www.google.com",
    "google": "https://www.google.com",
    "b站": "https://www.bilibili.com",
    "哔哩哔哩": "https://www.bilibili.com",
    "bilibili": "https://www.bilibili.com",
    "油管": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "知乎": "https://www.zhihu.com",
    "zhihu": "https://www.zhihu.com",
    "微博": "https://weibo.com",
    "淘宝": "https://www.taobao.com",
    "京东": "https://www.jd.com",
}


def normalize_web_url(value: str) -> str:
    """把站点别名、裸域名和 URL 规范化为可安全打开的 HTTP(S) URL。"""
    target = str(value or "").strip().strip("。！？，,;；")
    if not target:
        return ""
    alias = WEBSITE_ALIASES.get(target.lower()) or WEBSITE_ALIASES.get(target)
    if alias:
        return alias
    if target.startswith("www."):
        target = f"https://{target}"
    elif re.match(r"^(?:localhost|(?:\d{1,3}\.){3}\d{1,3})(?::\d+)?(?:/.*)?$", target, re.I):
        target = f"http://{target}"
    elif re.match(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#].*)?$", target, re.I):
        target = f"https://{target}"
    try:
        parsed = urllib.parse.urlsplit(target)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urllib.parse.urlunsplit(parsed)


def _run_checked(command: List[str], timeout: int = 10) -> tuple[bool, str]:
    """运行一个短命令，并以真实退出码判断成功。"""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    error = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, error


def _run_first_available(commands: List[List[str]]) -> tuple[bool, str]:
    errors = []
    found = False
    for command in commands:
        if not command or not shutil.which(command[0]):
            continue
        found = True
        success, error = _run_checked(command)
        if success:
            return True, ""
        errors.append(f"{command[0]}: {error or '执行失败'}")
    if not found:
        return False, "系统中没有可用的控制程序"
    return False, "; ".join(errors)


def _open_desktop_target(target: str) -> tuple[bool, str]:
    if not shutil.which("xdg-open"):
        return False, "系统中未安装 xdg-open"
    return _run_checked(["xdg-open", target])


def _launch_detached(command: List[str]) -> tuple[bool, str]:
    """启动 GUI 程序；立即崩溃算失败，持续运行或正常退出算成功。"""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        time.sleep(0.15)
        code = process.poll()
        if code is None or code == 0:
            return True, ""
        error = process.stderr.read().strip() if process.stderr else ""
        return False, error or f"退出码 {code}"
    except OSError as exc:
        return False, str(exc)


def open_application(app_name: str = "", path_or_url: str = "") -> str:
    """打开指定的电脑应用、已有文件/目录或 HTTP(S) 网站。"""
    app_name = str(app_name or "").strip()
    path_or_url = str(path_or_url or "").strip()

    url = normalize_web_url(path_or_url) or normalize_web_url(app_name)
    if url:
        success, error = _open_desktop_target(url)
        return f"已在浏览器中打开网址: {url}" if success else f"打开网址失败: {error}"

    if path_or_url:
        expanded = os.path.abspath(os.path.expanduser(path_or_url))
        if not os.path.exists(expanded):
            return f"要打开的文件、目录或网址不存在/无效: {path_or_url}"
        success, error = _open_desktop_target(expanded)
        return f"已打开文件/目录: {expanded}" if success else f"打开文件/目录失败: {error}"

    if not app_name:
        return "未指定要打开的应用、文件或网站"

    if app_name.lower() in {"browser", "浏览器"}:
        success, error = _open_desktop_target("https://www.baidu.com")
        return "已打开默认浏览器" if success else f"打开默认浏览器失败: {error}"
    if app_name.lower() in {"files", "explorer", "文件管理器"}:
        home_directory = os.path.expanduser("~")
        success, error = _open_desktop_target(home_directory)
        return "已打开文件管理器" if success else f"打开文件管理器失败: {error}"

    candidates = APP_ALIASES.get(app_name.lower(), APP_ALIASES.get(app_name, []))
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable:
            success, error = _launch_detached([executable])
            if success:
                return f"已成功启动 {app_name} ({candidate})"
            return f"启动 {app_name} 失败: {error}"

    executable = shutil.which(app_name)
    if executable:
        success, error = _launch_detached([executable])
        return f"已成功启动程序: {app_name}" if success else f"启动程序失败: {error}"

    if shutil.which("gtk-launch"):
        success, error = _run_checked(["gtk-launch", app_name])
        if success:
            return f"已通过桌面启动器打开: {app_name}"
        return f"未能启动应用 {app_name}: {error or '桌面启动器返回失败'}"
    return f"未能找到应用: {app_name}"


# ==========================================================
# 3. 硬件与系统控制 (System & Hardware Control)
# ==========================================================

def control_system(action: str, value: str = "") -> str:
    """控制系统硬件（音量、亮度、媒体播放、锁屏、关机、睡眠等）。"""
    act = action.strip().lower()

    if act in ["sleep", "suspend", "reboot", "restart", "shutdown", "power_off"]:
        if not ALLOW_DESTRUCTIVE_ACTIONS:
            return (
                "睡眠、重启和关机操作默认已禁用。若确实需要此能力，请在 .env 中设置 "
                "RAIN_ALLOW_DESTRUCTIVE_ACTIONS=1 后重启 Jarvis。"
            )

    def parse_percent(raw: str, default: int, minimum: int = 0, maximum: int = 100) -> int:
        cleaned = str(raw or "").replace("%", "").strip()
        if not cleaned:
            return default
        parsed = int(cleaned)
        if parsed < minimum or parsed > maximum:
            raise ValueError
        return parsed

    commands = []
    success_message = ""
    if act in ["volume_up", "vol_up"]:
        try:
            delta = f"{parse_percent(value, 5, 1, 100)}%"
        except ValueError:
            return f"无效的音量步长: {value}"
        commands = [
            ["wpctl", "set-volume", "-l", "1.5", "@DEFAULT_AUDIO_SINK@", f"{delta}+"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"+{delta}"],
            ["amixer", "-D", "pulse", "sset", "Master", f"{delta}+"],
        ]
        success_message = f"已调大音量 ({delta})"
    elif act in ["volume_down", "vol_down"]:
        try:
            delta = f"{parse_percent(value, 5, 1, 100)}%"
        except ValueError:
            return f"无效的音量步长: {value}"
        commands = [
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{delta}-"],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"-{delta}"],
            ["amixer", "-D", "pulse", "sset", "Master", f"{delta}-"],
        ]
        success_message = f"已调小音量 ({delta})"
    elif act in ["volume_set", "set_volume"]:
        try:
            percent = parse_percent(value, 0, 0, 150)
        except ValueError:
            return f"无效的音量数值: {value}"
        commands = [
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", str(percent / 100.0)],
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
            ["amixer", "-D", "pulse", "sset", "Master", f"{percent}%"],
        ]
        success_message = f"音量已设置为 {percent}%"
    elif act in ["mute", "volume_mute"]:
        commands = [
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
            ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"],
            ["amixer", "-D", "pulse", "sset", "Master", "mute"],
        ]
        success_message = "系统已静音"
    elif act in ["unmute", "volume_unmute"]:
        commands = [
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
            ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
            ["amixer", "-D", "pulse", "sset", "Master", "unmute"],
        ]
        success_message = "系统已取消静音"
    elif act in ["brightness_up"]:
        commands = [["brightnessctl", "set", "+10%"]]
        success_message = "已调高屏幕亮度"
    elif act in ["brightness_down"]:
        commands = [["brightnessctl", "set", "10%-"]]
        success_message = "已调低屏幕亮度"
    elif act in ["brightness_set"]:
        try:
            percent = parse_percent(value, 50, 1, 100)
        except ValueError:
            return f"无效的亮度数值: {value}"
        commands = [["brightnessctl", "set", f"{percent}%"]]
        success_message = f"屏幕亮度已设置为 {percent}%"
    elif act in ["media_play_pause", "play", "pause"]:
        commands = [["playerctl", "play-pause"], ["xdotool", "key", "XF86AudioPlay"]]
        success_message = "已切换媒体播放/暂停"
    elif act in ["media_next", "next_track"]:
        commands = [["playerctl", "next"], ["xdotool", "key", "XF86AudioNext"]]
        success_message = "已切换到下一曲"
    elif act in ["media_prev", "prev_track"]:
        commands = [["playerctl", "previous"], ["xdotool", "key", "XF86AudioPrev"]]
        success_message = "已切换到上一曲"
    elif act in ["lock_screen", "lock"]:
        commands = [
            ["xdg-screensaver", "lock"],
            ["loginctl", "lock-session"],
            ["gnome-screensaver-command", "-l"],
        ]
        success_message = "电脑屏幕已锁定"
    elif act in ["sleep", "suspend"]:
        commands = [["systemctl", "suspend"]]
        success_message = "系统正在进入睡眠模式"
    elif act in ["reboot", "restart"]:
        commands = [["systemctl", "reboot"]]
        success_message = "系统正在准备重启"
    elif act in ["shutdown", "power_off"]:
        commands = [["systemctl", "poweroff"]]
        success_message = "系统正在准备关机"
    else:
        return f"未知的系统操作: {action}"

    success, error = _run_first_available(commands)
    return success_message if success else f"系统操作失败: {error}"


# ==========================================================
# 4. 键盘鼠标与 GUI 自动化 (Keyboard & Mouse Automation)
# ==========================================================

class X11InputSimulator:
    def __init__(self):
        self.x11 = None
        self.xtst = None
        self.display = None
        try:
            self.x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            self.xtst = ctypes.cdll.LoadLibrary("libXtst.so.6")
            self.x11.XOpenDisplay.restype = ctypes.c_void_p
            self.display = self.x11.XOpenDisplay(None)
        except Exception:
            pass

    def send_key_combination(self, combo: str) -> bool:
        if not self.display or not self.xtst:
            try:
                result = subprocess.run(
                    ["xdotool", "key", combo], capture_output=True, check=False
                )
                return result.returncode == 0
            except OSError:
                return False

        keys = [k.strip().lower() for k in combo.split("+")]
        key_codes = []

        KEY_MAP = {
            "ctrl": 0xFFE3,
            "control": 0xFFE3,
            "alt": 0xFFE9,
            "shift": 0xFFE1,
            "super": 0xFFEB,
            "win": 0xFFEB,
            "enter": 0xFF0D,
            "return": 0xFF0D,
            "escape": 0xFF1B,
            "esc": 0xFF1B,
            "tab": 0xFF09,
            "space": 0x0020,
            "backspace": 0xFF08,
            "delete": 0xFFFF,
            "up": 0xFF52,
            "down": 0xFF54,
            "left": 0xFF51,
            "right": 0xFF53,
        }

        try:
            for k in keys:
                if k in KEY_MAP:
                    keysym = KEY_MAP[k]
                elif len(k) == 1:
                    keysym = ord(k)
                else:
                    keysym = self.x11.XStringToKeysym(ctypes.c_char_p(k.encode("utf-8")))

                keycode = self.x11.XKeysymToKeycode(ctypes.c_void_p(self.display), ctypes.c_ulong(keysym))
                if keycode != 0:
                    key_codes.append(keycode)

            if len(key_codes) != len(keys):
                return False
            for code in key_codes:
                self.xtst.XTestFakeKeyEvent(ctypes.c_void_p(self.display), ctypes.c_uint(code), ctypes.c_int(1), ctypes.c_ulong(0))
            self.x11.XFlush(ctypes.c_void_p(self.display))
            time.sleep(0.05)
            for code in reversed(key_codes):
                self.xtst.XTestFakeKeyEvent(ctypes.c_void_p(self.display), ctypes.c_uint(code), ctypes.c_int(0), ctypes.c_ulong(0))
            self.x11.XFlush(ctypes.c_void_p(self.display))
            return True
        except Exception:
            return False

    def type_text(self, text: str) -> bool:
        try:
            res = subprocess.run(
                ["xdotool", "type", "--", text], capture_output=True, check=False
            )
            if res.returncode == 0:
                return True
        except OSError:
            pass
        return all(self.send_key_combination(ch) for ch in text)


_simulator = X11InputSimulator()


def _remove_leading_dictation_period(text: str) -> str:
    """清除语音转写偶尔附加在打字正文前的单个中文句号。"""
    value = str(text or "")
    leading_space = value[: len(value) - len(value.lstrip())]
    body = value.lstrip()
    if len(body) > 1 and body.startswith("。"):
        body = body[1:].lstrip()
    return leading_space + body


def gui_control(action: str, key: str = "", text: str = "", x: int = None, y: int = None) -> str:
    act = action.strip().lower()

    if act in ["hotkey", "key", "press"]:
        target_key = key if key else text
        if not target_key:
            return "未指定按键或快捷键"
        success = _simulator.send_key_combination(target_key)
        return f"已发送快捷键: {target_key}" if success else f"快捷键发送失败: {target_key}"

    elif act in ["type", "write", "input"]:
        if not text:
            return "未指定要输入的文本"
        text_to_type = _remove_leading_dictation_period(text)
        success = _simulator.type_text(text_to_type)
        return f"已输入文本: {text_to_type}" if success else "文本输入失败"

    elif act in ["click", "left_click"]:
        if x is not None and y is not None:
            try:
                safe_x, safe_y = int(x), int(y)
            except (TypeError, ValueError):
                return f"无效的鼠标坐标: ({x}, {y})"
            success, error = _run_checked(
                ["xdotool", "mousemove", str(safe_x), str(safe_y), "click", "1"]
            )
            return (
                f"已在坐标 ({safe_x}, {safe_y}) 点击鼠标左键"
                if success else f"鼠标点击失败: {error}"
            )
        success, error = _run_checked(["xdotool", "click", "1"])
        return "已在当前位置点击鼠标左键" if success else f"鼠标点击失败: {error}"

    elif act in ["right_click"]:
        if x is not None and y is not None:
            try:
                safe_x, safe_y = int(x), int(y)
            except (TypeError, ValueError):
                return f"无效的鼠标坐标: ({x}, {y})"
            success, error = _run_checked(
                ["xdotool", "mousemove", str(safe_x), str(safe_y), "click", "3"]
            )
            return (
                f"已在坐标 ({safe_x}, {safe_y}) 点击鼠标右键"
                if success else f"鼠标右键点击失败: {error}"
            )
        success, error = _run_checked(["xdotool", "click", "3"])
        return "已点击鼠标右键" if success else f"鼠标右键点击失败: {error}"

    return f"未知的 GUI 动作: {action}"


# ==========================================================
# 5. 系统状态与硬件感知 (System Status & Metrics)
# ==========================================================

def get_system_status(query_type: str = "all") -> str:
    q = query_type.strip().lower()
    if q not in {"all", "cpu", "load", "memory", "mem", "ram", "disk", "storage", "window", "active_window", "time"}:
        return f"不支持的系统状态查询类型: {query_type}"
    info = []

    info.append(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S (%A)')}")
    info.append(f"操作系统: {platform.system()} {platform.release()} ({platform.machine()})")

    if q in ["all", "memory", "mem", "ram"]:
        try:
            with open("/proc/meminfo", "r") as f:
                lines = f.readlines()
            mem = {}
            for line in lines:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_mb = mem.get("MemTotal", 0) // 1024
            avail_mb = mem.get("MemAvailable", 0) // 1024
            used_mb = total_mb - avail_mb
            info.append(f"内存占用: {used_mb}MB / {total_mb}MB ({used_mb * 100 // max(1, total_mb)}%)")
        except Exception:
            pass

    if q in ["all", "cpu", "load"]:
        try:
            load1, load5, load15 = os.getloadavg()
            info.append(f"CPU 平均负载: 1分钟={load1:.2f}, 5分钟={load5:.2f}, 15分钟={load15:.2f}")
        except Exception:
            pass

    if q in ["all", "disk", "storage"]:
        try:
            du = shutil.disk_usage(os.path.expanduser("~"))
            total_gb = du.total // (1024**3)
            used_gb = du.used // (1024**3)
            free_gb = du.free // (1024**3)
            info.append(f"主磁盘占用: 已用 {used_gb}GB / 总共 {total_gb}GB (剩余 {free_gb}GB, {used_gb * 100 // max(1, total_gb)}%)")
        except Exception:
            pass

    if q in ["all", "window", "active_window"]:
        try:
            res = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if res.stdout.strip():
                info.append(f"当前活动窗口: {res.stdout.strip()}")
        except Exception:
            pass

    return "\n".join(info)


# ==========================================================
# 6. 文件全生命周期管理 (File System Management)
# ==========================================================

def manage_files(action: str, path: str, content: str = "", destination: str = "") -> str:
    act = action.strip().lower()
    target_path = os.path.abspath(os.path.expanduser(path))

    if act in ["read", "view", "cat"]:
        if not os.path.exists(target_path):
            return f"文件不存在: {path}"
        if os.path.isdir(target_path):
            return f"目标是目录而非文件: {path}"
        try:
            with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(3000)
            if len(data) >= 3000:
                data += "\n... (文件过长已截断)"
            return data
        except Exception as e:
            return f"读取文件失败: {e}"

    elif act in ["write", "create", "save"]:
        if os.path.exists(target_path) and not ALLOW_DESTRUCTIVE_ACTIONS:
            return (
                "为防止覆盖现有文件，写入操作已阻止。若确实需要覆盖，请在 .env 中设置 "
                "RAIN_ALLOW_DESTRUCTIVE_ACTIONS=1 后重启 Jarvis。"
            )
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"文件已成功保存: {target_path} (大小: {len(content)} 字符)"
        except Exception as e:
            return f"写入文件失败: {e}"

    elif act in ["append"]:
        if os.path.exists(target_path) and not ALLOW_DESTRUCTIVE_ACTIONS:
            return (
                "为防止修改现有文件，追加操作已阻止。若确实需要修改，请在 .env 中设置 "
                "RAIN_ALLOW_DESTRUCTIVE_ACTIONS=1 后重启 Jarvis。"
            )
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "a", encoding="utf-8") as f:
                f.write(content)
            return f"内容已成功追加到文件: {target_path}"
        except Exception as e:
            return f"追加失败: {e}"

    elif act in ["list_dir", "ls", "dir"]:
        if not os.path.exists(target_path):
            return f"目录不存在: {path}"
        try:
            items = os.listdir(target_path)
            res = []
            for item in sorted(items)[:50]:
                full = os.path.join(target_path, item)
                suffix = "/" if os.path.isdir(full) else ""
                res.append(f"{item}{suffix}")
            return f"目录内容 ({target_path}):\n" + "\n".join(res)
        except Exception as e:
            return f"列出目录失败: {e}"
            
    elif act in ["delete", "remove", "rm"]:
        if not ALLOW_DESTRUCTIVE_ACTIONS:
            return (
                "文件删除默认已禁用。若确实需要此能力，请在 .env 中设置 "
                "RAIN_ALLOW_DESTRUCTIVE_ACTIONS=1 后重启 Jarvis。"
            )
        if not os.path.exists(target_path):
            return f"目标不存在: {path}"
        try:
            if os.path.isdir(target_path):
                shutil.rmtree(target_path)
                return f"已删除目录: {target_path}"
            else:
                os.remove(target_path)
                return f"已删除文件: {target_path}"
        except Exception as e:
            return f"删除失败: {e}"
            
    return f"未识别的文件操作: {action}"


# ==========================================================
# 7. 联网搜索 (Web Search Engine)
# ==========================================================

def _strip_html(text: str) -> str:
    text = re.sub(r'<script.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z0-9#]+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def web_search(query: str, engine: str = "all", num_results: int = 5) -> str:
    """在互联网上实时搜索关键词或问题，返回清洗后的核心摘要与链接。"""
    query = query.strip()
    if not query:
        return "搜索关键词不能为空"
    engine = str(engine or "all").strip().lower()
    if engine not in {"all", "baidu", "bing"}:
        return f"不支持的搜索引擎: {engine}"

    results = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 1. 尝试百度搜索
    if engine in {"all", "baidu"}:
        try:
            baidu_url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}&rn={num_results}"
            req = urllib.request.Request(baidu_url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            snippets = re.findall(r'<div class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            if not snippets:
                snippets = re.findall(r'<div class="[^"]*content-right_8Zs40[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            for s in snippets[:num_results]:
                clean = _strip_html(s)
                if len(clean) > 20:
                    results.append(clean)
        except Exception:
            pass

    # 2. 若百度无结果，尝试维基百科中文 API
    if engine == "all" and len(results) < 2:
        try:
            wiki_url = f"https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json&utf8=1"
            req = urllib.request.Request(wiki_url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                search_items = data.get("query", {}).get("search", [])
                for item in search_items[:num_results]:
                    title = item.get("title", "")
                    snippet = _strip_html(item.get("snippet", ""))
                    if snippet:
                        results.append(f"【{title}】: {snippet}")
        except Exception:
            pass

    # 3. 若仍无结果，尝试 Bing 搜索
    if engine in {"all", "bing"} and (engine == "bing" or len(results) < 2):
        try:
            bing_url = f"https://cn.bing.com/search?q={urllib.parse.quote(query)}"
            req = urllib.request.Request(bing_url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            snippets = re.findall(r'<p class="b_algoSlug"[^>]*>(.*?)</p>', html, re.DOTALL)
            for s in snippets[:num_results]:
                clean = _strip_html(s)
                if len(clean) > 20:
                    results.append(clean)
        except Exception:
            pass

    if not results:
        return f"搜索失败：未能从 {engine} 获取 '{query}' 的结果，请检查网络后重试。"

    formatted = [f"🔍 联网搜索 '{query}' 找到以下信息:"]
    for idx, item in enumerate(results[:num_results], 1):
        formatted.append(f"{idx}. {item}")

    return "\n".join(formatted)


# ==========================================================
# 8. 用户常驻城市配置与位置持久化 (User Location Profile)
# ==========================================================

USER_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_config.json")


def get_user_config() -> Dict[str, Any]:
    """读取用户常驻配置。"""
    config = {
        "default_city": os.getenv("DEFAULT_CITY", "").strip(),
        "default_province": os.getenv("DEFAULT_PROVINCE", "").strip(),
    }
    if os.path.exists(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                config.update(saved)
        except Exception:
            pass
    return config


def set_home_location(city: str, province: str = "") -> str:
    """设置或修改用户的真实常驻城市（解决开启 VPN 代理导致 IP 漂移到香港/海外的问题）。"""
    clean_city = city.strip().replace("市", "")
    clean_province = province.strip().replace("省", "")

    if not clean_city:
        return "城市名称不能为空，例如：'深圳'、'北京'、'上海'、'广州'、'杭州'。"

    cfg = get_user_config()
    cfg["default_city"] = clean_city
    if clean_province:
        cfg["default_province"] = clean_province

    try:
        with open(USER_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.chmod(USER_CONFIG_PATH, 0o600)
        loc_str = f"{clean_province}省{clean_city}市" if clean_province else f"{clean_city}市"
        return f"📍 已成功将您的常驻城市设为【{loc_str}】！今后无论是开启 VPN 还是代理，查询天气和本地服务都将始终以【{clean_city}】为准。"
    except Exception as e:
        return f"保存常驻城市失败: {e}"


# ==========================================================
# 9. 实时天气查询 (Weather Service)
# ==========================================================

def get_weather(city: str = "") -> str:
    """查询指定城市或当前所在城市的实时天气、气温、湿度、风向与天气预报。"""
    target_city = city.strip()
    if not target_city:
        # 若未指定城市，优先使用用户配置的真实常驻城市（防止 VPN 代理导致天气城市错误）
        cfg = get_user_config()
        target_city = cfg.get("default_city", "").strip()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    # 1. 优先尝试 wttr.in 实时气象数据
    try:
        url_city = urllib.parse.quote(target_city) if target_city else ""
        req = urllib.request.Request(f"https://wttr.in/{url_city}?format=j1&lang=zh", headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        current = data.get("current_condition", [{}])[0]
        temp_c = current.get("temp_C", "")
        feels_like = current.get("FeelsLikeC", "")
        humidity = current.get("humidity", "")
        wind_km = current.get("windspeedKmph", "")
        desc_list = current.get("lang_zh", [{}])
        weather_desc = desc_list[0].get("value", "") if desc_list else current.get("weatherDesc", [{}])[0].get("value", "")

        area_obj = data.get("nearest_area", [{}])[0]
        detected_city = area_obj.get("areaName", [{}])[0].get("value", target_city or "本地")

        today_forecast = data.get("weather", [{}])[0]
        max_t = today_forecast.get("maxtempC", "")
        min_t = today_forecast.get("mintempC", "")

        return f"🌤️ 【{target_city or detected_city}】天气报告：当前【{weather_desc}】，实时气温 {temp_c}°C（体感温度 {feels_like}°C），今日最高气温 {max_t}°C，最低气温 {min_t}°C，空气湿度 {humidity}%，风速 {wind_km}km/h。"
    except Exception:
        pass

    # 2. 备选尝试百度搜索实时天气卡片
    try:
        kw = f"{target_city}天气" if target_city else "今日天气预报"
        baidu_url = f"https://www.baidu.com/s?wd={urllib.parse.quote(kw)}"
        req = urllib.request.Request(baidu_url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        clean = _strip_html(html)
        match = re.search(r'([\u4e00-\u9fa5]{2,6}天气[^\n。]*?\d{1,2}℃[^\n。]*?\d{1,2}℃)', clean)
        if match:
            return f"🌤️ 天气查询结果：{match.group(1)}"
    except Exception:
        pass

    return f"已尝试查询【{target_city or '当前位置'}】的天气。网络如受限，建议在浏览器中搜索查看实时天气。"


# ==========================================================
# 10. 地理位置与 IP 定位服务 (Geolocation Service)
# ==========================================================

def get_location(query_type: str = "all") -> str:
    """获取当前电脑所在的地理位置信息（支持区分用户真实常驻城市与 VPN 代理节点位置）。"""
    query_type = str(query_type or "all").strip().lower()
    if query_type not in {"all", "city", "coordinates", "ip"}:
        return f"不支持的位置查询类型: {query_type}"
    cfg = get_user_config()
    home_city = cfg.get("default_city", "").strip()
    home_province = cfg.get("default_province", "").strip()

    if query_type == "city" and home_city:
        home_full = f"{home_province}省{home_city}市" if home_province else f"{home_city}市"
        return f"📍 您设置的真实常驻城市为：【{home_full}】"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    vpn_info = {}

    # 尝试检测当前网络出口 IP 及位置
    try:
        req = urllib.request.Request("http://ip-api.com/json/?lang=zh-CN", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "success":
            country = data.get("country", "中国")
            region = data.get("regionName", "")
            city = data.get("city", "")
            lat = data.get("lat", "")
            lon = data.get("lon", "")
            isp = data.get("isp", "")
            ip = data.get("query", "")
            tz = data.get("timezone", "Asia/Shanghai")
            vpn_info = {
                "loc": f"{country} {region} {city}".strip(),
                "city": city,
                "lat": lat,
                "lon": lon,
                "isp": isp,
                "ip": ip,
                "tz": tz,
            }
    except Exception:
        pass

    if query_type == "ip":
        if not vpn_info:
            return "公网 IP 与网络运营商查询失败，请检查网络后重试。"
        return f"🌐 当前公网 IP: {vpn_info.get('ip')}（运营商: {vpn_info.get('isp')}）"

    if query_type == "coordinates":
        if not vpn_info:
            return "经纬度查询失败，请检查网络后重试。"
        return f"📍 当前网络定位坐标: {vpn_info.get('lat')}°N, {vpn_info.get('lon')}°E"

    if query_type == "city":
        if not vpn_info:
            return "当前城市查询失败；也可以先设置真实常驻城市。"
        return f"📍 当前网络识别城市为：【{vpn_info.get('loc')}】"

    if home_city:
        home_full = f"{home_province}省{home_city}市" if home_province else f"{home_city}市"
        if vpn_info:
            return (
                f"📍 您的真实常驻城市为：【{home_full}】（已绑定常驻配置，不受 VPN 影响）\n"
                f"🌐 当前网络连接出口：【{vpn_info.get('loc')}】 (IP: {vpn_info.get('ip')}, 运营商: {vpn_info.get('isp')})\n"
                f"💡 若您已搬迁或更换常驻城市，只需对我说“记住我住在某某城市”即可随时更新！"
            )
        else:
            return f"📍 您当前设置的真实常驻城市为：【{home_full}】。天气和本地生活查询将始终以【{home_city}】为准。"

    # 若未设置常驻城市，汇报当前网络定位并温馨提示
    if vpn_info:
        return (
            f"📍 当前网络识别位置为：【{vpn_info.get('loc')}】\n"
            f"- 经纬度坐标: {vpn_info.get('lat')}°N, {vpn_info.get('lon')}°E\n"
            f"- 当前网络 IP: {vpn_info.get('ip')} ({vpn_info.get('isp')})\n"
            f"⚠️ 检测到您可能开启了 VPN / 代理导致定位显示为代理节点。您可以直接对我说：“记住我在深圳”（或您所在的真实城市），我就会为您永久锁定真实位置！"
        )

    return "已尝试获取地理位置。若网络受限或开启了全局代理，您可以直接对我说“记住我在某某城市”来设置您的真实常驻位置。"


# ==========================================================
# 11. 长期个性化记忆管理 (Long-Term Memory Service)
# ==========================================================

from memory import memory_manager


def manage_memory(action: str, content: str = "", category: str = "general") -> str:
    """长期个性化记忆管理：支持添加记忆 (remember)、查询回忆 (recall)、遗忘删除 (forget)。"""
    act = action.strip().lower()
    if act in ["remember", "add", "save", "record"]:
        return memory_manager.add_memory(content, category=category)
    elif act in ["recall", "search", "get", "query", "view"]:
        return memory_manager.search_memory(content)
    elif act in ["forget", "delete", "remove", "clear"]:
        return memory_manager.delete_memory(content)
    return f"未知的记忆操作: {action}"


# ==========================================================
# 12. 浏览器 Agent 与网页内容提取 (Browser Agent)
# ==========================================================

PLATFORM_SEARCH_URLS = {
    "bilibili": "https://search.bilibili.com/all?keyword={}",
    "b站": "https://search.bilibili.com/all?keyword={}",
    "youtube": "https://www.youtube.com/results?search_query={}",
    "油管": "https://www.youtube.com/results?search_query={}",
    "github": "https://github.com/search?q={}",
    "zhihu": "https://www.zhihu.com/search?type=content&q={}",
    "知乎": "https://www.zhihu.com/search?type=content&q={}",
    "baidu": "https://www.baidu.com/s?wd={}",
    "百度": "https://www.baidu.com/s?wd={}",
    "google": "https://www.google.com/search?q={}",
    "谷歌": "https://www.google.com/search?q={}",
}


def browser_agent(action: str, target: str = "", platform: str = "") -> str:
    """浏览器智能 Agent：支持网页正文抓取提取、各主流平台直达搜索与网页导航。"""
    act = action.strip().lower()
    
    # 平台直达搜索 (如 B站, YouTube, GitHub, 知乎)
    if act in ["search_platform", "search", "platform"]:
        plat = platform.strip().lower() if platform else "bilibili"
        search_kw = target.strip()
        if not search_kw:
            return "搜索关键词不能为空"
        url_template = PLATFORM_SEARCH_URLS.get(plat, "https://www.baidu.com/s?wd={}")
        final_url = url_template.format(urllib.parse.quote(search_kw))
        success, error = _open_desktop_target(final_url)
        return (
            f"已在浏览器中打开 {plat} 搜索: '{search_kw}' (网址: {final_url})"
            if success else f"打开平台搜索失败: {error}"
        )
        
    # 提取网页正文内容 (Extract Webpage)
    elif act in ["extract_content", "read_page", "read", "extract"]:
        url = normalize_web_url(target)
        if not url:
            return f"无效或不支持的网页地址: {target or '（空）'}"
            
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                
            title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
            title = _strip_html(title_match.group(1)) if title_match else "网页"
            
            clean_body = _strip_html(html)
            if len(clean_body) > 2000:
                clean_body = clean_body[:2000] + "\n... (正文过长已截取前2000字符)"

            return f"📄 网页标题: 【{title}】\n\n正文内容摘要:\n{clean_body}"
        except Exception as e:
            return f"抓取网页正文失败: {str(e)}"
            
    # 直接在浏览器中打开网址
    elif act in ["open_url", "navigate", "open"]:
        url = normalize_web_url(target)
        if not url:
            return f"无效或不支持的网页地址: {target or '（空）'}"
        success, error = _open_desktop_target(url)
        return f"已在浏览器中打开: {url}" if success else f"打开网页失败: {error}"
        
    return f"未知的浏览器 Agent 操作: {action}"


# ==========================================================
# 9. Function Calling 工具注册与调度
# ==========================================================

_FLAT_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "execute_shell_command",
        "description": "在用户的 Linux 电脑上执行 Shell/Bash 终端命令。此高风险能力默认关闭，只有用户显式配置 RAIN_ALLOW_SHELL_COMMANDS=1 后才可用。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的完整 Bash 命令字符串，如 'ps aux | grep chrome', 'mkdir my_project', 'uptime' 等",
                },
                "working_dir": {
                    "type": "string",
                    "description": "可选。执行命令的工作目录路径（默认为用户主目录 ~）",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": "可选。命令超时秒数，默认 20 秒",
                }
            },
            "required": ["command"]
        }
    },
    {
        "type": "function",
        "name": "open_application",
        "description": "启动电脑上的应用程序、打开指定网页或打开文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用程序名称或别名（如 'chrome', 'vscode', 'terminal', 'files', 'calculator', 'settings' 或具体命令名）",
                },
                "path_or_url": {
                    "type": "string",
                    "description": "可选。要打开的具体网页 URL (如 'https://bilibili.com') 或文件路径",
                }
            },
            "required": []
        }
    },
    {
        "type": "function",
        "name": "web_search",
        "description": "在互联网上实时搜索信息（新闻、技术知识、即时问答等），返回搜索结果摘要。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                },
                "engine": {
                    "type": "string",
                    "enum": ["all", "baidu", "bing"],
                    "description": "可选。搜索引擎偏好，默认 all",
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "manage_memory",
        "description": "管理和使用关于用户的长期个性化记忆（记住用户的喜好、生日、习惯、事实、规则，或回忆检索已记住的信息，或遗忘指定信息）。当用户说'帮我记住...'、'你还记得我喜欢什么吗'、'忘掉关于...'时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["remember", "recall", "forget"],
                    "description": "记忆操作类型：remember(记住新事实/偏好), recall(回忆/查询记忆), forget(遗忘/删除记忆)",
                },
                "content": {
                    "type": "string",
                    "description": "要记住的具体内容（如'用户喜欢喝少糖美式咖啡'），或回忆/遗忘的目标关键词",
                },
                "category": {
                    "type": "string",
                    "enum": ["preference", "profile", "fact", "reminder", "rule"],
                    "description": "可选。记忆类别：preference(用户偏好), profile(个人信息), fact(事实知识), reminder(提醒备忘), rule(行为规则)",
                }
            },
            "required": ["action", "content"]
        }
    },
    {
        "type": "function",
        "name": "set_home_location",
        "description": "设置或修改用户的真实常驻城市（解决开启 VPN/代理导致 IP 定位漂移到香港或海外的问题）。当用户说'我在深圳'、'记住我的城市是北京'、'设置默认城市为杭州'时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "真实的常驻城市名称（如 '深圳', '北京', '上海', '广州', '杭州', '成都', '武汉' 等）",
                },
                "province": {
                    "type": "string",
                    "description": "可选。省份名称（如 '广东', '浙江', '四川' 等）",
                }
            },
            "required": ["city"]
        }
    },
    {
        "type": "function",
        "name": "get_location",
        "description": "获取当前电脑/用户所在的地理位置信息（国家、省份、城市、区县、经纬度坐标、时区、公网IP与网络运营商）。当用户询问我在哪、我的位置、当前在哪个城市等问题时调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["all", "city", "coordinates", "ip"],
                    "description": "查询类型：all(全部信息), city(仅城市省份), coordinates(经纬度), ip(公网IP与网络信息)",
                }
            },
            "required": []
        }
    },
    {
        "type": "function",
        "name": "get_weather",
        "description": "查询指定城市或当前所在城市的实时天气状况、气温、体感温度、湿度、风力与当天天气预报。当用户询问天气、气温、下雨情况时必须调用此工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名称（如 '北京', '上海', '深圳', '广州', '杭州', '成都', '武汉' 等；若用户未指明城市，留空即可自动定位当前城市）",
                }
            },
            "required": []
        }
    },
    {
        "type": "function",
        "name": "browser_agent",
        "description": "浏览器 Agent 自动化工具：支持网页正文抓取提取阅读、在各大主流平台（B站、YouTube、GitHub、知乎、百度等）精准检索视频/内容、或导航打开指定网页。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["extract_content", "search_platform", "open_url"],
                    "description": "操作类型：extract_content(抓取网页正文), search_platform(在指定平台搜索), open_url(在浏览器打开网址)",
                },
                "target": {
                    "type": "string",
                    "description": "目标网址 URL 或搜索关键词",
                },
                "platform": {
                    "type": "string",
                    "enum": ["bilibili", "youtube", "github", "zhihu", "baidu", "google"],
                    "description": "当 action 为 search_platform 时的目标平台名称",
                }
            },
            "required": ["action", "target"]
        }
    },
    {
        "type": "function",
        "name": "control_system",
        "description": "控制系统硬件与设置。音量、亮度、媒体和锁屏可直接使用；睡眠、关机和重启默认关闭，需用户显式启用。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "volume_up", "volume_down", "volume_set", "mute", "unmute",
                        "brightness_up", "brightness_down", "brightness_set",
                        "media_play_pause", "media_next", "media_prev",
                        "lock_screen", "sleep", "reboot", "shutdown"
                    ],
                    "description": "具体的控制动作",
                },
                "value": {
                    "type": "string",
                    "description": "动作参数值（如音量百分比 '50', 步长 '10%' 等，若无则留空）",
                }
            },
            "required": ["action"]
        }
    },
    {
        "type": "function",
        "name": "gui_control",
        "description": "模拟键盘快捷键（如 Ctrl+C, Ctrl+V, Super 等）、打字输入或鼠标点击。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["hotkey", "type", "click", "right_click"],
                    "description": "操作类型：hotkey(发送快捷键), type(输入文本), click(鼠标点击)",
                },
                "key": {
                    "type": "string",
                    "description": "快捷键组合，如 'ctrl+c', 'ctrl+v', 'alt+tab', 'super', 'enter', 'ctrl+alt+t'",
                },
                "text": {
                    "type": "string",
                    "description": "要输入的准确正文。不要把语音转写自动产生的开头句号、命令词“输入”或额外解释放进正文",
                },
                "x": {
                    "type": "integer",
                    "description": "鼠标点击的 X 坐标（可选）",
                },
                "y": {
                    "type": "integer",
                    "description": "鼠标点击的 Y 坐标（可选）",
                }
            },
            "required": ["action"]
        }
    },
    {
        "type": "function",
        "name": "get_system_status",
        "description": "获取电脑当前运行状态（CPU、内存、磁盘利用率、当前活动窗口、系统时间等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["all", "cpu", "memory", "disk", "active_window", "time"],
                    "description": "查询的状态类型",
                }
            },
            "required": []
        }
    },
    {
        "type": "function",
        "name": "manage_files",
        "description": "管理电脑文件与文件夹。读取、列目录和创建新文件可直接使用；覆盖、追加现有文件与删除操作默认关闭，需用户显式启用。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["read", "write", "append", "list_dir", "delete"],
                    "description": "文件操作类型",
                },
                "path": {
                    "type": "string",
                    "description": "文件或目录路径（支持 ~ 相对路径或绝对路径）",
                },
                "content": {
                    "type": "string",
                    "description": "写入或追加的内容字符串（仅 write / append 操作需要）",
                }
            },
            "required": ["action", "path"]
        }
    }
]

# Qwen Omni-Realtime / OpenAI 兼容协议要求函数信息位于 `function` 内。
# 保留上面的扁平源定义便于维护，在发送给模型前统一转换，避免模型看不到工具。
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": definition["name"],
            "description": definition["description"],
            "parameters": definition["parameters"],
        },
    }
    for definition in _FLAT_TOOL_DEFINITIONS
]


def dispatch_tool_call(name: str, arguments: Union[str, Dict[str, Any]]) -> str:
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return f"工具参数解析失败: {exc.msg}"
    else:
        args = arguments or {}

    if not isinstance(args, dict):
        return "工具参数必须是 JSON 对象"

    required_arguments = {
        "execute_shell_command": ["command"],
        "web_search": ["query"],
        "manage_memory": ["action", "content"],
        "set_home_location": ["city"],
        "browser_agent": ["action", "target"],
        "control_system": ["action"],
        "gui_control": ["action"],
        "manage_files": ["action", "path"],
    }
    missing = [
        key for key in required_arguments.get(name, [])
        if args.get(key) is None or args.get(key) == ""
    ]
    if missing:
        return f"工具参数缺失: {', '.join(missing)}"

    print(f"\n⚙️ 正在执行系统控制工具: {name}({args})")

    try:
        if name == "execute_shell_command":
            return execute_shell_command(
                command=args.get("command", ""),
                timeout=args.get("timeout", 20),
                working_dir=args.get("working_dir"),
            )
        elif name == "open_application":
            return open_application(
                app_name=args.get("app_name", ""),
                path_or_url=args.get("path_or_url", ""),
            )
        elif name == "web_search":
            return web_search(
                query=args.get("query", ""),
                engine=args.get("engine", "all"),
            )
        elif name == "manage_memory":
            return manage_memory(
                action=args.get("action", "remember"),
                content=args.get("content", ""),
                category=args.get("category", "general"),
            )
        elif name == "set_home_location":
            return set_home_location(
                city=args.get("city", ""),
                province=args.get("province", ""),
            )
        elif name == "get_location":
            return get_location(
                query_type=args.get("query_type", "all"),
            )
        elif name == "get_weather":
            return get_weather(
                city=args.get("city", ""),
            )
        elif name == "browser_agent":
            return browser_agent(
                action=args.get("action", ""),
                target=args.get("target", ""),
                platform=args.get("platform", ""),
            )
        elif name == "control_system":
            return control_system(
                action=args.get("action", ""),
                value=str(args.get("value", "")),
            )
        elif name == "gui_control":
            return gui_control(
                action=args.get("action", ""),
                key=args.get("key", ""),
                text=args.get("text", ""),
                x=args.get("x"),
                y=args.get("y"),
            )
        elif name == "get_system_status":
            return get_system_status(
                query_type=args.get("query_type", "all"),
            )
        elif name == "manage_files":
            return manage_files(
                action=args.get("action", ""),
                path=args.get("path", ""),
                content=args.get("content", ""),
            )
        else:
            return f"未知的工具名称: {name}"
    except Exception as e:
        return f"工具执行异常: {str(e)}"
