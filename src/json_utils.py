"""
Tolerant JSON extraction for LLM output.

Local models (and cloud models under prompt pressure) return JSON wrapped in
code fences, preceded by <think> blocks, or surrounded by prose. Several call
sites (scorer, profile enricher, browser-use recovery) each grew their own
partial version of this cleanup; this module is the single shared
implementation.

Design rules:
  - extract_json() never raises on malformed input — it returns None so the
    caller can degrade gracefully (flag-for-review, skip, etc.) and log.
  - clean_model_json() preserves the older raise-on-failure contract for
    callers that treat a parse failure as a hard step failure.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*", re.MULTILINE)
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


def strip_model_noise(text: str) -> str:
    """Remove <think> blocks and surrounding markdown code fences."""
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    text = text.strip()
    text = _FENCE_OPEN_RE.sub("", text, count=1)
    text = _FENCE_CLOSE_RE.sub("", text)
    return text.strip()


def _candidates(text: str, open_ch: str, close_ch: str):
    """Yield progressively smaller balanced-delimiter spans to try parsing."""
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    while start != -1 and end > start:
        yield text[start : end + 1]
        # Retry with the next closing delimiter inward — handles trailing prose
        # that itself contains a stray close_ch.
        end = text.rfind(close_ch, start, end)


_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _escape_control_chars_in_strings(text: str) -> str:
    """Escape raw newlines/tabs that appear inside JSON string literals.

    Local models asked for {"resume_markdown": "<multiline document>"} routinely
    emit the document with literal newlines inside the quoted string, which is
    invalid JSON and unrecoverable by span-trimming alone (observed live: all 3
    resume-tailor iterations discarded as "no usable draft"). Walk the text with
    a minimal string-literal state machine and escape bare control characters
    only when inside a string.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            elif ch in _CONTROL_ESCAPES:
                out.append(_CONTROL_ESCAPES[ch])
                continue
        elif ch == '"':
            in_string = True
        out.append(ch)
    return "".join(out)


def extract_json(text: str, expect: str = "any") -> Any:
    """Best-effort extraction of a JSON object/array from model output.

    expect: "object", "array", or "any".
    Returns the parsed value, or None when nothing parseable is found.
    Never raises.
    """
    if not text:
        return None
    cleaned = strip_model_noise(text)

    # Fast path: the whole (cleaned) response is valid JSON.
    try:
        value = json.loads(cleaned)
        if _matches(value, expect):
            return value
    except (ValueError, TypeError):
        pass

    delims = {"object": [("{", "}")], "array": [("[", "]")]}.get(
        expect, [("{", "}"), ("[", "]")]
    )
    for source in (cleaned, _escape_control_chars_in_strings(cleaned)):
        for open_ch, close_ch in delims:
            for candidate in _candidates(source, open_ch, close_ch):
                try:
                    value = json.loads(candidate)
                except (ValueError, TypeError):
                    continue
                if _matches(value, expect):
                    return value
    _log.warning(
        "json_utils.extract_json: no parseable JSON in model output (len=%d): %.120r",
        len(text),
        cleaned,
    )
    return None


def _matches(value: Any, expect: str) -> bool:
    if expect == "object":
        return isinstance(value, dict)
    if expect == "array":
        return isinstance(value, list)
    return isinstance(value, (dict, list))


def clean_model_json(text: str) -> dict:
    """Parse a JSON object from LLM output. Raises json.JSONDecodeError on
    malformed output (legacy contract used by the browser-use recovery loop)."""
    value = extract_json(text, expect="object")
    if value is None:
        # Raise a real JSONDecodeError so existing except-clauses keep working.
        raise json.JSONDecodeError("No JSON object found in model output", text or "", 0)
    return value
