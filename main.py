"""
main.py

App entry point. Wires together:
  - PillOverlay (Qt UI, main thread)
  - a background thread running the asyncio STT streamer (voice_stream.py)
  - IntentEngine (regex + optional LLM fallback, dispatches to windows_actions)
  - listening_control.py: gates *when* the pipeline is allowed to listen
    (push-to-talk hotkeys, or a wake-word text filter), so the mic isn't
    streamed/acted on unconditionally.

All cross-thread communication into the UI goes through pyqtSignal, since
Qt widgets may only be touched from the thread that owns them. This applies
to three different background threads here: the asyncio voice thread, the
IntentEngine's ThreadPoolExecutor, and pynput's global-hotkey listener
thread (LISTENING_MODE=ptt only).
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

from dotenv import load_dotenv
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from intent_engine import IntentEngine
from listening_control import PushToTalkController, WakeWordGate
from overlay_widget import PillOverlay
from voice_stream import make_streamer


class VoicePipelineBridge(QObject):
    """Marshals events from background threads onto the Qt thread."""

    transcript_received = pyqtSignal(str, bool, bool, int)
    action_started = pyqtSignal(str)
    listening_changed = pyqtSignal(bool)
    ptt_engaged = pyqtSignal()
    ptt_released = pyqtSignal()


class VoiceThread(threading.Thread):
    def __init__(self, bridge: VoicePipelineBridge) -> None:
        super().__init__(daemon=True)
        self._bridge = bridge
        self._loop: asyncio.AbstractEventLoop | None = None
        self._streamer = None

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        deepgram_key = os.environ.get("DEEPGRAM_API_KEY") or None
        backend_override = os.environ.get("STT_BACKEND") or None
        self._streamer = make_streamer(deepgram_key, backend_override)

        self._bridge.listening_changed.emit(True)

        def on_transcript(text: str, is_final: bool, speech_final: bool, utterance_id: int) -> None:
            self._bridge.transcript_received.emit(text, is_final, speech_final, utterance_id)

        try:
            self._loop.run_until_complete(self._streamer.run(on_transcript))
        except Exception as exc:  # noqa: BLE001
            print(f"[main] voice thread stopped: {exc}")
        finally:
            self._bridge.listening_changed.emit(False)

    def stop(self) -> None:
        if self._streamer is not None:
            self._streamer.stop()


def build_tray_icon(app: QApplication, on_quit) -> QSystemTrayIcon:
    icon = app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Voice Assistant")
    menu = QMenu()
    quit_action = QAction("Quit")
    quit_action.triggered.connect(on_quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.show()
    return tray


def main() -> int:
    load_dotenv()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    overlay = PillOverlay()
    overlay.show()

    bridge = VoicePipelineBridge()

    def dispatch_action_label(label: str) -> None:
        bridge.action_started.emit(label)

    intent_engine = IntentEngine(on_action_started=dispatch_action_label)

    # Note: bridge.listening_changed (fired the instant the streamer object
    # is constructed, before any audio is even heard) is intentionally NOT
    # wired to overlay.set_listening - both PTT and wake-word mode drive the
    # overlay's state explicitly (flash_active() / set_listening(False)) at
    # the moments that actually matter (hotkey press, wake word heard,
    # utterance done). Connecting it here as well would immediately
    # overwrite the "ANIMUS LISTENING" pop with the plain "Listening..."
    # state within milliseconds, since the streamer starts well before any
    # words are transcribed.
    bridge.action_started.connect(overlay.show_action)

    listening_mode = os.environ.get("LISTENING_MODE", "ptt").lower()
    wake_gate = WakeWordGate()
    awake_utterances: set[int] = set()

    def handle_transcript(text: str, is_final: bool, speech_final: bool, utterance_id: int) -> None:
        # Already on the Qt main thread here (queued-connection delivery of
        # transcript_received), so touching the overlay directly is safe.
        if listening_mode == "wakeword":
            stripped = wake_gate.process(text, utterance_id, speech_final)
            if stripped is None:
                return  # not woken up yet - don't show or act on overheard speech
            if utterance_id not in awake_utterances:
                awake_utterances.add(utterance_id)
                overlay.flash_active()
            overlay.update_transcript(stripped)
            intent_engine.on_transcript(stripped, is_final, speech_final, utterance_id)
            if speech_final:
                awake_utterances.discard(utterance_id)
                overlay.set_listening(False)  # back to idle/hint until the next wake word
        else:
            overlay.update_transcript(text)
            intent_engine.on_transcript(text, is_final, speech_final, utterance_id)

    bridge.transcript_received.connect(handle_transcript)

    voice_thread_box: list[VoiceThread | None] = [None]
    ptt_controller: PushToTalkController | None = None

    if listening_mode == "wakeword":
        overlay.set_idle_hint('Say "Hey Animus"')
        voice_thread_box[0] = VoiceThread(bridge)
        voice_thread_box[0].start()
    else:
        overlay.set_idle_hint("Hold Right Alt to talk  •  Ctrl+Alt+A to toggle")

        def start_listening() -> None:
            # Runs on pynput's listener thread - only touch Qt via signals.
            if voice_thread_box[0] is None:
                vt = VoiceThread(bridge)
                voice_thread_box[0] = vt
                vt.start()
            bridge.ptt_engaged.emit()

        def stop_listening() -> None:
            vt = voice_thread_box[0]
            voice_thread_box[0] = None
            if vt is not None:
                vt.stop()
            bridge.ptt_released.emit()

        bridge.ptt_engaged.connect(lambda: overlay.flash_active())
        bridge.ptt_released.connect(lambda: overlay.set_listening(False))

        ptt_controller = PushToTalkController(
            on_start=start_listening,
            on_stop=stop_listening,
            hold_key=os.environ.get("PTT_HOLD_KEY", "alt_r"),
            toggle_letter=os.environ.get("PTT_TOGGLE_LETTER", "a"),
        )
        ptt_controller.start()

    def on_about_to_quit() -> None:
        if ptt_controller is not None:
            ptt_controller.stop()
        vt = voice_thread_box[0]
        if vt is not None:
            vt.stop()
        intent_engine.shutdown(wait=True)  # must finish before QApplication tears down

    app.aboutToQuit.connect(on_about_to_quit)

    def on_quit() -> None:
        app.quit()

    build_tray_icon(app, on_quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
