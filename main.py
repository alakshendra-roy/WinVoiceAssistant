"""
main.py

App entry point. Wires together:
  - PillOverlay (Qt UI, main thread)
  - a background thread running the asyncio STT streamer (voice_stream.py)
  - IntentEngine (regex + optional LLM fallback, dispatches to windows_actions)

All cross-thread communication into the UI goes through pyqtSignal, since
Qt widgets may only be touched from the thread that owns them.
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
from overlay_widget import PillOverlay
from voice_stream import make_streamer


class VoicePipelineBridge(QObject):
    """Marshals events from the background asyncio thread onto the Qt thread."""

    transcript_received = pyqtSignal(str, bool, bool, int)
    action_started = pyqtSignal(str)
    listening_changed = pyqtSignal(bool)


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

    bridge.listening_changed.connect(overlay.set_listening)
    bridge.action_started.connect(overlay.show_action)

    def handle_transcript(text: str, is_final: bool, speech_final: bool, utterance_id: int) -> None:
        overlay.update_transcript(text)
        intent_engine.on_transcript(text, is_final, speech_final, utterance_id)

    bridge.transcript_received.connect(handle_transcript)

    voice_thread = VoiceThread(bridge)
    voice_thread.start()

    def on_about_to_quit() -> None:
        voice_thread.stop()
        intent_engine.shutdown(wait=True)  # must finish before QApplication tears down

    app.aboutToQuit.connect(on_about_to_quit)

    def on_quit() -> None:
        app.quit()

    build_tray_icon(app, on_quit)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
