"""
intent_engine.py

Consumes interim/final transcript events and fires Windows actions the
instant a clear imperative is recognized - it does not wait for the
utterance to end. A regex tokenizer handles the common commands with near
zero latency; an optional LLM classifier (Claude Haiku by default, Gemini
Flash as an alternative) is only consulted when the regex finds nothing,
since it costs a network round trip.

Anti-replay: each utterance has an id. Once an action's signature has fired
for a given utterance id, it will not fire again for that same utterance -
this is what stops the mid-sentence trigger and the later finalized-
transcript pass from double-executing the same command.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import windows_actions as wa

ActionCallback = Callable[[str], None]  # display label for the UI badge


@dataclass
class Action:
    name: str
    signature: str  # dedup key, e.g. "open_app:arc"
    run: Callable[[], str]  # returns a display label
    display_arg: str = ""  # human-readable arg for the confirmation badge, e.g. "arc"


# "notepad" must be its own alternative, not just "notes?" (note/notes):
# "notes?" can't match the "notepad" substring because \b after it fails
# ("notepad" has no word boundary between "note" and "pad") - this was why
# "open up notepad" previously matched nothing at all.
#
# Multi-word phrases ("visual studio code", "file explorer") must appear as
# their own alternative, not be assembled from shorter ones: alternation is
# tried at a fixed starting position (right after the trigger word), so
# "code" alone never matches when the first word there is actually "visual".
_APP_WORDS = (
    r"(notepad|notes?|arc|terminal|console|cmd|browser|chrome|x|twitter|"
    r"camera|photo\s?booth|calculator|calc|visual\s+studio\s+code|"
    r"vs\s+code|vscode|code|file\s+explorer|explorer|files)"
)

# Matches "open <app>", "open up <app>", "open the <app>", "launch <app>",
# and "pull up <app>" (with an optional "the" in between). All patterns use
# .search(), so leading/trailing filler ("can you ...", "please ...",
# "... for me") is already ignored without needing to appear in the pattern.
_OPEN_TRIGGER = r"(?:open(?:\s+(?:the|up))?|launch(?:\s+the)?|pull\s+up(?:\s+the)?)"

_KNOWN_SEARCH_TARGETS = r"(chrome|edge|arc|firefox|google|x|twitter)"

# Allows an optional comma/colon right after "write" ("write, hello world"),
# since that's exactly the shape smart-formatted STT punctuation can produce
# between an imperative verb and its content.
_WRITE_PATTERN = re.compile(r"\bwrite\b[,:]?\s*(.+?)(?:[.!?]|$)", re.IGNORECASE)

_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], Action]]] = [
    (
        re.compile(rf"\b{_OPEN_TRIGGER}\s+{_APP_WORDS}\b", re.IGNORECASE),
        lambda m: Action(
            name="open_app",
            signature=f"open_app:{m.group(1).lower()}",
            run=lambda: wa.open_app(m.group(1)),
            display_arg=m.group(1),
        ),
    ),
    (
        re.compile(r"\b(?:navigate to|go to)\s+([a-z0-9][\w.\-/:]*\.[a-z]{2,}[\w.\-/]*)", re.IGNORECASE),
        lambda m: Action(
            name="navigate_url",
            signature=f"navigate_url:{m.group(1).lower()}",
            run=lambda: wa.navigate_url(m.group(1)),
            display_arg=m.group(1),
        ),
    ),
    (
        re.compile(r"\btake (?:a )?(?:photo|picture|selfie)\b", re.IGNORECASE),
        lambda m: Action(
            name="take_photo",
            signature="take_photo",
            run=lambda: wa.take_photo_countdown().message,
        ),
    ),
]

# "write <text>" and "search ... for <query>" both capture open-ended,
# growing free text rather than a fixed short word (like an app name) - firing
# them mid-sentence (like the patterns above) means every interim update
# re-fires with a different, incomplete argument: e.g. "search x" -> "search
# x for" -> "search x for latest news" each produce a distinct dedup
# signature and were all three observed firing as separate browser searches
# during live testing. Both are only matched once the utterance is actually
# done speaking (speech_final), using the final, complete text.
_FINAL_ONLY_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], Action]]] = [
    (
        # "open <browser> and search <query>" - ties the search to the named
        # browser. Tried before the generic "search ..." pattern below,
        # which would otherwise still match the "search <query>" tail of
        # this same phrase but silently ignore that a browser was named and
        # fall back to "default" instead of actually using it.
        re.compile(
            rf"\bopen\s+{_KNOWN_SEARCH_TARGETS}\s+and\s+search\s+(?:for\s+)?(.+?)(?:[.!?]|$)",
            re.IGNORECASE,
        ),
        lambda m: Action(
            name="browser_search",
            signature=f"browser_search:{m.group(1).lower()}:{m.group(2).lower()}",
            run=lambda: wa.browser_search(
                query=m.group(2).strip(),
                browser="default" if m.group(1).lower() == "google" else m.group(1).strip(),
            ),
            display_arg=m.group(2),
        ),
    ),
    (
        # Explicit target named: "search <chrome/arc/x/...> for <query>".
        # "google" is accepted here too even though it isn't a browser app -
        # browser_search always searches via Google regardless, so it just
        # keeps "google" out of the captured query text below.
        re.compile(
            rf"\bsearch\s+(?:in\s+|on\s+)?{_KNOWN_SEARCH_TARGETS}\s+for\s+(.+?)(?:[.!?]|$)",
            re.IGNORECASE,
        ),
        lambda m: Action(
            name="browser_search",
            signature=f"browser_search:{m.group(1).lower()}:{m.group(2).lower()}",
            run=lambda: wa.browser_search(
                query=m.group(2).strip(),
                browser="default" if m.group(1).lower() == "google" else m.group(1).strip(),
            ),
            display_arg=m.group(2),
        ),
    ),
    (
        # Natural phrasing with no named target: "search for <query>" or
        # just "search <query>" - defaults to the system's default browser.
        re.compile(r"\bsearch\s+(?:for\s+)?(.+?)(?:[.!?]|$)", re.IGNORECASE),
        lambda m: Action(
            name="browser_search",
            signature=f"browser_search:default:{m.group(1).lower()}",
            run=lambda: wa.browser_search(query=m.group(1).strip(), browser="default"),
            display_arg=m.group(1),
        ),
    ),
    (
        _WRITE_PATTERN,
        lambda m: Action(
            name="write_in_app",
            signature="write_in_app",
            run=lambda: wa.write_in_app(m.group(1).strip()),
        ),
    ),
]

# Strips conversational punctuation Deepgram's smart-formatting can add
# ("Open the notes app, please!") before regex matching. Deliberately
# leaves '.', ':', '/', '-' untouched - those are load-bearing for
# navigate_url's domain-token detection (e.g. "github.com"); stripping all
# punctuation indiscriminately would break that instead of helping.
_STRIP_CHARS_PATTERN = re.compile(r"[,;!?\"'`]")


def _normalize_transcript(text: str) -> str:
    normalized = _STRIP_CHARS_PATTERN.sub("", text.lower())
    return re.sub(r"\s+", " ", normalized).strip()


# Bracketed, uppercase confirmation-badge style, e.g. "[LAUNCHED NOTEPAD]".
_ACTION_LABELS = {
    "open_app": "[LAUNCHED {arg}]",
    "navigate_url": "[NAVIGATING TO {arg}]",
    "browser_search": "[SEARCHING FOR {arg}]",
    "take_photo": "[CAPTURING PHOTO]",
    "write_in_app": "[WRITING TEXT]",
}


def _match_regex(text: str, speech_final: bool, already_fired: Callable[[str], bool]) -> Optional[Action]:
    """Returns the first NEW (not already-fired) action found.

    A match whose signature already fired earlier in this utterance is
    skipped rather than returned - otherwise a compound command like "open
    chrome and search for python tutorials" would never get past its own
    "open chrome" prefix: that substring always matches open_app first, and
    once it has already fired, returning it again just gets silently
    deduped one level up, so the trailing "search for ..." is never even
    considered on the same call.
    """
    for pattern, build in _PATTERNS:
        match = pattern.search(text)
        if match:
            action = build(match)
            if not already_fired(action.signature):
                return action
    if speech_final:
        for pattern, build in _FINAL_ONLY_PATTERNS:
            match = pattern.search(text)
            if match:
                action = build(match)
                if not already_fired(action.signature):
                    return action
    return None


class LLMFallbackClassifier:
    """Optional secondary pass for phrasing the regex layer doesn't cover.

    Only invoked when `_match_regex` returns None. Keeps the assistant
    responsive to the exact demo phrases without paying an LLM round trip on
    every single transcript update.
    """

    SYSTEM_PROMPT = (
        "You classify a spoken command fragment into at most one action. "
        "Respond with strict JSON only, one of: "
        '{"action": null} or '
        '{"action": "open_app", "app": "<name>"} or '
        '{"action": "navigate_url", "url": "<url>"} or '
        '{"action": "browser_search", "browser": "<name>", "query": "<text>"} or '
        '{"action": "take_photo"} or '
        '{"action": "write_in_app", "text": "<text>"}. '
        "Only classify a command if the intent is unambiguous and imperative. "
        "If the fragment is incomplete or unclear, respond {\"action\": null}."
    )

    def __init__(self) -> None:
        self.provider = os.environ.get("LLM_PROVIDER", "anthropic").lower()
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self.gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    def enabled(self) -> bool:
        if self.provider == "anthropic":
            return bool(self.anthropic_key)
        if self.provider == "gemini":
            return bool(self.gemini_key)
        return False

    def classify(self, text: str) -> Optional[Action]:
        if not self.enabled():
            return None
        try:
            raw = self._call_llm(text)
            data = json.loads(raw)
        except Exception as exc:  # network/parsing failures should never crash the pipeline
            print(f"[intent_engine] LLM fallback failed: {exc}")
            return None
        return self._action_from_json(data)

    def _call_llm(self, text: str) -> str:
        import requests

        if self.provider == "anthropic":
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.anthropic_model,
                    "max_tokens": 200,
                    "system": self.SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": text}],
                },
                timeout=4,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

        # Gemini
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent",
            params={"key": self.gemini_key},
            json={
                "systemInstruction": {"parts": [{"text": self.SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": text}]}],
            },
            timeout=4,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    @staticmethod
    def _action_from_json(data: dict) -> Optional[Action]:
        action = data.get("action")
        if action == "open_app":
            app = data["app"]
            return Action("open_app", f"open_app:{app.lower()}", lambda: wa.open_app(app), display_arg=app)
        if action == "navigate_url":
            url = data["url"]
            return Action(
                "navigate_url", f"navigate_url:{url.lower()}", lambda: wa.navigate_url(url), display_arg=url
            )
        if action == "browser_search":
            browser, query = data.get("browser", "default"), data["query"]
            return Action(
                "browser_search",
                f"browser_search:{browser.lower()}:{query.lower()}",
                lambda: wa.browser_search(query=query, browser=browser),
                display_arg=query,
            )
        if action == "take_photo":
            return Action("take_photo", "take_photo", lambda: wa.take_photo_countdown().message)
        if action == "write_in_app":
            text = data["text"]
            return Action(
                "write_in_app",
                f"write_in_app:{hash(text) & 0xFFFF}",
                lambda: wa.write_in_app(text),
            )
        return None


class IntentEngine:
    def __init__(self, on_action_started: ActionCallback, use_llm_fallback: bool = True) -> None:
        self._on_action_started = on_action_started
        self._fired: dict[int, set[str]] = {}
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._llm = LLMFallbackClassifier() if use_llm_fallback else None

    def _already_fired(self, utterance_id: int, signature: str) -> bool:
        return signature in self._fired.get(utterance_id, set())

    def _mark_fired(self, utterance_id: int, signature: str) -> None:
        self._fired.setdefault(utterance_id, set()).add(signature)

    def on_transcript(self, text: str, is_final: bool, speech_final: bool, utterance_id: int) -> None:
        # Lowercased/punctuation-stripped for trigger matching only - see
        # _normalize_transcript. Casing/punctuation doesn't matter for any
        # of the other captured args (Google search is case-insensitive,
        # and confirmation badges are uppercased anyway), so this is safe
        # everywhere except write_in_app's dictated content, handled below.
        normalized = _normalize_transcript(text)

        already_fired = lambda sig: self._already_fired(utterance_id, sig)  # noqa: E731
        action = _match_regex(normalized, speech_final, already_fired)
        if action is None and self._llm is not None and (is_final or len(normalized.split()) >= 4):
            action = self._llm.classify(normalized)

        if action is None:
            return
        # Same reasoning as _FINAL_ONLY_PATTERNS: open-ended captured text
        # (dictation, search queries) must not fire on a growing partial
        # transcript, regardless of which matcher (regex or LLM) found it.
        if action.name in ("write_in_app", "browser_search") and not speech_final:
            return

        if action.name == "write_in_app":
            # Re-extract from the ORIGINAL text: typing everything in
            # lowercase with punctuation stripped would be a real
            # regression for dictated notes, unlike the other actions.
            original_match = _WRITE_PATTERN.search(text)
            if original_match:
                original_content = original_match.group(1).strip()
                action = Action(
                    name="write_in_app",
                    signature="write_in_app",
                    run=lambda: wa.write_in_app(original_content),
                )
        if self._already_fired(utterance_id, action.signature):
            return

        self._mark_fired(utterance_id, action.signature)
        label = _ACTION_LABELS.get(action.name, "[WORKING]").format(arg=action.display_arg.upper())
        self._on_action_started(label)
        self._executor.submit(self._execute, action, label)

        if speech_final:
            self._fired.pop(utterance_id, None)

    def _execute(self, action: Action, fallback_label: str) -> None:
        try:
            result_label = action.run()
        except wa.ActionError as exc:
            result_label = f"Couldn't do that: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface any failure to the pill, don't crash the pipeline
            result_label = f"Error: {exc}"
        if result_label and result_label != fallback_label:
            self._on_action_started(result_label)

    def shutdown(self, wait: bool = True) -> None:
        # wait=True by default: the executor's worker threads must be fully
        # joined *before* QApplication starts tearing down, otherwise a
        # worker thread finishing an action (and emitting a Qt signal) can
        # race the interpreter's native cleanup and crash the process. Call
        # this before app.quit(), not after.
        self._executor.shutdown(wait=wait)
