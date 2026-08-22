import base64
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            _reconfigure(encoding="utf-8", errors="replace")

import dashscope
import numpy as np
import sounddevice as sd
import _webrtcvad
import webrtcvad
from dashscope.audio.qwen_omni import (
    MultiModality,
    OmniRealtimeCallback,
    OmniRealtimeConversation,
)
from dotenv import load_dotenv

from acoustic_echo_canceller import (
    AcousticEchoCanceller,
    PipeWireWebRTCAEC,
)
from history_manager import history_manager
from memory import memory_manager
from system_tools import TOOLS_DEFINITION, dispatch_tool_call
from wakeword import wait_for_wakeword


if os.name == "nt":
    class EdgeOverlayController:
        """Windows superellipse launcher and voice-to-desktop bridge.

        The orb owns the click target and starts the WebView2 desktop only on
        demand.  Voice events are streamed through a small JSONL file so a
        desktop window opened later can replay the current conversation.
        """

        handles_history = True

        def __init__(self):
            self.process = None
            self._lock = threading.RLock()
            self.sync_path = Path(tempfile.gettempdir()) / f"jarvis-windows-{os.getpid()}.jsonl"

        def start(self):
            with self._lock:
                if self.process is not None and self.process.poll() is None:
                    return self.process
                try:
                    self.sync_path.unlink(missing_ok=True)
                except OSError:
                    pass
                overlay_path = Path(__file__).with_name("windows_overlay.py")
                env = os.environ.copy()
                env["JARVIS_WINDOWS_SYNC_FILE"] = str(self.sync_path)
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                try:
                    self.process = subprocess.Popen(
                        [sys.executable, str(overlay_path)],
                        cwd=str(overlay_path.parent),
                        env=env,
                        stdin=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        creationflags=creationflags,
                    )
                except OSError as exc:
                    print(f"⚠️ 无法启动 Windows 超椭圆: {exc}")
                    self.process = None
                    return None
            return self.process

        def show(self):
            self.set_state("awake")

        def hide(self):
            self.set_state("hide")

        def set_state(self, state):
            self._send_cmd({"action": "state", "state": str(state)})

        def _send_cmd(self, payload):
            with self._lock:
                process = self.process
                stream = process.stdin if process is not None else None
                if process is None or process.poll() is not None or stream is None:
                    return
                try:
                    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    stream.flush()
                except (BrokenPipeError, OSError):
                    pass

        def update_user_transcript(self, text, is_final=False):
            if is_final and text.strip():
                try:
                    history_manager.add_message(
                        history_manager.get_active_session_id(),
                        "user",
                        text.strip(),
                    )
                except Exception:
                    pass
                self._send_cmd({"action": "turn_start"})
            self._send_cmd({"action": "user_text", "text": text, "final": bool(is_final)})

        def append_ai_delta(self, delta):
            self._send_cmd({"action": "ai_delta", "delta": delta})

        def finish_ai_turn(self, full_text=""):
            if full_text.strip():
                try:
                    history_manager.add_message(
                        history_manager.get_active_session_id(),
                        "assistant",
                        full_text.strip(),
                    )
                except Exception:
                    pass
            self._send_cmd({"action": "ai_finish", "text": full_text})
            self._send_cmd({"action": "turn_complete"})

        def show_tool_call(self, tool_name, query=""):
            self._send_cmd({"action": "tool_call", "name": tool_name, "query": query})

        def close(self):
            with self._lock:
                process = self.process
                self.process = None
            if process is None:
                return None
            try:
                if process.stdin is not None:
                    process.stdin.write(json.dumps({"action": "quit"}) + "\n")
                    process.stdin.flush()
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                self.sync_path.unlink(missing_ok=True)
            except OSError:
                pass
else:
    from edge_overlay import EdgeOverlayController


load_dotenv()


# ==========================
# Qwen-Omni Realtime 配置
# ==========================

MODEL_NAME = os.getenv(
    "QWEN_OMNI_MODEL",
    "qwen3.5-omni-flash-realtime",
)

VOICE = os.getenv(
    "QWEN_OMNI_VOICE",
    "Tina",
)

INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000

INSTRUCTIONS = """
你叫 Jarvis，是一个拥有全方位电脑控制、联网能力与长期记忆的智能桌面 AI 语音管家。
请始终使用简体中文回答。
回答自然、直接、简洁，适合口语播放。
不要使用 Markdown、列表符号或其他特殊格式。

你可以在用户启用的安全权限范围内控制当前电脑、实时访问互联网并使用长期个性化记忆。你拥有以下工具：
1. manage_memory: 管理和使用关于用户的长期个性化记忆（记住用户的喜好、生日、习惯、事实、规则，或回忆检索已记住的信息，或遗忘指定信息）。当用户说“帮我记住...”、“你还记得我喜欢什么吗”、“忘掉关于...”时主动调用！
2. set_home_location: 设置或记住用户的真实常驻城市（如深圳、北京、上海等），永久解决开启 VPN 代理时定位漂移的问题。当用户说“我在深圳”、“记住我的城市是北京”、“把默认城市设为上海”时主动调用！
3. get_location: 获取当前电脑/用户所在的实时地理位置（国家、省份、城市、区县、经纬度坐标、时区、公网IP与运营商）。当用户询问“我在哪”、“我的位置”、“我们在哪个城市”时，必须主动调用此工具！
4. get_weather: 查询指定城市或当前位置的实时天气、气温、体感温度、湿度、风力与天气预报。当用户询问天气、气温、下雨情况时，必须立即调用此工具！
5. web_search: 在互联网上实时搜索新闻、技术文档、百科、即时知识等，获取最新资讯。
6. browser_agent: 浏览器 Agent 自动化，支持抓取任意网页正文提取阅读、在各大平台（B站、YouTube、GitHub、知乎、百度等）精准检索视频/内容或打开网页。
7. execute_shell_command: 在电脑终端执行 Shell/Bash 命令；此高风险能力默认关闭，只有用户显式配置后才可用。
8. open_application: 启动任何应用程序（浏览器、VS Code、终端、计算器、设置等）或打开指定网页与文件。
9. control_system: 控制电脑音量（调大/调小/静音/设置百分比）、调节屏幕亮度、控制媒体播放、锁屏、睡眠等。
10. gui_control: 模拟键盘快捷键（Ctrl+C, Ctrl+V, Super, Alt+Tab等）、输入文本、点击鼠标。调用输入文本时，text 参数只能包含用户要求输入的正文，不要带开头句号“。”、命令词“输入”或解释文字。
11. get_system_status: 实时查询 CPU、内存、磁盘占用、当前活动窗口、系统时间等硬件与运行状态。
12. manage_files: 读取文件内容、创建文件或列出目录；覆盖、追加现有文件和删除默认关闭，只有用户显式配置后才可用。

当用户的语音指令涉及记忆管理、常驻城市设置、位置定位、天气查询、联网搜索、查看网页、查询最新资讯、控制电脑、打开软件、调节音量、查询系统、操作文件、执行命令等操作时，请立即主动调用对应的工具执行，执行完成后口头向用户简明汇报结果。
""".strip()


dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")


def _require_dashscope_api_key():
    """在真正建立云端会话前校验密钥，允许离线单元测试导入本模块。"""
    if not str(dashscope.api_key or "").strip():
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")


def _resample_pcm16(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample a short mono PCM16 block without requiring platform codecs."""
    if source_rate == target_rate or not len(samples):
        return samples.astype(np.int16, copy=False)
    target_count = max(1, round(len(samples) * target_rate / source_rate))
    positions = np.linspace(0, len(samples) - 1, target_count)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.int16)

# ==========================
# 异步低延迟音频播放引擎
# ==========================

class AsyncAudioPlayer:
    """可立即清空的非阻塞播放引擎，同时维护 AEC 远端参考。"""

    def __init__(
        self,
        target_sample_rate=OUTPUT_SAMPLE_RATE,
        pipewire_aec=None,
        software_aec=None,
    ):
        self.device_sample_rate = target_sample_rate
        self.target_sample_rate = target_sample_rate
        self.pipewire_aec = pipewire_aec
        self.software_aec = software_aec
        self.generation = 0
        self.generation_lock = threading.Lock()
        self.stream_lock = threading.Lock()
        self.level_lock = threading.Lock()
        self.last_reference_rms = 0.0
        self.last_reference_time = 0.0
        # Set as soon as a response chunk is queued, not only when the audio
        # thread reaches the device.  This gives the capture loop a reliable
        # half-duplex gate and prevents the first speaker frames leaking back
        # into the microphone buffer.
        self.output_active = threading.Event()

        if pipewire_aec is not None and pipewire_aec.active:
            self.stream = pipewire_aec.open_playback(target_sample_rate)
        else:
            # 检查硬件是否原生支持 24kHz，若不支持则自动采用 48kHz。
            try:
                sd.check_output_settings(samplerate=target_sample_rate)
            except Exception:
                try:
                    info = sd.query_devices(kind="output")
                    default_rate = float(info.get("default_samplerate", 48000))
                    self.device_sample_rate = int(round(default_rate))
                except Exception:
                    self.device_sample_rate = 48000

            self.stream = sd.RawOutputStream(
                samplerate=self.device_sample_rate,
                channels=1,
                dtype="int16",
            )
            self.stream.start()

        self.queue = queue.Queue()
        self.running = True
        self.is_writing = False
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _worker(self):
        while self.running:
            try:
                item = self.queue.get(timeout=0.04)
                if item is None:
                    break
                generation, chunk = item
                with self.generation_lock:
                    current_generation = self.generation
                if generation != current_generation:
                    self.queue.task_done()
                    if self.queue.empty():
                        self.output_active.clear()
                    continue

                self.is_writing = True
                samples = np.frombuffer(chunk, dtype=np.int16)
                if len(samples):
                    rms = float(
                        np.sqrt(np.mean(samples.astype(np.float32) ** 2))
                    )
                    with self.level_lock:
                        self.last_reference_rms = rms
                        self.last_reference_time = time.monotonic()

                if self.software_aec is not None:
                    self.software_aec.feed_reference_audio(
                        chunk,
                        source_rate=self.target_sample_rate,
                    )

                if self.device_sample_rate != self.target_sample_rate:
                    chunk = _resample_pcm16(
                        samples,
                        self.target_sample_rate,
                        self.device_sample_rate,
                    ).tobytes()
                try:
                    with self.stream_lock:
                        if self.running and hasattr(self.stream, "write"):
                            self.stream.write(chunk)
                except (BrokenPipeError, OSError, RuntimeError):
                    pass
                self.queue.task_done()
                if self.queue.empty():
                    self.output_active.clear()
            except queue.Empty:
                self.is_writing = False
                self.output_active.clear()

    def play(self, audio_bytes: bytes):
        if not self.running or not audio_bytes:
            return
        with self.generation_lock:
            generation = self.generation
        self.output_active.set()
        self.queue.put((generation, audio_bytes))

    def interrupt(self):
        """丢弃尚未播放的当前响应音频并立刻截断硬件发声。"""
        with self.generation_lock:
            self.generation += 1
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except (queue.Empty, ValueError):
                break

        with self.stream_lock:
            if self.pipewire_aec is not None and self.pipewire_aec.active:
                try:
                    self.stream.close()
                    self.stream = self.pipewire_aec.open_playback(
                        self.target_sample_rate
                    )
                except Exception:
                    pass
            elif hasattr(self.stream, "abort"):
                try:
                    self.stream.abort()
                    self.stream.start()
                except Exception:
                    pass
        self.is_writing = False
        self.output_active.clear()

    def recent_reference_rms(self):
        with self.level_lock:
            if time.monotonic() - self.last_reference_time > 0.15:
                return 0.0
            return self.last_reference_rms

    def is_playing(self):
        return self.is_writing or not self.queue.empty()

    def wait_drained(self, timeout=15.0):
        t0 = time.time()
        while self.is_playing() and (time.time() - t0 < timeout):
            time.sleep(0.03)

    def close(self):
        self.running = False
        self.output_active.clear()
        self.interrupt()
        self.queue.put(None)
        try:
            with self.stream_lock:
                if hasattr(self.stream, "stop"):
                    self.stream.stop()
                self.stream.close()
        except Exception:
            pass


# ==========================
# 实时事件回调与多轮对话
# ==========================

class RealtimeCallback(OmniRealtimeCallback):

    def __init__(self, player: AsyncAudioPlayer, state_callback, overlay=None):

        super().__init__()

        self.player = player
        self.set_state = state_callback
        self.overlay = overlay
        self.session_ready = threading.Event()
        self.speech_stopped = threading.Event()
        self.response_done = threading.Event()
        self.connection_closed = threading.Event()
        self.error = None
        self.printing_answer = False
        self.playing_audio = False
        self.conversation = None
        self.executed_calls = set()
        self.user_speaking = False
        self.waiting_for_response = False
        self.response_active = False
        self.active_tool_count = 0
        self.submitted_tool_count = 0
        self.is_tool_running = False
        self.awaiting_tool_followup = False
        self.tool_state_lock = threading.Lock()
        self.current_ai_text = ""

    def prepare_turn(self):

        self.speech_stopped.clear()
        self.response_done.clear()
        self.error = None
        self.printing_answer = False
        self.playing_audio = False
        self.executed_calls.clear()
        self.submitted_tool_count = 0
        self.user_speaking = False
        self.waiting_for_response = False
        self.awaiting_tool_followup = False
        self.current_ai_text = ""

    def on_open(self):

        print(f"✅ 已连接 {MODEL_NAME}")

    def _execute_tool_async(self, call_id: str, name: str, arguments: str):
        with self.tool_state_lock:
            if call_id in self.executed_calls:
                return
            self.executed_calls.add(call_id)
            self.active_tool_count += 1
            self.is_tool_running = True
            self.awaiting_tool_followup = True
        self.waiting_for_response = True
        self.set_state("thinking")
        if self.overlay:
            self.overlay.show_tool_call(name, arguments)

        def worker():
            item_submitted = False
            try:
                result = dispatch_tool_call(name, arguments)
                print(f"📋 工具执行完成，正在向模型返回结果...")

                if self.conversation is not None:
                    # Realtime 协议要求上一响应结束后再提交工具结果并创建后续响应。
                    self.response_done.wait(timeout=15.0)
                    self.conversation.create_item({
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(result),
                    })
                    item_submitted = True
            except Exception as e:
                print(f"❌ 工具执行异常: {e}")
            finally:
                with self.tool_state_lock:
                    if item_submitted:
                        self.submitted_tool_count += 1
                    self.active_tool_count = max(0, self.active_tool_count - 1)
                    is_last_tool = self.active_tool_count == 0
                    has_submitted_tools = self.submitted_tool_count > 0
                    if is_last_tool:
                        self.is_tool_running = False
                        self.submitted_tool_count = 0

                if is_last_tool and has_submitted_tools and self.conversation is not None:
                    try:
                        self.conversation.create_response()
                    except Exception as e:
                        print(f"❌ 无法创建工具后续响应: {e}")
                        self.awaiting_tool_followup = False
                        self.waiting_for_response = False
                elif is_last_tool and not has_submitted_tools:
                    self.awaiting_tool_followup = False
                    self.waiting_for_response = False

        threading.Thread(target=worker, daemon=True).start()

    def on_event(self, event):

        # DashScope 1.27 parses WebSocket JSON before invoking the callback.
        # Keep string support for older SDKs, but never json.loads() a dict.
        if isinstance(event, dict):
            response = event
        elif isinstance(event, str):
            try:
                response = json.loads(event)
            except json.JSONDecodeError:
                return
        else:
            return

        event_type = response.get("type", "")

        if event_type == "session.updated":
            self.session_ready.set()

        elif event_type == "response.created":
            self.response_active = True
            self.waiting_for_response = False
            self.response_done.clear()
            if self.awaiting_tool_followup:
                self.awaiting_tool_followup = False

        # 用户开始发声：立刻截断上一轮旧音频，停止扬声器旧声音！
        elif event_type == "input_audio_buffer.speech_started":
            self.player.interrupt()
            if self.conversation is not None and self.response_active:
                try:
                    self.conversation.cancel_response()
                except Exception:
                    pass
            self.response_active = False
            self.playing_audio = False
            self.user_speaking = True
            self.waiting_for_response = False
            self.set_state("listening")
            self.speech_stopped.clear()
            print("\n🎤 正在聆听...")

        # 用户停止发声
        elif event_type == "input_audio_buffer.speech_stopped":
            self.user_speaking = False
            self.waiting_for_response = True
            self.speech_stopped.set()
            self.set_state("thinking")
            print("🧠 Qwen 正在思考与处理...")

        elif event_type == "conversation.item.input_audio_transcription.delta":
            preview = response.get("text", "") + response.get("stash", "")
            if preview:
                print(f"\r你说: {preview}", end="", flush=True)
                if self.overlay:
                    self.overlay.update_user_transcript(preview, is_final=False)

        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = response.get("transcript", "").strip()
            if transcript:
                print(f"\r你说: {transcript}")
                if self.overlay:
                    self.overlay.update_user_transcript(transcript, is_final=True)
                else:
                    try:
                        active_sid = history_manager.get_active_session_id()
                        history_manager.add_message(active_sid, "user", transcript)
                    except Exception:
                        pass

        # Function Call 处理
        elif event_type in ["response.function_call_arguments.done", "response.output_item.done"]:
            item = response.get("item", {})
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments", "")
                if call_id and name:
                    self._execute_tool_async(call_id, name, arguments)
            elif event_type == "response.function_call_arguments.done":
                call_id = response.get("call_id")
                name = response.get("name")
                arguments = response.get("arguments", "")
                if call_id and name:
                    self._execute_tool_async(call_id, name, arguments)

        elif event_type == "response.audio_transcript.delta":
            delta = response.get("delta", "")
            if delta:
                self.current_ai_text += delta
                if not self.printing_answer:
                    print("\n🤖 AI: ", end="", flush=True)
                    self.printing_answer = True
                print(delta, end="", flush=True)
                if self.overlay:
                    self.overlay.append_ai_delta(delta)

        # 接收到下行音频数据
        elif event_type == "response.audio.delta":
            self.waiting_for_response = False
            if not self.playing_audio:
                self.set_state("speaking")
                self.playing_audio = True

            audio = base64.b64decode(response.get("delta", ""))
            if audio:
                self.player.play(audio)

        # 服务端生成完毕
        elif event_type == "response.done":
            if self.awaiting_tool_followup:
                self.playing_audio = False
                self.response_active = False
                self.waiting_for_response = True
                self.response_done.set()
                return
            if self.printing_answer:
                print()
            completed_text = self.current_ai_text
            if completed_text and not self.overlay:
                try:
                    active_sid = history_manager.get_active_session_id()
                    history_manager.add_message(active_sid, "assistant", completed_text)
                except Exception:
                    pass
            self.playing_audio = False
            self.response_active = False
            self.waiting_for_response = False
            self.response_done.set()
            if self.overlay:
                self.overlay.finish_ai_turn(completed_text)
            self.current_ai_text = ""
            self.printing_answer = False

        elif event_type == "error":
            error = response.get("error", response)
            err_msg = str(error)
            if "already has an active response" in err_msg:
                # 忽略服务端活动响应重叠告警
                return
            if "response_idle_timeout" in err_msg or "300 seconds" in err_msg:
                # 服务端 300 秒空闲自动休眠断开，属于正常节电机制，静默标记并在下次唤醒时自动重连
                self.connection_closed.set()
                self.response_active = False
                self.waiting_for_response = False
                return
            self.error = RuntimeError(f"Qwen Realtime 错误: {error}")
            self.response_active = False
            self.waiting_for_response = False
            self.is_tool_running = False
            self.set_state("error")
            print(f"\n❌ {self.error}")
            self.speech_stopped.set()
            self.response_done.set()

    def on_close(self, close_status_code, close_msg):

        self.connection_closed.set()
        self.speech_stopped.set()
        self.response_done.set()
        self.response_active = False
        self.waiting_for_response = False

        if close_status_code and close_status_code not in [1000, 1005, 1006]:
            self.set_state("error")
            self.error = RuntimeError(
                f"Qwen Realtime 连接已关闭: "
                f"{close_status_code} {close_msg}"
            )


# ==========================
# 端到端实时语音助手
# ==========================

class QwenRealtimeAssistant:

    def __init__(self, state_callback, overlay=None):

        self.set_state = state_callback
        self.overlay = overlay
        self.pipewire_aec = PipeWireWebRTCAEC()
        self.software_aec = None
        self.half_duplex_echo_guard = False

        if self.pipewire_aec.start():
            print("✅ WebRTC AEC3 全双工声学链路已启用")
        else:
            self.pipewire_aec.close()
            try:
                self.software_aec = AcousticEchoCanceller(
                    sample_rate=INPUT_SAMPLE_RATE,
                    delay_samples=320,
                )
                print("⚠️ WebRTC AEC 不可用，已启用应用内 AEC 回退")
            except (OSError, RuntimeError):
                self.half_duplex_echo_guard = True
                print("⚠️ AEC 不可用，已启用半双工回声保护")

        self.player = AsyncAudioPlayer(
            OUTPUT_SAMPLE_RATE,
            pipewire_aec=self.pipewire_aec,
            software_aec=self.software_aec,
        )
        self.callback = RealtimeCallback(
            self.player,
            state_callback,
            overlay=overlay,
        )
        self.conversation = None

    def connect(self):

        _require_dashscope_api_key()

        if self.conversation is not None:
            try:
                self.conversation.close()
            except Exception:
                pass
            self.conversation = None

        memory_ctx = memory_manager.get_system_prompt_context()
        session_instructions = INSTRUCTIONS + (
            f"\n{memory_ctx}" if memory_ctx else ""
        )
        last_error = None

        for attempt in range(1, 4):
            print(
                f"🌐 正在连接 {MODEL_NAME} 并注册全套系统控制工具..."
                f"（{attempt}/3）"
            )
            self.callback.error = None
            self.callback.session_ready.clear()
            self.callback.connection_closed.clear()

            try:
                self.conversation = OmniRealtimeConversation(
                    model=MODEL_NAME,
                    callback=self.callback,
                )
                self.callback.conversation = self.conversation
                self.conversation.connect()
                self.conversation.update_session(
                    output_modalities=[
                        MultiModality.TEXT,
                        MultiModality.AUDIO,
                    ],
                    voice=VOICE,
                    instructions=session_instructions,
                    enable_input_audio_transcription=True,
                    enable_turn_detection=True,
                    turn_detection_type="semantic_vad",
                    turn_detection_threshold=0.55,
                    prefix_padding_ms=300,
                    turn_detection_silence_duration_ms=750,
                    temperature=0.7,
                    max_tokens=300,
                    tools=TOOLS_DEFINITION,
                )

                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if self.callback.session_ready.wait(timeout=0.2):
                        return
                    if self.callback.error is not None:
                        raise self.callback.error
                    if self.callback.connection_closed.is_set():
                        raise ConnectionError(
                            "Qwen Realtime 在会话初始化期间关闭连接"
                        )
                raise TimeoutError(
                    "WebSocket 已连接，但 20 秒内未收到 session.updated"
                )
            except (ConnectionError, RuntimeError, TimeoutError, OSError) as error:
                last_error = error
                print(f"⚠️ Qwen 会话初始化失败: {error}")
                if self.conversation is not None:
                    try:
                        self.conversation.close()
                    except Exception:
                        pass
                    self.callback.connection_closed.wait(timeout=0.5)
                self.conversation = None
                self.callback.conversation = None
                if attempt < 3:
                    time.sleep(float(attempt))

        raise ConnectionError(
            f"Qwen Realtime 连续 3 次初始化失败: {last_error}"
        )

    def ensure_connected(self):

        if (
            self.conversation is None
            or self.callback.connection_closed.is_set()
            or self.callback.error is not None
        ):
            self.connect()

    def listen_and_respond(self):
        """AEC 纯净多轮交互：工具执行与模型回答不被打断，播报完毕后 5 秒内可连续对话。"""
        self.ensure_connected()
        self.callback.prepare_turn()
        self.set_state("listening")

        print("🎤 正在聆听您的指令（播报完毕后 5 秒内可连续追问）...")

        if self.pipewire_aec.active:
            capture_sample_rate = INPUT_SAMPLE_RATE
            downsample_factor = 1
            block_size = INPUT_SAMPLE_RATE * 20 // 1000
            microphone_context = self.pipewire_aec.open_capture(
                INPUT_SAMPLE_RATE
            )
        else:
            capture_sample_rate = INPUT_SAMPLE_RATE
            try:
                sd.check_input_settings(samplerate=INPUT_SAMPLE_RATE)
            except Exception:
                try:
                    info = sd.query_devices(kind="input")
                    default_rate = float(info.get("default_samplerate", 48000))
                    capture_sample_rate = int(round(default_rate))
                except Exception:
                    capture_sample_rate = 48000

            block_size = max(1, capture_sample_rate * 20 // 1000)
            microphone_context = sd.RawInputStream(
                samplerate=capture_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=block_size,
            )

        with microphone_context as microphone:

            silence_start_time = None
            has_responded = False
            session_start_time = time.time()
            microphone_muted = False
            resume_capture_after = 0.0

            while True:
                read_result = microphone.read(block_size)
                if isinstance(read_result, tuple):
                    raw_data, _overflowed = read_result
                else:
                    raw_data = read_result

                if self.callback.connection_closed.is_set():
                    break

                # 提取 16kHz PCM 数据
                samples = np.frombuffer(raw_data, dtype=np.int16)
                samples = _resample_pcm16(
                    samples,
                    capture_sample_rate,
                    INPUT_SAMPLE_RATE,
                )
                audio_16k_bytes = samples.tobytes()

                # 没有 AEC 时，AI 播报期间暂停上传麦克风，避免扬声器回声自激。
                output_active = (
                    self.callback.playing_audio
                    or self.player.is_playing()
                    or self.player.output_active.is_set()
                )
                if self.half_duplex_echo_guard and output_active:
                    if not microphone_muted:
                        microphone_muted = True
                        if self.software_aec is not None:
                            self.software_aec.reset()
                    # Keep draining the OS input buffer while muted so that a
                    # resumed stream cannot replay speaker echo accumulated in
                    # PortAudio's input queue.
                    continue

                if microphone_muted:
                    microphone_muted = False
                    resume_capture_after = time.monotonic() + 0.12
                    if self.software_aec is not None:
                        self.software_aec.reset()

                if self.software_aec is not None:
                    audio_16k_bytes = self.software_aec.cancel_echo(
                        audio_16k_bytes
                    )

                # Discard the short tail of the speaker after playback stops;
                # this avoids reopening the gate on the last device buffer.
                if (
                    self.half_duplex_echo_guard
                    and time.monotonic() < resume_capture_after
                ):
                    continue

                if self.conversation is not None:
                    self.conversation.append_audio(
                        base64.b64encode(audio_16k_bytes).decode("ascii")
                    )

                # 判断系统当前是否正处于用户发声、等待模型、工具执行、思考或语音播报中
                is_busy = (
                    self.callback.user_speaking
                    or self.callback.waiting_for_response
                    or self.callback.is_tool_running
                    or self.callback.active_tool_count > 0
                    or self.callback.response_active
                    or self.callback.playing_audio
                    or self.player.is_playing()
                )

                now = time.time()
                if is_busy:
                    # 只要系统在处理、执行工具、回答或用户在说话，完全冻结倒计时并刷新会话时间戳
                    silence_start_time = None
                    session_start_time = now
                    if self.callback.playing_audio or self.player.is_playing() or self.callback.response_active:
                        has_responded = True
                else:
                    if has_responded:
                        # 阶段二：AI 已经回答过至少一轮，进入 5 秒多轮连续对话窗口
                        if silence_start_time is None:
                            silence_start_time = now
                            self.set_state("listening")
                            print("\n⏳ 保持聆听中...（5秒内可直接继续说话，无声将自动隐退）")
                        elif now - silence_start_time >= 5.0:
                            print("\n💤 超过 5 秒未检测到说话，会话结束，超椭圆自动隐退。")
                            self.set_state("hide")
                            break
                    else:
                        # 阶段一：刚唤醒，等待用户说出第一句指令（给予充足的 10 秒思考与发言时间）
                        if now - session_start_time >= 10.0:
                            print("\n💤 唤醒后未检测到指令，超椭圆自动隐退。")
                            self.set_state("hide")
                            break

        # 会话结束，立即隐退超椭圆悬浮球
        self.set_state("hide")

        if self.callback.error:
            raise self.callback.error

    def close(self):

        if self.conversation is not None:
            self.conversation.close()

        self.player.close()

        if self.software_aec is not None:
            self.software_aec.close()

        self.pipewire_aec.close()


# ==========================
# 主循环
# ==========================

def main():

    overlay = EdgeOverlayController()
    overlay.start()
    if os.name != "nt":
        overlay.hide()
    if os.name == "nt":
        print("🎙️ Jarvis Windows 语音服务已启动（超椭圆悬浮球已打开，点击可显示桌面）")
    else:
        print("🔮 Jarvis 玻璃球界面已启动（隐藏待唤醒）")
    assistant = None

    try:
        assistant = QwenRealtimeAssistant(overlay.set_state, overlay=overlay)
        print("✅ 离线唤醒引擎已就绪；唤醒后再连接实时语音")

        while True:
            if os.name != "nt":
                overlay.hide()
            # 增加 300ms 声学回声隔离保护，确保扬声器残余回声不会被唤醒词模型误触发
            time.sleep(0.3)
            wait_for_wakeword()
            overlay.show()
            overlay.set_state("awake")
            assistant.listen_and_respond()
            overlay.hide()

    except KeyboardInterrupt:

        print("\n退出 AI")

    finally:

        if assistant is not None:
            assistant.close()

        overlay.close()


if __name__ == "__main__":

    main()
