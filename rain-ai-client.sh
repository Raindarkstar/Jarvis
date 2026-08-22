#!/usr/bin/env bash
# 兼容旧快捷方式：启动 Jarvis 后台语音服务。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/.venv/bin/python"
elif [[ -x "${SCRIPT_DIR}/bin/python" ]]; then
    PYTHON="${SCRIPT_DIR}/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "未找到 Python 3，请先运行 ./install.sh。" >&2
    exit 1
fi

exec "${PYTHON}" -m jarvis_cli voice "$@"
