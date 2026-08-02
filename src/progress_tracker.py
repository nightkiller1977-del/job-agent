"""
progress_tracker.py — Tracks LLM agent progress and detects response loops.

Monitors form-filling state changes to detect when the agent is:
- Making genuine progress (DOM changes, new fields filled, navigation)
- Repeating the same state (loop indicator)
- Stuck without any visible progress

The tracker maintains:
1. DOM state hashes: detect when the page state actually changes
2. Action history: identify repeated sequences
3. Selector usage frequency: spot repeated attempts on the same field
4. Progress score: measure progress toward submission
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

_log = logging.getLogger(__name__)


@dataclass
class DomState:
    """Snapshot of page DOM state for comparison."""
    body_text_hash: str
    title: str
    interactive_element_count: int
    form_fields_count: int
    has_submit_button: bool
    timestamp: datetime = field(default_factory=datetime.now)

    @staticmethod
    def compute_hash(text: str) -> str:
        """Compute SHA-256 hash of text for comparison."""
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class ActionRecord:
    """Record of a single LLM action."""
    step: int
    action: str
    selector: str
    value: str
    success: bool
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class LoopDetectionResult:
    """Result of loop detection analysis."""
    is_looping: bool
    reason: str
    confidence: float  # 0.0 to 1.0
    repeated_states_count: int
    repeated_actions_count: int
    selector_repetition: dict[str, int] = field(default_factory=dict)
    progress_score: float = 0.5


class ProgressTracker:
    """Track progress through form-filling and detect loops."""

    def __init__(
        self,
        max_repeated_states: int = 3,
        max_repeated_actions: int = 2,
        state_hash_window: int = 5,
        min_progress_threshold: float = 0.3,
        recent_action_window: int = 5,
        top_selectors_count: int = 3,
        state_change_target_ratio: float = 0.7,
    ):
        self.max_repeated_states = max_repeated_states
        self.max_repeated_actions = max_repeated_actions
        self.state_hash_window = state_hash_window
        self.min_progress_threshold = min_progress_threshold
        self.recent_action_window = recent_action_window
        self.top_selectors_count = top_selectors_count
        self.state_change_target_ratio = state_change_target_ratio

        self.dom_states: list[DomState] = []
        self.action_history: list[ActionRecord] = []
        self.selector_usage: dict[str, int] = {}
        self.last_progress_score: float = 0.5

    def record_state(
        self,
        body_text: str,
        title: str,
        interactive_count: int,
        form_fields_count: int,
        has_submit: bool,
    ) -> None:
        """Record current page state."""
        state = DomState(
            body_text_hash=DomState.compute_hash(body_text),
            title=title,
            interactive_element_count=interactive_count,
            form_fields_count=form_fields_count,
            has_submit_button=has_submit,
        )
        self.dom_states.append(state)
        _log.debug(f"State recorded: hash={state.body_text_hash[:8]}... fields={form_fields_count}")

    def record_action(
        self,
        step: int,
        action: str,
        selector: str,
        value: str,
        success: bool,
    ) -> None:
        """Record an action taken by the LLM agent."""
        record = ActionRecord(
            step=step, action=action, selector=selector, value=value, success=success
        )
        self.action_history.append(record)

        # Track selector usage
        if selector:
            self.selector_usage[selector] = self.selector_usage.get(selector, 0) + 1

        _log.debug(
            f"Action recorded: step={step} action={action} selector={selector} success={success}"
        )

    def detect_loop(self) -> LoopDetectionResult:
        """
        Analyze tracked state and actions to detect if the agent is looping.
        Returns a LoopDetectionResult with confidence and reasoning.
        """
        if not self.dom_states or not self.action_history:
            return LoopDetectionResult(
                is_looping=False,
                reason="insufficient data",
                confidence=0.0,
                repeated_states_count=0,
                repeated_actions_count=0,
            )

        # Check for repeated DOM states
        repeated_states = self._count_repeated_dom_states()

        # Check for repeated action sequences (same action+selector pair)
        repeated_actions = self._count_repeated_actions()

        # Check for selector repetition (trying the same field multiple times)
        high_repetition_selectors = {
            sel: count
            for sel, count in self.selector_usage.items()
            if count >= self.max_repeated_actions
        }

        # Calculate progress score
        progress_score = self._calculate_progress_score()
        self.last_progress_score = progress_score

        # Determine if looping
        is_looping = False
        reason = "no loop detected"
        confidence = 0.0

        if repeated_states >= self.max_repeated_states:
            is_looping = True
            reason = f"page state repeated {repeated_states} times without change"
            confidence = min(0.95, 0.5 + (repeated_states * 0.15))

        elif repeated_actions >= self.max_repeated_actions:
            is_looping = True
            reason = f"action+selector pair repeated {repeated_actions} times consecutively"
            confidence = min(0.90, 0.5 + (repeated_actions * 0.2))

        elif high_repetition_selectors:
            is_looping = True
            sel = list(high_repetition_selectors.keys())[0]
            count = high_repetition_selectors[sel]
            reason = f"selector '{sel}' attempted {count} times (likely same-field loop)"
            confidence = min(0.85, 0.4 + (count * 0.15))

        elif progress_score < self.min_progress_threshold:
            is_looping = True
            reason = f"progress score {progress_score:.2f} below threshold {self.min_progress_threshold}"
            confidence = min(0.80, 0.6 + ((self.min_progress_threshold - progress_score) * 2))

        return LoopDetectionResult(
            is_looping=is_looping,
            reason=reason,
            confidence=confidence,
            repeated_states_count=repeated_states,
            repeated_actions_count=repeated_actions,
            selector_repetition=high_repetition_selectors,
            progress_score=progress_score,
        )

    def get_progress_context(self) -> dict:
        """
        Generate context about progress for the LLM to understand.
        Includes: state change summary, action effectiveness, selector patterns.
        """
        total_steps = len(self.action_history)
        successful_steps = sum(1 for a in self.action_history if a.success)
        recent_states = self.dom_states[-self.state_hash_window :] if self.dom_states else []

        # Detect state transitions
        state_changes = self._count_state_changes(recent_states)

        # Identify most-used selectors
        top_selectors = sorted(self.selector_usage.items(), key=lambda x: x[1], reverse=True)[:self.top_selectors_count]

        return {
            "total_steps": total_steps,
            "successful_steps": successful_steps,
            "success_rate": successful_steps / total_steps if total_steps > 0 else 0,
            "state_changes_recent": state_changes,
            "most_used_selectors": [{"selector": s, "attempts": c} for s, c in top_selectors],
            "progress_score": self.last_progress_score,
            "action_history_recent": [
                {
                    "step": a.step,
                    "action": a.action,
                    "success": a.success,
                }
                for a in self.action_history[-self.recent_action_window:]
            ],
        }

    def _count_repeated_dom_states(self) -> int:
        """Count how many times the latest DOM state hash appears in the recent window."""
        if len(self.dom_states) < 2:
            return 0

        recent = self.dom_states[-self.state_hash_window :]
        latest_hash = recent[-1].body_text_hash
        return sum(1 for state in recent if state.body_text_hash == latest_hash)

    def _count_repeated_actions(self) -> int:
        """
        Count the longest consecutive run of identical (action, selector) pairs.
        Using pairs instead of action type alone avoids false positives on forms
        that legitimately require multiple fill/click actions on different fields.
        """
        if len(self.action_history) < 2:
            return 0

        recent = self.action_history[-self.recent_action_window:]
        action_pairs = [(a.action, a.selector) for a in recent]
        max_consecutive = 1
        current_run = 1

        for i in range(1, len(action_pairs)):
            if action_pairs[i] == action_pairs[i - 1]:
                current_run += 1
                max_consecutive = max(max_consecutive, current_run)
            else:
                current_run = 1

        return max_consecutive

    def _calculate_progress_score(self) -> float:
        """
        Calculate overall progress score (0.0 to 1.0).
        Considers: state changes, successful actions, form field activity.
        """
        if not self.dom_states or not self.action_history:
            return 0.5

        # State change contribution (40%)
        state_changes = self._count_state_changes(self.dom_states)
        max_states = len(self.dom_states)
        state_progress = min(1.0, state_changes / max(1, max_states * self.state_change_target_ratio))

        # Action success contribution (40%)
        successful = sum(1 for a in self.action_history if a.success)
        total = len(self.action_history)
        success_rate = successful / max(1, total)

        # Form field activity (20%) — any change in field count indicates progress.
        # Multi-step forms frequently reduce visible fields after completing a section,
        # so we use absolute change rather than requiring monotonic increase.
        if len(self.dom_states) >= 2:
            first_state = self.dom_states[0]
            last_state = self.dom_states[-1]
            field_delta = abs(last_state.form_fields_count - first_state.form_fields_count)
            field_progress = min(1.0, field_delta / max(1, first_state.form_fields_count))
        else:
            field_progress = 0.5

        # Combine weighted scores
        score = (state_progress * 0.4) + (success_rate * 0.4) + (field_progress * 0.2)
        return max(0.0, min(1.0, score))

    def _count_state_changes(self, states: list[DomState]) -> int:
        """Count distinct states in the given list."""
        if not states:
            return 0

        distinct_hashes = set(s.body_text_hash for s in states)
        return len(distinct_hashes)

    def get_summary(self) -> dict:
        """Get a summary of tracking data for logging."""
        return {
            "total_steps": len(self.action_history),
            "successful_steps": sum(1 for a in self.action_history if a.success),
            "distinct_dom_states": len(set(s.body_text_hash for s in self.dom_states)),
            "unique_selectors": len(self.selector_usage),
            "high_repetition_selectors": [
                {"selector": s, "count": c}
                for s, c in self.selector_usage.items()
                if c >= self.max_repeated_actions
            ],
            "progress_score": self.last_progress_score,
        }
