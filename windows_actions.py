"""
windows_actions.py

Native Windows automation primitives used by the voice assistant. Every
function is synchronous and safe to call from a worker thread (never call
these directly from the Qt UI thread — dispatch through a ThreadPoolExecutor,
see main.py::ActionExecutor).

DRY_RUN=1 (env var) makes every action log what it *would* do instead of
touching the OS. simulate_demo.py sets this automatically unless --live is
passed.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

# ---------------------------------------------------------------------------
# App resolution
# ---------------------------------------------------------------------------

# Known install locations checked in order; first hit wins. Kept short and
# explicit rather than a fuzzy PATH search, since these are the apps named in
# the product brief.
_ARC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Arc" / "Arc.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Arc" / "Arc.exe",
]

_CHROME_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]

# Every key resolves to one of the handler branches in open_app() below.
# Multi-word phrases ("notes app", "file explorer") are matched here for
# direct resolve_app() lookups (e.g. from the LLM fallback); the regex path
# in intent_engine.py already matches them via its own word-boundary
# patterns without needing the exact multi-word key.
_APP_ALIASES = {
    "notes": "notepad",
    "note": "notepad",
    "notes app": "notepad",
    "notepad": "notepad",
    "terminal": "terminal",
    "cmd": "terminal",
    "console": "terminal",
    "arc": "arc",
    "browser": "browser",
    "chrome": "chrome",
    "google chrome": "chrome",
    "x": "x",
    "twitter": "x",
    "camera": "camera",
    "photo booth": "camera",
    "photobooth": "camera",
    "calculator": "calc",
    "calc": "calc",
    "code": "code",
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "file explorer": "explorer",
    "explorer": "explorer",
    "files": "explorer",
}


class ActionError(RuntimeError):
    """Raised when a requested automation cannot be completed."""


def _run(cmd: list[str] | str, **kwargs) -> None:
    if DRY_RUN:
        print(f"[DRY_RUN] would run: {cmd}")
        return
    if isinstance(cmd, str):
        os.startfile(cmd)  # noqa: S606 - intentional, user-initiated
    else:
        subprocess.Popen(cmd, **kwargs)  # noqa: S603 - trusted local commands


def _open_browser(url: str) -> None:
    if DRY_RUN:
        print(f"[DRY_RUN] would open in default browser: {url}")
        return
    webbrowser.open_new_tab(url)


def resolve_app(name: str) -> str:
    # Multi-word aliases ("visual studio code", "file explorer") are matched
    # by regex with \s+ between words, so whitespace can vary - collapse it
    # before the exact-string dict lookup.
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key not in _APP_ALIASES:
        raise ActionError(f"Unknown app alias: {name!r}")
    return _APP_ALIASES[key]


def open_app(app_name: str) -> str:
    """Open (or focus) a friendly-named application. Returns a short status string."""
    resolved = resolve_app(app_name)

    if resolved == "notepad":
        _run(["notepad.exe"])
        return "Opening Notes"

    if resolved == "terminal":
        wt = shutil.which("wt.exe") or shutil.which("wt")
        if wt:
            _run([wt])
        else:
            _run(["cmd.exe"])
        return "Opening Terminal"

    if resolved == "arc":
        for candidate in _ARC_CANDIDATES:
            if candidate.exists():
                _run([str(candidate)])
                return "Opening Arc"
        # Arc not installed on this machine — fall back to default browser.
        _open_browser("https://arc.net")
        return "Arc not found, opening arc.net instead"

    if resolved == "chrome":
        for candidate in _CHROME_CANDIDATES:
            if candidate.exists():
                _run([str(candidate)])
                return "Opening Chrome"
        exe = shutil.which("chrome")
        if exe:
            _run([exe])
            return "Opening Chrome"
        _open_browser("about:blank")
        return "Chrome not found, opening default browser instead"

    if resolved == "browser":
        _open_browser("about:blank")
        return "Opening browser"

    if resolved == "x":
        navigate_url("https://x.com")
        return "Opening X"

    if resolved == "camera":
        _run("microsoft.windows.camera:")
        return "Opening Camera"

    if resolved == "calc":
        _run(["calc.exe"])
        return "Opening Calculator"

    if resolved == "code":
        exe = shutil.which("code.cmd") or shutil.which("code")
        if exe:
            _run([exe])
            return "Opening VS Code"
        raise ActionError("VS Code ('code') not found on PATH")

    if resolved == "explorer":
        _run(["explorer.exe"])
        return "Opening File Explorer"

    raise ActionError(f"No handler registered for resolved app: {resolved}")


# ---------------------------------------------------------------------------
# Text injection
# ---------------------------------------------------------------------------

# Modifier virtual-key codes: generic + left/right variants for Alt, Ctrl, Shift.
_MODIFIER_VKS = (0x12, 0xA4, 0xA5, 0x11, 0xA2, 0xA3, 0x10, 0xA0, 0xA1)
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002


def _release_modifier_keys() -> None:
    """Force-clears the OS's key-state for Alt/Ctrl/Shift.

    Confirmed via live reproduction: if the PTT hold key (Right Alt) is
    still logically down when keystrokes get injected right after, every
    injected letter is read as an Alt+<letter> accelerator instead of typed
    text - in a tabbed app like modern Notepad this drove File-menu/Open
    behavior and pulled in unrelated recently-used files, not just opened a
    menu.

    Right-side modifiers (Right Alt, Right Ctrl) are "extended" keys - a
    KEYUP without KEYEVENTF_EXTENDEDKEY set doesn't reliably clear the same
    internal state that a real (or pynput-synthesized) press of the
    extended key set, confirmed by a follow-up repro where a single
    unflagged KEYUP left the paste still going nowhere. Sending both
    variants for every vk covers whichever one is actually the active bit,
    since releasing an already-up key is a harmless no-op regardless of
    flags.
    """
    for vk in _MODIFIER_VKS:
        ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP, 0)


def _get_clipboard_text() -> Optional[str]:
    import win32clipboard

    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            return None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def _set_clipboard_text(text: str) -> None:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def write_in_app(text: str, title: Optional[str] = None) -> str:
    """
    Type `text` into the target app via clipboard-paste (Ctrl+V), not
    character-by-character keystroke injection.

    Clipboard-paste replaces the old SendKeys-style approach for two
    reasons confirmed via live reproduction against a real Notepad window:
    (1) if a modifier is still logically held when keys are injected, every
    injected letter is read as an Alt+<letter> accelerator - a single
    Ctrl+V is far less exposed to that than dozens of individual
    keystrokes; (2) it types with 100% fidelity, with no risk of a
    character in the dictated text (`%^+~{}()`) being misread as SendKeys
    syntax.

    If `title` is given, focuses the first top-level window whose title
    contains it and pastes into whatever tab is currently active there.
    Otherwise defaults to Notepad, launching it fresh if none is open, and
    always opens a brand-new tab (Ctrl+T) before pasting - an ambient,
    already-running Notepad window can have the user's own real files open
    in other tabs, and we must never type into one of those by accident.
    """
    if DRY_RUN:
        print(f"[DRY_RUN] would type into {title or 'Notepad'}: {text!r}")
        return "Typed (dry run)"

    from pywinauto import Application, findwindows  # local import: optional dep

    _release_modifier_keys()
    time.sleep(0.15)  # let the OS settle the key-up before injecting anything

    target_title = title or "Notepad"
    matches = findwindows.find_windows(title_re=f".*{target_title}.*", top_level_only=True)

    if matches:
        app = Application(backend="uia").connect(handle=matches[0])
        window = app.top_window()
    else:
        app = Application(backend="uia").start("notepad.exe")
        time.sleep(0.6)  # let the process create its main window
        window = app.top_window()

    try:
        import win32gui

        win32gui.SetForegroundWindow(window.handle)
    except Exception:
        # Best-effort: Windows' foreground-lock can refuse this from a
        # non-activating background process. set_focus() below is the
        # primary mechanism and has its own internal fallback for that.
        pass
    window.set_focus()
    time.sleep(0.05)
    # A held Alt can leave the window in menu/accelerator-navigation mode
    # even after the OS's own modifier key-state is cleared - confirmed via
    # a follow-up repro where the paste still landed nowhere despite
    # _release_modifier_keys(). Escape is the standard way to force a
    # window out of that mode regardless of which input subsystem (classic
    # Win32 vs. a modern app's own accelerator handling) is tracking it.
    window.type_keys("{ESC}", pause=0)

    if title is None:
        window.type_keys("^t", pause=0)  # new blank tab - never paste into an ambient real file
        time.sleep(0.15)

    # Re-release right before injecting the paste itself: set_focus()/
    # SetForegroundWindow can (on some Windows versions) synthesize their
    # own Alt keypress internally to defeat the foreground-lock, so the
    # earlier release alone isn't guaranteed to still hold by this point.
    _release_modifier_keys()
    window.type_keys("{ESC}", pause=0)

    previous_clipboard = _get_clipboard_text()
    _set_clipboard_text(text)
    try:
        window.type_keys("^v", pause=0)
        time.sleep(0.1)
    finally:
        if previous_clipboard is not None:
            _set_clipboard_text(previous_clipboard)

    return "Typed text"


# ---------------------------------------------------------------------------
# Browser control
# ---------------------------------------------------------------------------

_BROWSER_COMMANDS = {
    "edge": "msedge",
    "firefox": "firefox",
}

# Browsers that aren't reliably on PATH get an explicit install-path search,
# same as open_app()'s _ARC_CANDIDATES/_CHROME_CANDIDATES.
_BROWSER_CANDIDATES = {
    "arc": _ARC_CANDIDATES,
    "chrome": _CHROME_CANDIDATES,
}


def _open_url_in_browser(url: str, browser: str = "default") -> None:
    browser = browser.lower()

    if browser in ("default", ""):
        _open_browser(url)
        return

    candidates = _BROWSER_CANDIDATES.get(browser)
    if candidates is not None:
        for candidate in candidates:
            if candidate.exists():
                _run([str(candidate), url])
                return
        _open_browser(url)
        return

    exe = _BROWSER_COMMANDS.get(browser)
    if exe and shutil.which(exe):
        _run([exe, url])
        return

    # Unknown/uninstalled browser name — don't fail the whole action.
    _open_browser(url)


def browser_search(query: str, browser: str = "default") -> str:
    """Launch/focus `browser` and search for `query` using the default search engine."""
    from urllib.parse import quote_plus

    search_url = f"https://www.google.com/search?q={quote_plus(query)}"
    _open_url_in_browser(search_url, browser)
    return f"Searching for '{query}'"


def navigate_url(url: str) -> str:
    """Navigate straight to `url`, adding a scheme if the caller omitted one."""
    if "://" not in url:
        url = f"https://{url}"
    _open_url_in_browser(url)
    return f"Navigating to {url}"


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

@dataclass
class PhotoResult:
    path: Optional[Path]
    message: str


def take_photo_countdown(seconds: int = 3, on_tick=None, save_dir: Optional[Path] = None) -> PhotoResult:
    """
    Count down from `seconds`, calling on_tick(remaining) each second (the UI
    layer uses this to drive the pill's visual countdown), then capture a
    single frame from the default webcam via OpenCV and save it as a JPEG.
    """
    for remaining in range(seconds, 0, -1):
        if on_tick:
            on_tick(remaining)
        if not DRY_RUN:
            time.sleep(1)

    if DRY_RUN:
        print("[DRY_RUN] would capture photo from webcam")
        return PhotoResult(path=None, message="Captured (dry run)")

    import cv2

    capture = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    try:
        if not capture.isOpened():
            raise ActionError("No webcam available")
        ok, frame = capture.read()
        if not ok:
            raise ActionError("Failed to read a frame from the webcam")
    finally:
        capture.release()

    out_dir = save_dir or (Path.home() / "Pictures" / "VoiceAssistant")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"photo_{int(time.time())}.jpg"
    cv2.imwrite(str(out_path), frame)
    return PhotoResult(path=out_path, message=f"Saved photo to {out_path}")


def is_foreground_fullscreen() -> bool:
    """Best-effort check so the overlay can dim/hide itself over fullscreen apps."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    win_rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
    screen_w = user32.GetSystemMetrics(0)
    screen_h = user32.GetSystemMetrics(1)
    return (win_rect.right - win_rect.left) >= screen_w and (win_rect.bottom - win_rect.top) >= screen_h
