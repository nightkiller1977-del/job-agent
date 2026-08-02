"""
Unit tests for ProgressTracker loop detection and progress scoring.
"""
import pytest
from src.progress_tracker import ProgressTracker


class TestLoopDetection:
    def _make_tracker(self, **kwargs):
        defaults = dict(max_repeated_states=3, max_repeated_actions=2,
                        state_hash_window=5, min_progress_threshold=0.3)
        defaults.update(kwargs)
        return ProgressTracker(**defaults)

    # -- insufficient data --

    def test_no_data_returns_no_loop(self):
        t = self._make_tracker()
        assert not t.detect_loop().is_looping

    def test_only_states_no_actions_returns_no_loop(self):
        t = self._make_tracker()
        t.record_state("text", "title", 3, 2, False)
        assert not t.detect_loop().is_looping

    # -- repeated DOM states --

    def test_distinct_states_do_not_trigger_state_loop(self):
        t = self._make_tracker(max_repeated_states=3)
        for i in range(4):
            t.record_state(f"unique text {i}", "title", 3, 2, False)
            t.record_action(i + 1, "fill", f"#field{i}", f"value{i}", True)
        assert not t.detect_loop().is_looping

    def test_repeated_states_trigger_loop(self):
        t = self._make_tracker(max_repeated_states=3)
        for _ in range(3):
            t.record_state("same body text", "title", 3, 2, False)
        t.record_action(1, "click", "#btn", "", True)
        result = t.detect_loop()
        assert result.is_looping
        assert "repeated" in result.reason
        assert result.confidence > 0.5

    def test_state_loop_needs_threshold_not_one_fewer(self):
        """2 identical states with max=3 should NOT trigger."""
        t = self._make_tracker(max_repeated_states=3)
        t.record_state("same", "title", 3, 2, False)
        t.record_state("same", "title", 3, 2, False)
        t.record_action(1, "fill", "#a", "v", True)
        assert not t.detect_loop().is_looping

    # -- repeated action sequences (action+selector pairs) --

    def test_different_selectors_fill_does_not_loop(self):
        """Normal form filling: same action type but different selectors — NOT a loop."""
        t = self._make_tracker(max_repeated_actions=2)
        fields = [("#name", "Alice"), ("#email", "a@b.com"), ("#phone", "555")]
        for i, (sel, val) in enumerate(fields):
            t.record_state(f"snapshot {i}", "title", 5, 3, False)
            t.record_action(i + 1, "fill", sel, val, True)
        assert not t.detect_loop().is_looping

    def test_same_selector_repeated_triggers_action_loop(self):
        t = self._make_tracker(max_repeated_actions=2)
        t.record_state("s1", "title", 3, 2, False)
        t.record_action(1, "fill", "#email", "a@b.com", False)
        t.record_state("s1", "title", 3, 2, False)
        t.record_action(2, "fill", "#email", "a@b.com", False)
        result = t.detect_loop()
        assert result.is_looping

    def test_same_selector_different_value_does_not_loop(self):
        """Correcting a validation error: same field, different value = not a loop."""
        t = self._make_tracker(max_repeated_actions=2)
        t.record_state("s1 invalid", "title", 3, 2, False)
        t.record_action(1, "fill", "#email", "bad-email", False)
        t.record_state("s2 error", "title", 3, 2, False)
        t.record_action(2, "fill", "#email", "good@example.com", True)
        assert not t.detect_loop().is_looping

    # -- selector repetition --

    def test_selector_used_multiple_times_triggers(self):
        t = self._make_tracker(max_repeated_actions=2)
        for i in range(3):
            t.record_state(f"s{i}", f"title{i}", 3, 2, False)
            t.record_action(i + 1, "click", "#same-btn", "", True)
        result = t.detect_loop()
        assert result.is_looping
        assert "#same-btn" in result.selector_repetition

    # -- progress score --

    def test_all_failed_actions_drive_progress_below_threshold(self):
        t = self._make_tracker(min_progress_threshold=0.3)
        for i in range(4):
            t.record_state("frozen page", "title", 3, 2, False)
            t.record_action(i + 1, "click", f"#btn{i}", "", False)
        result = t.detect_loop()
        assert result.is_looping
        assert result.progress_score < 0.3

    def test_successful_varied_actions_keep_good_score(self):
        t = self._make_tracker(min_progress_threshold=0.3)
        for i in range(4):
            t.record_state(f"page {i}", f"title{i}", 5, 4 - i, False)
            t.record_action(i + 1, "fill", f"#f{i}", f"v{i}", True)
        score = t._calculate_progress_score()
        assert score >= 0.3

    # -- field change metric uses absolute delta --

    def test_fewer_fields_after_section_completion_is_not_zero(self):
        """Multi-step forms often remove fields after completing a section."""
        t = self._make_tracker()
        t.record_state("section 1 text", "Step 1", 5, 5, False)
        t.record_action(1, "fill", "#a", "v", True)
        t.record_state("section 2 text", "Step 2", 3, 3, False)
        t.record_action(2, "fill", "#b", "v", True)
        score = t._calculate_progress_score()
        # field_delta=2, field_progress = 2/5 = 0.4 → contributes to score
        assert score > 0.0


class TestProgressTrackerSummary:
    def test_summary_structure(self):
        t = ProgressTracker()
        t.record_state("text", "title", 3, 2, True)
        t.record_action(1, "fill", "#n", "Alice", True)
        s = t.get_summary()
        assert s["total_steps"] == 1
        assert s["successful_steps"] == 1
        assert s["distinct_dom_states"] == 1
