import ctypes
import os
import shutil
import subprocess
import threading
import time

import numpy as np


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(PROJECT_DIR, "libaec.so")


class _PipeWirePCMStream:
    """Small raw-PCM wrapper around pw-cat."""

    def __init__(self, process, mode):
        self.process = process
        self.mode = mode
        self.closed = False

    def read(self, frames):
        if self.mode != "record" or self.process.stdout is None:
            raise RuntimeError("PipeWire stream is not readable")

        expected = frames * 2
        chunks = bytearray()
        while len(chunks) < expected:
            chunk = self.process.stdout.read(expected - len(chunks))
            if not chunk:
                raise RuntimeError("PipeWire AEC capture stream closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def write(self, pcm16_bytes):
        if self.mode != "playback" or self.process.stdin is None:
            raise RuntimeError("PipeWire stream is not writable")
        self.process.stdin.write(pcm16_bytes)

    def close(self):
        if self.closed:
            return
        self.closed = True

        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except (BrokenPipeError, OSError):
                    pass

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()


class PipeWireWebRTCAEC:
    """Lifecycle-managed PipeWire WebRTC echo-cancel graph.

    The graph provides a virtual playback sink and an echo-cancelled microphone
    source.  It exists only while this object is alive and does not modify the
    user's persistent PipeWire configuration.
    """

    def __init__(self):
        suffix = str(os.getpid())
        self.capture_name = f"jarvis.aec.capture.{suffix}"
        self.source_name = f"jarvis.aec.source.{suffix}"
        self.sink_name = f"jarvis.aec.sink.{suffix}"
        self.playback_name = f"jarvis.aec.playback.{suffix}"
        self.cli_process = None
        self.streams = []
        self.active = False

    @staticmethod
    def is_supported():
        return all(
            shutil.which(command)
            for command in ("pw-cli", "pw-cat", "pw-dump")
        ) and os.path.exists(
            "/usr/lib/x86_64-linux-gnu/spa-0.2/aec/libspa-aec-webrtc.so"
        )

    def start(self, timeout=3.0):
        if self.active:
            return True
        if not self.is_supported():
            return False

        self.cli_process = subprocess.Popen(
            ["pw-cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

        command = (
            "load-module libpipewire-module-echo-cancel { "
            "library.name = aec/libspa-aec-webrtc "
            "audio.rate = 48000 audio.channels = 1 "
            f'capture.props = {{ node.name = "{self.capture_name}" }} '
            f'source.props = {{ node.name = "{self.source_name}" '
            'node.description = "Jarvis AEC Microphone" } '
            f'sink.props = {{ node.name = "{self.sink_name}" '
            'node.description = "Jarvis AEC Playback" } '
            f'playback.props = {{ node.name = "{self.playback_name}" }} '
            "}\n"
        )

        try:
            self.cli_process.stdin.write(command)
            self.cli_process.stdin.flush()
        except (AttributeError, BrokenPipeError, OSError):
            self.close()
            return False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cli_process.poll() is not None:
                break
            try:
                graph = subprocess.run(
                    ["pw-dump"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                ).stdout
            except (OSError, subprocess.TimeoutExpired):
                graph = ""

            if self.source_name in graph and self.sink_name in graph:
                self.active = True
                return True
            time.sleep(0.05)

        self.close()
        return False

    def _open_stream(self, mode, target, sample_rate):
        if not self.active:
            raise RuntimeError("PipeWire WebRTC AEC is not active")

        flag = "--playback" if mode == "playback" else "--record"
        process = subprocess.Popen(
            [
                "pw-cat",
                flag,
                "--target",
                target,
                "--latency",
                "20ms",
                "--rate",
                str(sample_rate),
                "--channels",
                "1",
                "--format",
                "s16",
                "-",
            ],
            stdin=subprocess.PIPE if mode == "playback" else subprocess.DEVNULL,
            stdout=subprocess.PIPE if mode == "record" else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        time.sleep(0.05)
        if process.poll() is not None:
            raise RuntimeError(f"Unable to open PipeWire AEC {mode} stream")

        stream = _PipeWirePCMStream(process, mode)
        self.streams.append(stream)
        return stream

    def open_playback(self, sample_rate):
        return self._open_stream("playback", self.sink_name, sample_rate)

    def open_capture(self, sample_rate):
        return self._open_stream("record", self.source_name, sample_rate)

    def close(self):
        for stream in self.streams:
            stream.close()
        self.streams.clear()

        if self.cli_process is not None:
            if self.cli_process.poll() is None:
                try:
                    if self.cli_process.stdin is not None:
                        self.cli_process.stdin.write("quit\n")
                        self.cli_process.stdin.flush()
                    self.cli_process.wait(timeout=1.0)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    self.cli_process.terminate()
            self.cli_process = None
        self.active = False


class AcousticEchoCanceller:
    """Application-level fallback for hosts without PipeWire WebRTC AEC."""

    def __init__(self, sample_rate=16000, delay_samples=320):
        self.sample_rate = sample_rate
        self.lib = ctypes.cdll.LoadLibrary(LIB_PATH)
        self.lib.aec_create.restype = ctypes.c_void_p
        self.lib.aec_destroy.argtypes = [ctypes.c_void_p]
        self.lib.aec_set_delay.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.aec_process.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_short),
            ctypes.POINTER(ctypes.c_short),
            ctypes.POINTER(ctypes.c_short),
            ctypes.c_int,
        ]

        self.handle = self.lib.aec_create()
        if not self.handle:
            raise RuntimeError("Unable to create fallback AEC engine")
        self.lib.aec_set_delay(self.handle, delay_samples)
        self.reference = bytearray()
        self.max_reference_bytes = sample_rate * 2 * 4
        self.lock = threading.Lock()

    def feed_reference_audio(self, pcm16_bytes, source_rate=16000):
        if not pcm16_bytes:
            return
        samples = np.frombuffer(pcm16_bytes, dtype=np.int16)
        if source_rate != self.sample_rate and len(samples):
            target_count = max(
                1,
                round(len(samples) * self.sample_rate / source_rate),
            )
            positions = np.linspace(0, len(samples) - 1, target_count)
            samples = np.interp(
                positions,
                np.arange(len(samples)),
                samples,
            ).astype(np.int16)

        with self.lock:
            self.reference.extend(samples.tobytes())
            overflow = len(self.reference) - self.max_reference_bytes
            if overflow > 0:
                del self.reference[:overflow]

    def cancel_echo(self, mic_pcm16_bytes):
        if not mic_pcm16_bytes:
            return b""

        mic_arr = np.frombuffer(mic_pcm16_bytes, dtype=np.int16)
        needed = len(mic_pcm16_bytes)
        with self.lock:
            available = min(needed, len(self.reference))
            reference_bytes = bytes(self.reference[:available])
            del self.reference[:available]
        reference_bytes += b"\x00" * (needed - available)
        ref_arr = np.frombuffer(reference_bytes, dtype=np.int16)
        clean_arr = np.empty(len(mic_arr), dtype=np.int16)

        self.lib.aec_process(
            self.handle,
            mic_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            ref_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            clean_arr.ctypes.data_as(ctypes.POINTER(ctypes.c_short)),
            len(mic_arr),
        )
        return clean_arr.tobytes()

    def reset(self):
        with self.lock:
            self.reference.clear()

    def close(self):
        if self.handle:
            self.lib.aec_destroy(self.handle)
            self.handle = None
