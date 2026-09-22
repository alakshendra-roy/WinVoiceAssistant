# WinVoiceAssistant

A voice-controlled Windows desktop assistant with a floating "pill" overlay:
a frameless, always-on-top widget pinned to the top-center of your screen
that shows live speech transcription and fires Windows automations
(open apps, search, navigate, take a photo, dictate text) the instant a
clear command is recognized — without waiting for you to finish talking.

## How it works

| File | Role |
|---|---|
| `main.py` | App entry point, wires everything together, PyQt6 event loop |
| `overlay_widget.py` | The floating pill UI and its animations |
| `voice_stream.py` | Microphone capture + streaming speech-to-text (Deepgram or local faster-whisper) |
| `listening_control.py` | Gates *when* the pipeline listens: push-to-talk hotkeys, or a wake-word text filter |
| `intent_engine.py` | Mid-sentence intent matching (regex-first, optional LLM fallback) and dedup |
| `windows_actions.py` | The actual Windows automations (open app, type text, search, navigate, photo) |
| `simulate_demo.py` | Replays a scripted transcript through the whole pipeline — no mic or API key needed |

## Requirements

- Windows 10/11
- Python 3.10+ (a native Windows install — not the WSL/MSYS one, if you have both on PATH)
- A working microphone (only needed for `main.py`, not for `simulate_demo.py`)

## Setup

```powershell
git clone https://github.com/alakshendra-roy/WinVoiceAssistant.git
cd WinVoiceAssistant

python -m venv venv
.\venv\Scripts\activate

pip install -r requirements.txt
```

`faster-whisper` in `requirements.txt` is only needed if you plan to run
without a Deepgram API key (see below) — it's a larger download (it pulls in
a CTranslate2/ONNX runtime).

Copy the env template and fill in what you have:

```powershell
copy .env.example .env
```

```dotenv
# Get one at https://console.deepgram.com — leave blank to fall back to
# local faster-whisper (offline, no API key, slightly higher latency).
DEEPGRAM_API_KEY=

# Optional: only used when the regex intent matcher can't classify a phrase.
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
```

## Running it

```powershell
python main.py
```

The mic is never captured continuously and unconditionally — you choose one
of two listening modes via `LISTENING_MODE` in `.env` (default: `ptt`):

**Push-to-talk (`LISTENING_MODE=ptt`, default)**
- Hold **Right Alt** to talk, or press **Ctrl+Alt+A** to toggle listening on/off.
- No hotkey engaged → no mic capture at all. The pill shows the hotkey hint
  while idle.
- The instant the hotkey engages, the pill pops to a vivid cyan
  "ANIMUS LISTENING" badge (no ramp-up), before showing the live transcript.

**Wake word (`LISTENING_MODE=wakeword`)**
- Say **"animus"** or **"hey animus"** to wake it up; everything before the
  wake phrase is discarded and never shown or acted on. Only the words
  after it are passed on as your command.
- The mic itself streams continuously in this mode — that's unavoidable for
  any wake-word system (something has to be listening for the trigger
  phrase), but nothing is surfaced in the UI or executed until the wake
  word is actually heard.
- The pill reverts to idle after each command finishes, waiting for the
  next "hey animus".

Once listening is active, try saying:

- "Open Arc" / "Open Notes" (or "notepad") / "Open Terminal" / "Open Camera"
- "Launch terminal" / "Pull up notepad" / "Can you please open up notepad for me"
- "Search for the latest AI news" / "Search arc for python tutorials"
- "Navigate to github.com"
- "Take a photo"
- "Write hello from the voice assistant"

Filler words ("can you", "please", "for me") and alternate trigger verbs
("open", "open up", "launch", "pull up") are all handled — see
`intent_engine.py` for the exact patterns.

When an action fires, the pill flashes an emerald confirmation badge (e.g.
`[LAUNCHED ARC]`) before reverting to idle or the live transcript.

A tray icon is added so you can quit the app (the pill window itself has no
title bar or close button by design).

## Trying it without a microphone or API key

```powershell
python simulate_demo.py          # dry run: logs actions instead of executing them
python simulate_demo.py --live   # actually opens apps / browser / camera
```

This feeds a scripted, growing transcript (mimicking real interim STT
output) through the same `IntentEngine` and `PillOverlay` used by `main.py`,
so you can verify the pill's state transitions and the action dispatch
end-to-end.

## Notes / current limitations

- App aliases (`open_app`) currently cover: notes/notepad → Notepad, terminal →
  Windows Terminal (falls back to `cmd`), arc → Arc browser (falls back to
  arc.net if not installed), x/twitter → x.com, camera/photo booth →
  Windows Camera.
- `write_in_app` and `browser_search` only fire once the utterance is
  actually finished (not mid-sentence like `open_app`/`navigate_url`/
  `take_photo`) — their argument is open-ended growing text, so firing
  early on a partial transcript ("search x" → "search x for" → ...) would
  trigger a separate, incomplete action each time.
- `take_photo_countdown` grabs a single frame from the default webcam via
  OpenCV and saves it to `Pictures\VoiceAssistant\`.
- Set `DRY_RUN=1` in the environment to log every action instead of
  executing it — useful for testing without side effects.
- Global hotkeys (PTT mode) are implemented with `pynput`; on Windows the
  physical Right Alt key is reported as `Key.alt_gr`, not `Key.alt_r` -
  `listening_control.py` already accounts for this.
