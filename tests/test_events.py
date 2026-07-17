"""Phase 2a — structured per-run event log."""
from src.events import RunLog, read_run, SCHEMA_VERSION


def test_emit_writes_jsonl_with_envelope(tmp_path):
    rl = RunLog(agent="apply", runs_dir=tmp_path)
    rec = rl.emit("attempt_started", attempt_id="a1", vendor="greenhouse", phase="started")
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["run_id"] == rl.run_id
    assert rec["event"] == "attempt_started"
    assert rec["agent"] == "apply"
    assert "ts" in rec
    events = read_run(rl.run_id, runs_dir=tmp_path)
    assert len(events) == 1 and events[0]["attempt_id"] == "a1"


def test_run_id_stable_and_appends(tmp_path):
    rl = RunLog(runs_dir=tmp_path)
    rl.emit("attempt_started", phase="started")
    rl.emit("attempt_finished", phase="receipt_verified", outcome="applied")
    events = read_run(rl.run_id, runs_dir=tmp_path)
    assert len(events) == 2
    assert {e["event"] for e in events} == {"attempt_started", "attempt_finished"}
    assert all(e["run_id"] == rl.run_id for e in events)


def test_redaction_drops_sensitive_fields(tmp_path):
    rl = RunLog(runs_dir=tmp_path)
    rec = rl.emit(
        "attempt_finished",
        vendor="lever", outcome="applied",           # safe -> kept
        resume_path="/home/me/resume.pdf",           # dropped
        applicant_email="me@example.com",            # dropped (email)
        phone="555-0100",                            # dropped
        screening_answer="yes",                      # dropped (answer)
        cookie="session=abc",                        # dropped
        raw_prompt="You are an agent...",            # dropped (prompt)
    )
    assert rec["vendor"] == "lever" and rec["outcome"] == "applied"
    for leaked in ("resume_path", "applicant_email", "phone", "screening_answer", "cookie", "raw_prompt"):
        assert leaked not in rec


def test_long_strings_truncated(tmp_path):
    rl = RunLog(runs_dir=tmp_path)
    rec = rl.emit("note", detail="x" * 1000)
    assert len(rec["detail"]) <= 301 and rec["detail"].endswith("…")


def test_emit_never_raises_on_bad_dir(tmp_path):
    # point at a path that cannot be created (a file where a dir is expected)
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    rl = RunLog(runs_dir=blocker / "runs")
    rec = rl.emit("attempt_started", phase="started")  # must not raise
    assert rec["event"] == "attempt_started"
