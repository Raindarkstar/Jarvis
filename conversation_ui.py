#!/usr/bin/env python3
"""
conversation_ui.py - 极简高颜值流式对话客户端
基于 GTK3 + WebKit2 打造，具备极简现代排版（无传统气泡边框、无头像、右上角胶囊、Markdown 排版与流式打印输出）。
"""

import os
import sys
import json
import threading

DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if DIST_PACKAGES not in sys.path:
    sys.path.append(DIST_PACKAGES)

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gtk, Gdk, WebKit2, GLib


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis</title>
<style>
  :root {
    --bg-color: rgba(255, 255, 255, 0.94);
    --text-main: #1d1d1f;
    --text-secondary: #6e6e73;
    --user-bg: #f2f2f7;
    --user-text: #1d1d1f;
    --border-color: rgba(0, 0, 0, 0.08);
    --code-bg: #f6f8fa;
    --tool-bg: rgba(0, 113, 227, 0.08);
    --tool-text: #0071e3;
    --table-border: #e5e5ea;
  }

  @media (prefers-color-scheme: dark) {
    :root {
      --bg-color: rgba(28, 28, 30, 0.92);
      --text-main: #f5f5f7;
      --text-secondary: #98989d;
      --user-bg: rgba(255, 255, 255, 0.12);
      --user-text: #ffffff;
      --border-color: rgba(255, 255, 255, 0.12);
      --code-bg: rgba(255, 255, 255, 0.08);
      --tool-bg: rgba(41, 151, 255, 0.15);
      --tool-text: #2997ff;
      --table-border: rgba(255, 255, 255, 0.15);
    }
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    -webkit-user-select: text;
    user-select: text;
  }

  html, body {
    width: 100%;
    height: 100%;
    background: transparent;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Segoe UI", Roboto, sans-serif;
    color: var(--text-main);
    overflow: hidden;
  }

  #app {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    background: var(--bg-color);
    backdrop-filter: blur(40px) saturate(190%);
    -webkit-backdrop-filter: blur(40px) saturate(190%);
    border-radius: 24px;
    border: 1px solid var(--border-color);
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.22), 0 4px 16px rgba(0, 0, 0, 0.08);
    overflow: hidden;
  }

  /* 顶部状态栏与关闭按钮 */
  #header {
    height: 48px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 20px;
    border-bottom: 1px solid var(--border-color);
    -webkit-app-region: drag;
  }

  #header .title {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.5px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  #header .title .indicator {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #34c759;
    box-shadow: 0 0 8px rgba(52, 199, 89, 0.6);
  }

  #header .close-btn {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: rgba(120, 120, 128, 0.16);
    border: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    color: var(--text-secondary);
    transition: all 0.2s ease;
  }

  #header .close-btn:hover {
    background: rgba(120, 120, 128, 0.3);
    color: var(--text-main);
  }

  /* 消息流动区 */
  #chat-stream {
    flex: 1;
    overflow-y: auto;
    padding: 24px 28px;
    display: flex;
    flex-direction: column;
    gap: 28px;
    scroll-behavior: smooth;
  }

  #chat-stream::-webkit-scrollbar {
    width: 6px;
  }

  #chat-stream::-webkit-scrollbar-track {
    background: transparent;
  }

  #chat-stream::-webkit-scrollbar-thumb {
    background: rgba(120, 120, 128, 0.25);
    border-radius: 3px;
  }

  /* 对话轮次容器 */
  .turn-container {
    display: flex;
    flex-direction: column;
    gap: 16px;
    width: 100%;
    animation: fadeIn 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  }

  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* 用户提问：右上角圆润小胶囊（Soft Pill） */
  .user-pill-wrapper {
    display: flex;
    justify-content: flex-end;
    width: 100%;
  }

  .user-pill {
    max-width: 82%;
    padding: 10px 18px;
    background: var(--user-bg);
    color: var(--user-text);
    font-size: 14.5px;
    font-weight: 500;
    line-height: 1.5;
    border-radius: 20px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
    letter-spacing: 0.2px;
    word-break: break-word;
  }

  /* 工具调用徽章 */
  .tool-badge {
    align-self: flex-start;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    background: var(--tool-bg);
    color: var(--tool-text);
    font-size: 12.5px;
    font-weight: 500;
    border-radius: 12px;
    margin-bottom: 2px;
    border: 1px solid rgba(41, 151, 255, 0.2);
  }

  .tool-badge .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* AI 回答：极简无框文档流，支持完整 Markdown */
  .ai-response {
    width: 100%;
    color: var(--text-main);
    font-size: 15px;
    line-height: 1.72;
    letter-spacing: 0.1px;
    word-break: break-word;
  }

  .ai-response p {
    margin-bottom: 12px;
  }

  .ai-response p:last-child {
    margin-bottom: 0;
  }

  .ai-response strong {
    font-weight: 600;
    color: var(--text-main);
  }

  .ai-response ul, .ai-response ol {
    margin: 8px 0 14px 22px;
  }

  .ai-response li {
    margin-bottom: 6px;
    line-height: 1.65;
  }

  .ai-response code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 13.5px;
    padding: 2px 6px;
    background: var(--code-bg);
    border-radius: 6px;
    border: 1px solid var(--border-color);
  }

  .ai-response pre {
    margin: 12px 0;
    padding: 14px 16px;
    background: var(--code-bg);
    border-radius: 12px;
    overflow-x: auto;
    border: 1px solid var(--border-color);
  }

  .ai-response pre code {
    padding: 0;
    background: transparent;
    border: none;
    font-size: 13px;
    line-height: 1.5;
  }

  .ai-response table {
    width: 100%;
    border-collapse: collapse;
    margin: 14px 0;
    font-size: 14px;
  }

  .ai-response th, .ai-response td {
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid var(--table-border);
  }

  .ai-response th {
    font-weight: 600;
    color: var(--text-secondary);
    background: rgba(120, 120, 128, 0.05);
  }

  /* 打字机流式光标 */
  .streaming-cursor {
    display: inline-block;
    width: 2px;
    height: 15px;
    background: #0071e3;
    margin-left: 2px;
    vertical-align: -2px;
    animation: blink 0.9s infinite;
  }

  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
  }

  /* 底部提示 */
  #footer-hint {
    padding: 10px 20px;
    font-size: 12px;
    color: var(--text-secondary);
    text-align: center;
    border-top: 1px solid var(--border-color);
    background: rgba(120, 120, 128, 0.03);
  }
</style>
</head>
<body>
<div id="app">
  <div id="header">
    <div class="title">
      <div class="indicator"></div>
      <span>实时语音助手</span>
    </div>
    <button class="close-btn" onclick="closeWindow()" title="收起对话">✕</button>
  </div>

  <div id="chat-stream">
    <div class="turn-container" id="welcome-turn">
      <div class="ai-response">
        <p><strong>你好！我是你的桌面级 AI 语音助手。</strong></p>
        <p>说出 <code>Hey Jarvis</code> 即可随时唤醒我，支持自然对话、联网搜索、天气与位置查询及系统控制。</p>
      </div>
    </div>
  </div>

  <div id="footer-hint">随时点击屏幕顶部超椭圆或右上角 ✕ 即可收起窗口</div>
</div>

<script>
  function closeWindow() {
    try {
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.closeWindow) {
        window.webkit.messageHandlers.closeWindow.postMessage("close");
      }
    } catch (e) {
      console.error(e);
    }
  }

  window.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      closeWindow();
    }
  });

  let currentTurnId = 0;
  let currentTurnEl = null;
  let currentAiEl = null;
  let currentAiRawText = '';
  let currentUserEl = null;
  let currentToolEl = null;

  function createNewTurn() {
    currentTurnId++;
    const container = document.getElementById('chat-stream');

    const turn = document.createElement('div');
    turn.className = 'turn-container';
    turn.id = 'turn-' + currentTurnId;

    container.appendChild(turn);
    currentTurnEl = turn;
    currentUserEl = null;
    currentAiEl = null;
    currentAiRawText = '';
    currentToolEl = null;
    scrollToBottom();
    return turn;
  }

  function updateUserTranscript(text, isFinal) {
    if (!text || text.trim() === '') return;
    if (!currentTurnEl || (currentAiEl && isFinal)) {
      createNewTurn();
    }

    if (!currentUserEl) {
      const wrapper = document.createElement('div');
      wrapper.className = 'user-pill-wrapper';
      const pill = document.createElement('div');
      pill.className = 'user-pill';
      wrapper.appendChild(pill);
      currentTurnEl.appendChild(wrapper);
      currentUserEl = pill;
    }

    currentUserEl.textContent = text;
    scrollToBottom();
  }

  function showToolBadge(toolName, query) {
    if (!currentTurnEl) {
      createNewTurn();
    }
    if (!currentToolEl) {
      const badge = document.createElement('div');
      badge.className = 'tool-badge';
      badge.innerHTML = '<div class="spinner"></div><span id="tool-text"></span>';
      currentTurnEl.appendChild(badge);
      currentToolEl = badge;
    }
    const textSpan = currentToolEl.querySelector('#tool-text');
    let label = '正在调用系统工具...';
    if (toolName === 'web_search') {
      label = '正在联网搜索: ' + (query || '');
    } else if (toolName === 'get_weather') {
      label = '正在查询实时天气...';
    } else if (toolName === 'get_location') {
      label = '正在获取当前位置...';
    } else if (toolName === 'manage_memory') {
      label = '正在同步记忆数据库...';
    }
    textSpan.textContent = label;
    scrollToBottom();
  }

  function hideToolBadge() {
    if (currentToolEl) {
      currentToolEl.style.display = 'none';
      currentToolEl = null;
    }
  }

  function appendAiDelta(delta) {
    if (!currentTurnEl) {
      createNewTurn();
    }
    hideToolBadge();

    if (!currentAiEl) {
      const aiDiv = document.createElement('div');
      aiDiv.className = 'ai-response';
      currentTurnEl.appendChild(aiDiv);
      currentAiEl = aiDiv;
      currentAiRawText = '';
    }

    currentAiRawText += delta;
    currentAiEl.innerHTML = parseSimpleMarkdown(currentAiRawText) + '<span class="streaming-cursor"></span>';
    scrollToBottom();
  }

  function finishAiTurn() {
    if (currentAiEl) {
      currentAiEl.innerHTML = parseSimpleMarkdown(currentAiRawText);
    }
    currentTurnEl = null;
    currentAiEl = null;
    currentAiRawText = '';
    currentToolEl = null;
    currentUserEl = null;
    scrollToBottom();
  }

  function scrollToBottom() {
    const container = document.getElementById('chat-stream');
    container.scrollTop = container.scrollHeight;
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function parseSimpleMarkdown(src) {
    if (!src) return '';
    let text = escapeHtml(src);

    // 行内代码 `code`
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');

    // 加粗 **bold**
    text = text.replace(/\\*\\*([^\\*]+)\\*\\*/g, '<strong>$1</strong>');

    // 简单段落与换行
    const lines = text.split('\\n');
    let inTable = false;
    let tableHtml = '';
    let html = '';

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();

      // 表格处理
      if (line.startsWith('|') && line.endsWith('|')) {
        const cells = line.split('|').map(c => c.trim()).filter((c, idx, arr) => idx > 0 && idx < arr.length - 1);
        if (cells.some(c => c.startsWith('---') || c.startsWith(':---'))) {
          // 分隔行，忽略
          continue;
        }
        if (!inTable) {
          inTable = true;
          tableHtml = '<table><thead><tr>' + cells.map(c => '<th>' + c + '</th>').join('') + '</tr></thead><tbody>';
        } else {
          tableHtml += '<tr>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>';
        }
        continue;
      } else if (inTable) {
        inTable = false;
        tableHtml += '</tbody></table>';
        html += tableHtml;
        tableHtml = '';
      }

      if (!line) {
        html += '<p></p>';
      } else if (line.startsWith('- ') || line.startsWith('* ') || line.startsWith('o ')) {
        html += '<ul><li>' + line.substring(2) + '</li></ul>';
      } else if (/^\\d+\\.\\s/.test(line)) {
        const item = line.replace(/^\\d+\\.\\s/, '');
        html += '<ol><li>' + item + '</li></ol>';
      } else {
        html += '<p>' + line + '</p>';
      }
    }

    if (inTable) {
      tableHtml += '</tbody></table>';
      html += tableHtml;
    }

    // 合并相邻的 ul 和 ol
    html = html.replace(/<\\/ul>\\s*<ul>/g, '');
    html = html.replace(/<\\/ol>\\s*<ol>/g, '');
    return html;
  }
</script>
</body>
</html>
"""


class ConversationWindow(Gtk.Window):
    """极简高颜值对话客户端浮窗"""

    WINDOW_WIDTH = 760
    WINDOW_HEIGHT = 580
    TOP_MARGIN = 118  # 置于超椭圆正下方

    def __init__(self):
        super().__init__(Gtk.WindowType.TOPLEVEL)

        self.set_title("Jarvis")
        self.set_default_size(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_role("JarvisConversationWindow")

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)

        self.set_app_paintable(True)
        self.connect("draw", self._on_draw)

        # 键盘快捷键监听 (如 Escape 关闭)
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("key-press-event", self._on_key_press)

        # 居中放置于顶部超椭圆下方
        screen_width = screen.get_width()
        left = round((screen_width - self.WINDOW_WIDTH) / 2)
        self.move(left, self.TOP_MARGIN)

        # WebKit2 视图
        self.webview = WebKit2.WebView()
        settings = self.webview.get_settings()
        settings.set_enable_javascript(True)
        settings.set_enable_developer_extras(False)
        self.webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))

        # 暴露 JavaScript 关闭调用
        user_content = self.webview.get_user_content_manager()
        user_content.register_script_message_handler("closeWindow")
        user_content.connect("script-message-received::closeWindow", self._on_close_requested)

        self.add(self.webview)
        resource_dir = os.getenv("JARVIS_RESOURCE_DIR", os.path.dirname(os.path.abspath(__file__)))
        html_file = os.path.abspath(os.path.join(resource_dir, "chat_ui.html"))
        self.webview.load_uri(f"file://{html_file}")
        self.is_visible_ui = False

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide_window()
            return True
        return False

    def _on_draw(self, widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0)  # CLEAR
        cr.paint()
        return False

    def _on_close_requested(self, user_content, message):
        GLib.idle_add(self.hide_window)

    def show_window(self):
        self.is_visible_ui = True
        self.show_all()
        self.present()

    def hide_window(self):
        self.is_visible_ui = False
        self.hide()

    def toggle(self):
        if self.is_visible_ui and self.get_visible():
            self.hide_window()
        else:
            self.show_window()

    def run_js(self, script: str):
        GLib.idle_add(self._eval_js, script)

    def _eval_js(self, script: str):
        try:
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        except Exception:
            pass
        return False

    def update_user_transcript(self, text: str, is_final: bool = False):
        safe_text = json.dumps(text)
        safe_final = "true" if is_final else "false"
        self.run_js(f"window.updateUserTranscript({safe_text}, {safe_final});")

    def show_tool_badge(self, tool_name: str, query: str = ""):
        safe_name = json.dumps(tool_name)
        safe_query = json.dumps(query)
        self.run_js(f"window.showToolBadge({safe_name}, {safe_query});")

    def append_ai_delta(self, delta: str):
        safe_delta = json.dumps(delta)
        self.run_js(f"window.appendAiDelta({safe_delta});")

    def finish_ai_turn(self):
        self.run_js("window.finishAiTurn();")


# 全局对话单例控制器
class ConversationUIController:
    """线程安全的对话视窗控制器"""

    def __init__(self):
        self.window = None

    def bind_window(self, window: ConversationWindow):
        self.window = window

    def show(self):
        if self.window:
            GLib.idle_add(self.window.show_window)

    def hide(self):
        if self.window:
            GLib.idle_add(self.window.hide_window)

    def toggle(self):
        if self.window:
            GLib.idle_add(self.window.toggle)

    def update_user_transcript(self, text: str, is_final: bool = False):
        if self.window:
            self.window.update_user_transcript(text, is_final)

    def show_tool_call(self, tool_name: str, query: str = ""):
        if self.window:
            self.window.show_tool_badge(tool_name, query)

    def append_ai_delta(self, delta: str):
        if self.window:
            self.window.append_ai_delta(delta)

    def finish_ai_turn(self):
        if self.window:
            self.window.finish_ai_turn()


conversation_controller = ConversationUIController()
