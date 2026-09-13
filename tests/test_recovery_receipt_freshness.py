"""Attempt-scoped receipt freshness in BrowserUse recovery.

These tests drive the real BrowserUseRecoveryRefactored.apply loop. The only
fakes are browser/LLM boundaries. A stale confirmation already visible before
any submit must never short-circuit recovery to `applied`; after an actual
submit-like dispatch, a newly-added identical receipt occurrence may verify.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.sources.adapters.recovery_browseruse_refactored import BrowserUseRecoveryRefactored


class _AllowPolicy:
    async def confirm_submit(self, ctx, meta):
        return True

    def authorized(self, ctx):
        return True


class _Locator:
    async def inner_text(self):
        return "form body text"


class ReceiptPage:
    def __init__(self, signal="t:application submitted", count=1):
        self.url = "https://jobs.example.com/apply"
        self.signal = signal
        self.count = count
        self.probe_map = {"button[type=submit]": "submit"}

    async def title(self):
        return "Apply"

    def locator(self, selector):
        return _Locator()

    async def evaluate(self, script, arg=None):
        if "sentinel: acceptance-matcher harness" in script:
            return self.signal
        if "sentinel: acceptance-count harness" in script:
            return self.count
        if arg is not None:
            return self.probe_map.get(arg, "other")
        return ""


class _MC:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        return next(self._responses)


def _loop_cfg():
    return SimpleNamespace(
        enabled=True,
        max_steps=5,
        body_text_snippet_len=200,
        post_action_delay_ms=0,
        skill_replay_delay_ms=0,
        step_timeout_ms=10,
        success_indicators=[],
        loop_detection=SimpleNamespace(
            enabled=False, max_repeated_states=3, max_repeated_actions=3,
            state_hash_window=5,
        ),
        progress_metrics=SimpleNamespace(
            min_progress_threshold=0.1, recent_action_window=5,
            top_selectors_count=3, state_change_target_ratio=0.5,
        ),
    )


def _recovery(responses, executed):
    r = BrowserUseRecoveryRefactored.__new__(BrowserUseRecoveryRefactored)
    r.browser_config = _loop_cfg()
    r.config = SimpleNamespace(
        telemetry=SimpleNamespace(track_step_efficiency=False, track_loop_events=False),
        llm_prompting=SimpleNamespace(model_task="general", temperature=0.0),
    )
    r._load_skills = lambda domain: []
    r._save_skills = lambda domain, steps: None
    r._build_system_prompt = lambda pc: "s"
    r._build_user_prompt = lambda sd, ctx, pc: "u"
    r._clean_json_response = json.loads
    r.mc = _MC(responses)

    async def _elements(page):
        return []

    async def _inputs(page):
        return ""

    r._get_interactive_elements = _elements
    r._get_input_values_snapshot = _inputs

    async def _exec(page, action, selector, val, resume):
        executed.append((action, selector))
        if action == "click" and selector == "button[type=submit]":
            # Same receipt wording already existed, but this attempt adds one
            # fresh occurrence. Freshness must be occurrence-aware, not merely
            # string-inequality-aware.
            page.count += 1
        return True

    r._execute_action = _exec
    return r


def _ctx(page):
    return SimpleNamespace(
        page=page, url=page.url, resume_path=None, policy=_AllowPolicy(),
    )


@pytest.mark.asyncio
async def test_stale_receipt_before_any_submit_does_not_short_circuit_recovery():
    executed = []
    r = _recovery([
        '{"action":"fail","selector":null,"value":null,"explanation":"no submit yet"}'
    ], executed)
    page = ReceiptPage(signal="t:application submitted", count=1)

    result = await r.apply(_ctx(page))

    assert result.verified is False
    assert result.status == "submit_not_found"
    assert r.mc.calls == 1, "stale receipt must not bypass the LLM before any submit"
    assert executed == []


@pytest.mark.asyncio
async def test_fresh_identical_receipt_after_submit_dispatch_verifies_recovery():
    executed = []
    r = _recovery([
        '{"action":"click","selector":"button[type=submit]","value":null}'
    ], executed)
    page = ReceiptPage(signal="t:application submitted", count=1)

    result = await r.apply(_ctx(page))

    assert executed == [("click", "button[type=submit]")]
    assert result.verified is True
    assert result.status == "applied"
    assert r.mc.calls == 1, "fresh receipt should verify before another LLM decision"
