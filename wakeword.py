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

# 仅加载 hey_jarvis 目标模型文件
all_paths = openwakeword.get_pretrained_model_paths()
jarvis_paths = [p for p in all_paths if "hey_jarvis" in p]

print(f"📦 正在加载单一目标唤醒模型 [hey_jarvis]...")
model = Model(wakeword_model_paths=jarvis_paths if jarvis_paths else [])
target_key = list(model.models.keys())[0] if model.models else "hey_jarvis_v0.1"
print(f"✅ 唤醒模型加载就绪 (目标 Key: {target_key})")


def wait_for_wakeword():
    print("🟢 等待唤醒词...")

    samplerate = 16000
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
        blocksize=1280,
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
