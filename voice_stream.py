"""
voice_stream.py

Streams microphone audio to a speech-to-text backend and reports interim +
final transcripts through a single callback interface, so intent_engine.py
doesn't need to know which backend is active.

Two backends:
  - DeepgramStreamer: wss://api.deepgram.com Nova-2, interim_results=True.
    Real streaming, ~150-300ms partials.
  - FasterWhisperStreamer: local fallback with no network dependency. It is
    NOT true streaming ASR (Whisper doesn't support that natively) - it runs
    short-window inference on a rolling audio buffer, which trades some
    latency for zero API cost / offline operation. Good enough for demoing
    the mid-sentence intent pipeline without credentials.

Both backends are driven from an asyncio event loop that should be run on a
background thread (see main.py); they call `on_transcript` which itself must
be safe to call from that thread (main.py marshals it onto the Qt thread via
a pyqtSignal before touching any widgets).
"""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_MS = 30
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_MS / 1000)

# on_transcript(text: str, is_final: bool, speech_final: bool, utterance_id: int)
TranscriptCallback = Callable[[str, bool, bool, int], None]


class _MicSource:
    """Wraps sounddevice's callback-based capture into an asyncio queue of PCM16 bytes."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self._stream: Optional[sd.RawInputStream] = None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"[voice_stream] mic status: {status}")
        data = bytes(indata)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)

    def start(self) -> None:
        self._stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            channels=CHANNELS,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def get(self) -> bytes:
        return await self._queue.get()


class DeepgramStreamer:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._running = False

    async def run(self, on_transcript: TranscriptCallback) -> None:
        import websockets

        url = (
            "wss://api.deepgram.com/v1/listen"
            "?model=nova-2&interim_results=true&endpointing=300"
            f"&encoding=linear16&sample_rate={SAMPLE_RATE}&channels={CHANNELS}"
        )
        headers = {"Authorization": f"Token {self._api_key}"}

        loop = asyncio.get_running_loop()
        mic = _MicSource(loop)
        self._running = True
        utterance_id = 0

        async with websockets.connect(url, additional_headers=headers) as ws:
            mic.start()

            async def sender():
                while self._running:
                    chunk = await mic.get()
                    await ws.send(chunk)

            async def receiver():
                nonlocal utterance_id
                async for message in ws:
                    payload = json.loads(message)
                    if payload.get("type") != "Results":
                        continue
                    alt = payload["channel"]["alternatives"][0]
                    text = alt.get("transcript", "")
                    if not text:
                        continue
                    is_final = payload.get("is_final", False)
                    speech_final = payload.get("speech_final", False)
                    on_transcript(text, is_final, speech_final, utterance_id)
                    if speech_final:
                        utterance_id += 1

            try:
                await asyncio.gather(sender(), receiver())
            finally:
                mic.stop()

    def stop(self) -> None:
        self._running = False


class FasterWhisperStreamer:
    """Rolling-window local fallback. Emits interim results every WINDOW_S
    seconds and a final result after SILENCE_S seconds of low RMS energy."""

    WINDOW_S = 2.0
    SILENCE_S = 1.0
    SILENCE_RMS_THRESHOLD = 300  # int16 RMS

    def __init__(self, model_size: str = "small.en") -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self._running = False

    async def run(self, on_transcript: TranscriptCallback) -> None:
        loop = asyncio.get_running_loop()
        mic = _MicSource(loop)
        mic.start()
        self._running = True

        buffer = bytearray()
        silence_accum = 0.0
        utterance_id = 0
        window_bytes = int(self.WINDOW_S * SAMPLE_RATE) * 2
        block_seconds = BLOCK_SIZE / SAMPLE_RATE

        try:
            while self._running:
                chunk = await mic.get()
                buffer.extend(chunk)

                samples = np.frombuffer(chunk, dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) if len(samples) else 0.0

                if rms < self.SILENCE_RMS_THRESHOLD:
                    silence_accum += block_seconds
                else:
                    silence_accum = 0.0

                if len(buffer) >= window_bytes:
                    text = self._transcribe(bytes(buffer))
                    if text:
                        on_transcript(text, False, False, utterance_id)

                if silence_accum >= self.SILENCE_S and len(buffer) > 0:
                    text = self._transcribe(bytes(buffer))
                    if text:
                        on_transcript(text, True, True, utterance_id)
                    utterance_id += 1
                    buffer.clear()
                    silence_accum = 0.0
        finally:
            mic.stop()

    def _transcribe(self, pcm_bytes: bytes) -> str:
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(audio, language="en", vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def stop(self) -> None:
        self._running = False


def make_streamer(deepgram_api_key: Optional[str], backend_override: Optional[str] = None):
    backend = backend_override or ("deepgram" if deepgram_api_key else "whisper")
    if backend == "deepgram":
        if not deepgram_api_key:
            raise RuntimeError("STT_BACKEND=deepgram but DEEPGRAM_API_KEY is not set")
        return DeepgramStreamer(deepgram_api_key)
    return FasterWhisperStreamer()
