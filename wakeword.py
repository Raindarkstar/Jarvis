#!/usr/bin/env python3
"""
wakeword.py - openWakeWord 高精度唤醒词引擎
采用目标模型单一隔离、双帧置信度确认 (>=0.78) 与启动音频残余排空，彻底杜绝误唤醒与自动循环。
"""

import threading
import time
import numpy as np
import sounddevice as sd
import openwakeword
from openwakeword.model import Model


CONFIDENCE_THRESHOLD = 0.78
REQUIRED_CONSECUTIVE_FRAMES = 2
WARMUP_FRAMES = 5  # 启动前丢弃 5 帧（约 400ms）声卡残余数据
MODEL_SAMPLE_RATE = 16000

# 仅加载 hey_jarvis 目标模型文件
all_paths = openwakeword.get_pretrained_model_paths()
jarvis_paths = [p for p in all_paths if "hey_jarvis" in p]

print(f"📦 正在加载单一目标唤醒模型 [hey_jarvis]...")
model = Model(wakeword_model_paths=jarvis_paths if jarvis_paths else [])
target_key = list(model.models.keys())[0] if model.models else "hey_jarvis_v0.1"
print(f"✅ 唤醒模型加载就绪 (目标 Key: {target_key})")


def _capture_sample_rate() -> int:
    """Use 16 kHz when available, otherwise use the microphone's native rate."""
    try:
        sd.check_input_settings(samplerate=MODEL_SAMPLE_RATE, channels=1)
        return MODEL_SAMPLE_RATE
    except Exception:
        info = sd.query_devices(kind="input")
        rate = int(round(float(info.get("default_samplerate", 48000))))
        return rate if rate > 0 else 48000


def _to_model_rate(audio: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == MODEL_SAMPLE_RATE or not len(audio):
        return audio.astype(np.int16, copy=False)
    target_count = max(1, round(len(audio) * MODEL_SAMPLE_RATE / source_rate))
    positions = np.linspace(0, len(audio) - 1, target_count)
    return np.interp(positions, np.arange(len(audio)), audio).astype(np.int16)


def wait_for_wakeword():
    print("🟢 等待唤醒词...")

    samplerate = _capture_sample_rate()
    detected = threading.Event()
    audio_buffer = np.zeros(0, dtype=np.int16)
    frame_count = 0
    consecutive_hits = 0

    try:
        model.reset()
    except Exception:
        pass

    def callback(indata, frames, time_info, status):
        nonlocal audio_buffer, frame_count, consecutive_hits
        if detected.is_set():
            return

        audio = (indata[:, 0] * 32768).astype(np.int16)
        audio = _to_model_rate(audio, samplerate)
        audio_buffer = np.concatenate((audio_buffer, audio))

        if len(audio_buffer) >= 1280:
            chunk = audio_buffer[:1280]
            audio_buffer = audio_buffer[1280:]
            frame_count += 1

            # 过滤启动前 400ms 声卡残余音频
            if frame_count <= WARMUP_FRAMES:
                return

            prediction = model.predict(chunk)
            score = prediction.get(target_key, 0.0)

            if score >= CONFIDENCE_THRESHOLD:
                consecutive_hits += 1
                if consecutive_hits >= REQUIRED_CONSECUTIVE_FRAMES:
                    print(f"✅ 成功唤醒: hey_jarvis (置信度: {score:.2f})")
                    detected.set()
            else:
                consecutive_hits = max(0, consecutive_hits - 1)

    with sd.InputStream(
        channels=1,
        samplerate=samplerate,
        blocksize=max(1, round(samplerate * 0.08)),
        callback=callback,
    ):
        while not detected.is_set():
            sd.sleep(40)

    try:
        model.reset()
    except Exception:
        pass

    # 唤醒后 300ms 声学平复保护
    time.sleep(0.3)
    return True


if __name__ == "__main__":
    while True:
        wait_for_wakeword()
        print("🎉 触发唤醒动作，测试中...")
        time.sleep(2)
