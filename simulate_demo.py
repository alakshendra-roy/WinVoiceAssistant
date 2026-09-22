"""
simulate_demo.py

Replays a scripted transcript (mimicking the interim/final events a real STT
backend would emit) through the same PillOverlay + IntentEngine pipeline used
in main.py, so the UI transitions and Windows automations can be verified
end-to-end without a microphone or any API credits.

Usage:
    python simulate_demo.py            # dry run: actions are logged, not executed
    python simulate_demo.py --live     # actually opens apps / browsers / camera
"""

from __future__ import annotations

import os
import sys

if "--live" not in sys.argv:
    os.environ.setdefault("DRY_RUN", "1")

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from intent_engine import IntentEngine
from overlay_widget import PillOverlay


class _ActionBridge(QObject):
    # IntentEngine may invoke the action-started callback from its
    # ThreadPoolExecutor worker thread (e.g. when an action's result label
    # differs from its initial "Opening..." label). QWidget methods are not
    # thread-safe, so the callback must cross back to the GUI thread via a
    # signal rather than touching the overlay directly.
    action_started = pyqtSignal(str)

# Each step: (delay_ms_after_previous, transcript_so_far, is_final, speech_final, utterance_id)
# Mirrors how a real STT stream grows the transcript token-by-token within one
# utterance, then marks it final on silence.
SCRIPT: list[tuple[int, str, bool, bool, int]] = [
    (400, "hey", False, False, 0),
    (250, "hey can", False, False, 0),
    (250, "hey can you", False, False, 0),
    (250, "hey can you open", False, False, 0),
    (300, "hey can you open arc", False, False, 0),  # <- mid-sentence trigger fires here
    (400, "hey can you open arc for", False, False, 0),
    (300, "hey can you open arc for me", True, True, 0),  # finalized: must NOT refire

    (900, "then", False, False, 1),
    (250, "then search", False, False, 1),
    (250, "then search x", False, False, 1),
    (300, "then search x for", False, False, 1),
    (350, "then search x for latest ai news", False, False, 1),  # <- fires here
    (400, "then search x for latest ai news", True, True, 1),

    (900, "now", False, False, 2),
    (250, "now take", False, False, 2),
    (250, "now take a", False, False, 2),
    (300, "now take a photo", False, False, 2),  # <- fires here
    (300, "now take a photo", True, True, 2),

    (900, "write", False, False, 3),
    (300, "write hello", False, False, 3),
    (300, "write hello from", False, False, 3),
    (400, "write hello from the voice assistant demo", False, False, 3),  # <- fires here
    (300, "write hello from the voice assistant demo", True, True, 3),
]


def run() -> int:
    app = QApplication(sys.argv)
    overlay = PillOverlay()
    overlay.show()
    overlay.set_listening(True)

    fired_log: list[str] = []
    bridge = _ActionBridge()

    def on_action_started(label: str) -> None:
        fired_log.append(label)
        print(f"[simulate_demo] action: {label}")
        bridge.action_started.emit(label)  # safe from any thread

    bridge.action_started.connect(overlay.show_action)
    engine = IntentEngine(on_action_started=on_action_started)
    app.aboutToQuit.connect(lambda: engine.shutdown(wait=True))

    def step(index: int) -> None:
        if index >= len(SCRIPT):
            print("\n[simulate_demo] script complete. Actions fired:")
            for label in fired_log:
                print(f"  - {label}")
            QTimer.singleShot(3000, app.quit)
            return

        delay, text, is_final, speech_final, utterance_id = SCRIPT[index]

        def fire():
            overlay.update_transcript(text)
            engine.on_transcript(text, is_final, speech_final, utterance_id)
            step(index + 1)

        QTimer.singleShot(delay, fire)

    step(0)
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
