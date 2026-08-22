# Jarvis

Jarvis 是一款面向 Linux 桌面的中文 AI 助手，支持语音对话、文字聊天、长期记忆、网页访问和受控的系统操作。

## 功能

- Qwen Omni 实时语音对话与文字聊天
- 唤醒词、回声消除和语音状态提示
- 共享的长期记忆与会话历史
- 浏览器、天气、位置、文件和系统控制工具
- 原生 GTK/WebKit 桌面窗口，支持最小化、最大化与全屏
- 高风险 Shell、覆盖和删除能力默认关闭

## 环境要求

- Linux 与 Python 3.12+
- GTK 3、WebKitGTK 4.1、PortAudio
- 可用的 DashScope API Key

Ubuntu/Debian 可先安装系统依赖：

```bash
sudo apt install python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 portaudio19-dev
```

## 安装

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中填入 `DASHSCOPE_API_KEY`，然后启动桌面客户端：

```bash
python client_app.py
```

语音主程序可通过以下命令启动：

```bash
python rain_ai.py
```

## 测试

```bash
python -m unittest discover -s tests -q
```

## 隐私与安全

`.env`、个人配置、长期记忆、聊天数据库和录音文件均被排除在版本控制之外。Shell 命令及破坏性文件操作需要用户主动启用。

## License

GPL-3.0，详见 [COPYING](COPYING)。
