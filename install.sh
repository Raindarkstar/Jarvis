#!/usr/bin/env bash
# Jarvis 一键安装脚本：安装系统依赖、创建虚拟环境、安装项目并注册桌面入口。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${JARVIS_VENV_DIR:-${SCRIPT_DIR}/.venv}"

if [[ "${EUID}" -eq 0 ]]; then
    SUDO=()
elif command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
else
    echo "错误：安装系统依赖需要 root 权限或 sudo。" >&2
    exit 1
fi

if [[ ! -r /etc/os-release ]]; then
    echo "错误：无法识别 Linux 发行版（缺少 /etc/os-release）。" >&2
    exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release

install_system_dependencies() {
    case "${ID:-}" in
        ubuntu|debian|linuxmint|pop)
            "${SUDO[@]}" apt-get update
            "${SUDO[@]}" apt-get install -y \
                gcc libasound2-dev portaudio19-dev pkg-config python3 \
                python3-dev python3-gi python3-venv gir1.2-gtk-3.0 \
                gir1.2-webkit2-4.1 xdg-utils xdotool brightnessctl playerctl \
                pipewire wireplumber libspa-0.2-modules
            ;;
        fedora|rhel|centos)
            "${SUDO[@]}" dnf install -y \
                gcc alsa-lib-devel portaudio-devel pkgconf-pkg-config python3 \
                python3-devel python3-gobject gtk3 webkit2gtk4.1 xdg-utils \
                xdotool brightnessctl playerctl pipewire wireplumber
            ;;
        arch|manjaro)
            "${SUDO[@]}" pacman -Sy --needed --noconfirm \
                base-devel alsa-lib portaudio pkgconf python python-pip \
                python-gobject gtk3 webkit2gtk-4.1 xdg-utils xdotool brightnessctl \
                playerctl pipewire wireplumber
            ;;
        *)
            echo "错误：暂不支持发行版 '${ID:-unknown}'。请手动安装 GTK3、WebKitGTK 4.1、PortAudio、Python 3.12+ 和 gcc。" >&2
            exit 1
            ;;
    esac
}

echo "==> 安装系统依赖（${PRETTY_NAME:-${ID:-Linux}}）"
install_system_dependencies

PYTHON3=""
for candidate in python3.12 python3; do
    if command -v "${candidate}" >/dev/null 2>&1 \
        && "${candidate}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        PYTHON3="$(command -v "${candidate}")"
        break
    fi
done
if [[ -z "${PYTHON3}" ]]; then
    echo "错误：需要 Python 3.12+；请先安装 python3.12，再重新运行此脚本。" >&2
    exit 1
fi

echo "==> 创建虚拟环境：${VENV_DIR}"
"${PYTHON3}" -m venv --system-site-packages "${VENV_DIR}"

PYTHON="${VENV_DIR}/bin/python"
echo "==> 安装 Jarvis Python 依赖"
"${PYTHON}" -m pip install --upgrade pip
"${PYTHON}" -m pip install --editable "${SCRIPT_DIR}"

if command -v gcc >/dev/null 2>&1; then
    echo "==> 编译可选的软件 AEC 引擎"
    gcc -shared -fPIC -O3 -o "${SCRIPT_DIR}/libaec.so" "${SCRIPT_DIR}/aec_engine.c" -lm
fi

if [[ -f "${SCRIPT_DIR}/.env.example" && ! -e "${SCRIPT_DIR}/.env" ]]; then
    echo "==> 创建配置文件：${SCRIPT_DIR}/.env"
    install -m 600 "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
fi

APP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/icons/hicolor/128x128/apps"
mkdir -p "${APP_DIR}" "${ICON_DIR}"
install -m 644 "${SCRIPT_DIR}/assets/jarvis-logo.png" "${ICON_DIR}/jarvis.png"
sed \
    -e "s|^Exec=.*|Exec=${SCRIPT_DIR}/jarvis-client.sh|" \
    -e "s|^Icon=.*|Icon=${ICON_DIR}/jarvis.png|" \
    "${SCRIPT_DIR}/jarvis.desktop" > "${APP_DIR}/jarvis.desktop"

echo
echo "安装完成。"
echo "  桌面客户端：${VENV_DIR}/bin/jarvis desktop"
echo "  后台语音服务：${VENV_DIR}/bin/jarvis voice"
echo "  环境诊断：${VENV_DIR}/bin/jarvis doctor"
echo "首次使用前请编辑 ${SCRIPT_DIR}/.env，填写 DASHSCOPE_API_KEY。"
