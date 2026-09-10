"""Tests for src/resume_tailor.py — scoring parse robustness, the integrity
post-check, the ≥min_score apply gate, tailored-resume caching, and the
dummy-resume refusal. No network/model access: model calls are stubbed."""
import json
from pathlib import Path

import pytest

from src.resume_tailor import (
    GateDecision,
    ResumeScore,
    ResumeTailor,
    TailorResult,
    check_facts_against_baseline,
    evaluate_resume_gate,
    is_dummy_resume,
    _markdown_to_html,
)
from src.state_manager import StateManager, parse_extra_json

BASELINE_MD = """# Jane Smith
Email: jane@example.com

## Summary
Engineering leader with 12 years of experience delivering cloud platforms.

## Experience
### Director of Engineering — Acme Corporation (2019 - 2024)
- Led 40-person org building AWS-based SaaS platform (EC2, S3, Terraform)
- Cut infrastructure cost 30% via autoscaling

### Engineering Manager — Initech (2014 - 2019)
- Managed 8 engineers shipping Python/Django services

## Education
- BS Computer Science, State University (2010 - 2014)

## Certifications
- PMP
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_config(tmp_path: Path, **resume_overrides) -> dict:
    baseline = tmp_path / "baseline.md"
    baseline.write_text(BASELINE_MD, encoding="utf-8")
    resume_cfg = {
        "enabled": True,
        "baseline_path": str(baseline),
        "min_score": 90,
        "max_iterations": 3,
        "output_dir": str(tmp_path / "resumes"),
    }
    resume_cfg.update(resume_overrides)
    return {"resume": resume_cfg}


def make_job(job_id="job-1") -> dict:
    return {
        "job_id": job_id,
        "source": "linkedin",
        "title": "Director of Engineering",
        "company": "Globex",
        "url": "https://example.com/job",
        "description": "Lead cloud platform engineering. AWS, Terraform, leadership.",
        "status": "approved",
    }


class StubModelClient:
    """Feeds canned responses to ModelClient.complete calls in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, system="", task_type="general", max_tokens=1024, temperature=None, force_provider=None):
        self.calls.append(messages[0]["content"])
        if not self.responses:
            raise AssertionError("StubModelClient ran out of responses")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    # Matches the real ModelClient shape so ResumeTailor can gate its
    # gateway-escalation branch without needing to introspect the client.
    # The stub reports "not configured" so tests exercise the local-only path
    # unless a specific test overrides this.
    def get_gateway_config(self):
        return (False, "", "")


def score_json(total, kw=30, title=30, exp=30, missing=None):
    return json.dumps(
        {
            "score": total,
            "subscores": {
                "keyword_coverage": kw,
                "title_alignment": title,
                "experience_relevance": exp,
            },
            "missing_keywords": missing or [],
            "reasoning": "stub",
        }
    )


def facts_json(employers=None, titles=None, dates=None, degrees=None, certs=None):
    return json.dumps(
        {
            "employers": employers or [],
            "titles": titles or [],
            "dates": dates or [],
            "degrees": degrees or [],
            "certifications": certs or [],
        }
    )


TAILORED_MD = BASELINE_MD.replace("delivering cloud platforms", "delivering AWS cloud platforms")


def tailor_json(md=TAILORED_MD):
    return json.dumps({"resume_markdown": md})


async def _passthrough_render(self, markdown_text, out_path):
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text("fake-pdf", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# 1. Score parsing robustness
# ---------------------------------------------------------------------------
def test_parse_score_plain_json():
    score = ResumeTailor.parse_score_response(score_json(92, kw=38, title=28, exp=26))
    assert score.total == 92
    assert score.subscores == {
        "keyword_coverage": 38,
        "title_alignment": 28,
        "experience_relevance": 26,
    }


def test_parse_score_fenced_and_prose_wrapped():
    raw = "Here is my evaluation:\n```json\n" + score_json(77, missing=["Terraform"]) + "\n```\nHope that helps!"
    score = ResumeTailor.parse_score_response(raw)
    assert score.total == 77
    assert score.missing_keywords == ["Terraform"]


def test_parse_score_think_block():
    raw = "<think>hmm let me think</think>" + score_json(55)
    assert ResumeTailor.parse_score_response(raw).total == 55


def test_parse_score_clamps_out_of_range():
    raw = json.dumps({"score": 250, "subscores": {"keyword_coverage": 99, "title_alignment": -5, "experience_relevance": 10}})
    score = ResumeTailor.parse_score_response(raw)
    assert score.total == 100
    assert score.subscores["keyword_coverage"] == 40
    assert score.subscores["title_alignment"] == 0


def test_parse_score_missing_total_sums_subscores():
    raw = json.dumps({"subscores": {"keyword_coverage": 30, "title_alignment": 25, "experience_relevance": 20}})
    assert ResumeTailor.parse_score_response(raw).total == 75


def test_parse_score_garbage_returns_none():
    assert ResumeTailor.parse_score_response("total garbage, no json") is None
    assert ResumeTailor.parse_score_response("") is None
    assert ResumeTailor.parse_score_response("No model available for scoring") is None


# ---------------------------------------------------------------------------
# 2. Integrity post-check
# ---------------------------------------------------------------------------
def test_integrity_catches_invented_employer_and_cert():
    facts = {
        "employers": ["Acme Corporation", "Google"],  # Google not in baseline
        "titles": ["Director of Engineering"],
        "dates": ["2019 - 2024"],
        "degrees": ["BS Computer Science"],
        "certifications": ["PMP", "CISSP"],  # CISSP not in baseline
    }
    violations = check_facts_against_baseline(facts, BASELINE_MD)
    assert any("Google" in v for v in violations)
    assert any("CISSP" in v for v in violations)
    assert len(violations) == 2


def test_integrity_passes_grounded_facts_with_punctuation_variance():
    facts = {
        "employers": ["Acme Corporation", "Initech"],
        "titles": ["Director of Engineering", "Engineering Manager"],
        "dates": ["2019-2024", "2014 to 2019"],
        "degrees": ["B.S. Computer Science"],  # punctuation normalized away
        "certifications": ["PMP"],
    }
    assert check_facts_against_baseline(facts, BASELINE_MD) == []


def test_integrity_catches_invented_year():
    facts = {"employers": [], "titles": [], "dates": ["2005 - 2009"], "degrees": [], "certifications": []}
    violations = check_facts_against_baseline(facts, BASELINE_MD)
    assert violations and "2005" in violations[0]


@pytest.mark.asyncio
async def test_verify_integrity_fails_closed_on_extraction_failure(tmp_path):
    tailor = ResumeTailor(make_config(tmp_path), model_client=StubModelClient(["not json at all"]))
    ok, violations = await tailor.verify_integrity(TAILORED_MD, BASELINE_MD)
    assert ok is False
    assert violations


@pytest.mark.asyncio
async def test_ensure_tailored_rejects_fabricated_draft(tmp_path, monkeypatch):
    """A draft claiming an invented employer must never become the best draft."""
    fabricated = BASELINE_MD + "\n### VP Engineering — Google (2024 - 2025)\n- Ran search infra\n"
    stub = StubModelClient(
        [
            score_json(60),  # baseline score
            tailor_json(fabricated),  # iteration 1 draft
            facts_json(employers=["Acme Corporation", "Google"]),  # facts → violation
            tailor_json(),  # iteration 2 draft (clean)
            facts_json(employers=["Acme Corporation", "Initech"]),  # grounded
            score_json(95),  # clean draft scores above threshold
        ]
    )
    tailor = ResumeTailor(make_config(tmp_path), model_client=stub)
    monkeypatch.setattr(ResumeTailor, "render_pdf", _passthrough_render)
    result = await tailor.ensure_tailored(make_job())
    assert result.status == "ready"
    assert result.score == 95
    assert "Google" not in Path(result.markdown_path).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_gateway_escalation_only_fires_when_local_exhausted_and_opted_in(tmp_path, monkeypatch):
    """When local iterations can't clear the gate and allowPaidCloud=true with a
    configured gateway, one extra attempt runs via force_provider='openrouter'.
    Without either signal, the gateway must NOT fire (spend/dependency safety).
    """
    # Scenario A: opt-in + configured gateway → escalation happens and clears the gate.
    stub = StubModelClient(
        [
            score_json(60),      # baseline scores below gate
            tailor_json(),       # local iter 1 draft (matches BASELINE_MD → passes integrity)
            facts_json(),
            score_json(70),      # local iter 1 still below gate
            tailor_json(),       # local iter 2
            facts_json(),
            score_json(72),      # local iter 2 still below gate
            tailor_json(),       # local iter 3
            facts_json(),
            score_json(75),      # local iter 3 still below gate — local exhausted
            tailor_json(),       # gateway escalation draft
            facts_json(),
            score_json(95),      # gateway escalation clears the gate
        ]
    )
    stub.get_gateway_config = lambda: (True, "https://ai-openrouter.onrender.com", "test-key")
    tailor = ResumeTailor(
        make_config(tmp_path, allowPaidCloud=True), model_client=stub
    )
    monkeypatch.setattr(ResumeTailor, "render_pdf", _passthrough_render)
    result = await tailor.ensure_tailored(make_job())
    assert result.status == "ready"
    assert result.score == 95

    # Scenario B: opt-in but gateway NOT configured → escalation skipped (no extra call).
    stub_no_gw = StubModelClient(
        [
            score_json(60),
            tailor_json(), facts_json(), score_json(75),  # iter 1
            tailor_json(), facts_json(), score_json(75),  # iter 2
            tailor_json(), facts_json(), score_json(75),  # iter 3 — exhausted
        ]
    )
    stub_no_gw.get_gateway_config = lambda: (False, "", "")
    tailor_no_gw = ResumeTailor(
        make_config(tmp_path, allowPaidCloud=True), model_client=stub_no_gw
    )
    result = await tailor_no_gw.ensure_tailored(make_job(job_id="job-2"))
    assert result.status == "below_threshold"

    # Scenario C: gateway configured but allowPaidCloud=false → escalation skipped.
    stub_no_opt = StubModelClient(
        [
            score_json(60),
            tailor_json(), facts_json(), score_json(75),
            tailor_json(), facts_json(), score_json(75),
            tailor_json(), facts_json(), score_json(75),
        ]
    )
    stub_no_opt.get_gateway_config = lambda: (True, "https://ai-openrouter.onrender.com", "test-key")
    tailor_no_opt = ResumeTailor(
        make_config(tmp_path), model_client=stub_no_opt  # default allowPaidCloud=False
    )
    result = await tailor_no_opt.ensure_tailored(make_job(job_id="job-3"))
    assert result.status == "below_threshold"


# ---------------------------------------------------------------------------
# 3. Gate behavior
# ---------------------------------------------------------------------------
@pytest.fixture
def state(tmp_path):
    sm = StateManager(db_path=str(tmp_path / "jobs.db"))
    job = make_job()
    sm.upsert_job(job)
    yield sm
    sm.close()


class StubTailor:
    def __init__(self, result, baseline="baseline text"):
        self.result = result
        self.baseline = baseline
        self.calls = 0

    def load_baseline(self):
        return self.baseline

    async def ensure_tailored(self, job):
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_gate_blocks_below_threshold_and_records_marker(tmp_path, state):
    config = make_config(tmp_path)
    tailor = StubTailor(
        TailorResult(status="below_threshold", score=72, iterations=3,
                     markdown_path=str(tmp_path / "d.md"),
                     detail="best score 72 < 90 after 3 iteration(s)")
    )
    decision = await evaluate_resume_gate(make_job(), config, state, tailor=tailor)
    assert decision.proceed is False
    assert decision.status == "needs_resume_review"

    row = state.get_job("job-1")
    extra = parse_extra_json(row["extra_json"])
    assert extra["apply_last_status"] == "needs_resume_review"
    assert extra["resume_score"] == 72
    assert "72" in extra["apply_last_detail"]
    # Job is NOT applied — status untouched
    assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_gate_passes_at_threshold_and_persists_artifacts(tmp_path, state):
    config = make_config(tmp_path)
    pdf = tmp_path / "resumes" / "job-1.pdf"
    tailor = StubTailor(
        TailorResult(status="ready", resume_path=str(pdf), score=93,
                     subscores={"keyword_coverage": 38, "title_alignment": 28, "experience_relevance": 27},
                     iterations=2)
    )
    decision = await evaluate_resume_gate(make_job(), config, state, tailor=tailor)
    assert decision.proceed is True
    assert decision.resume_path == str(pdf)

    extra = parse_extra_json(state.get_job("job-1")["extra_json"])
    assert extra["tailored_resume_path"] == str(pdf)
    assert extra["resume_score"] == 93
    assert extra["resume_tailor_iterations"] == 2


@pytest.mark.asyncio
async def test_gate_blocks_on_tailor_error(tmp_path, state):
    config = make_config(tmp_path)
    tailor = StubTailor(TailorResult(status="error", detail="model cascade exhausted"))
    decision = await evaluate_resume_gate(make_job(), config, state, tailor=tailor)
    assert decision.proceed is False
    assert decision.status == "resume_tailor_error"


@pytest.mark.asyncio
async def test_gate_no_baseline_falls_back_gracefully(tmp_path, state):
    real_resume = tmp_path / "my_resume.pdf"
    real_resume.write_text("pdf")
    config = {"resume": {"enabled": True}, "local_resume_path": str(real_resume)}
    tailor = StubTailor(None, baseline="")  # no baseline configured
    decision = await evaluate_resume_gate(make_job(), config, state, tailor=tailor)
    assert decision.proceed is True
    assert decision.resume_path == ""
    assert tailor.calls == 0  # tailoring never attempted without a baseline


# ---------------------------------------------------------------------------
# 4. Cache behavior
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_hit_skips_retailoring(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    stub = StubModelClient([score_json(94)])  # single scoring call, no tailoring needed
    tailor = ResumeTailor(config, model_client=stub)
    monkeypatch.setattr(ResumeTailor, "render_pdf", _passthrough_render)

    first = await tailor.ensure_tailored(make_job())
    assert first.status == "ready" and first.from_cache is False

    # Second run: no model responses left — a cache miss would raise.
    tailor2 = ResumeTailor(config, model_client=StubModelClient([]))
    second = await tailor2.ensure_tailored(make_job())
    assert second.status == "ready"
    assert second.from_cache is True
    assert second.score == 94
    assert second.resume_path == first.resume_path


@pytest.mark.asyncio
async def test_cache_invalidated_when_baseline_changes(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    tailor = ResumeTailor(config, model_client=StubModelClient([score_json(94)]))
    monkeypatch.setattr(ResumeTailor, "render_pdf", _passthrough_render)
    first = await tailor.ensure_tailored(make_job())
    assert first.status == "ready"

    # Change the baseline → hash changes → cached artifact must be ignored.
    Path(config["resume"]["baseline_path"]).write_text(
        BASELINE_MD + "\n- New: Kubernetes migration\n", encoding="utf-8"
    )
    stub2 = StubModelClient([score_json(96)])
    tailor2 = ResumeTailor(config, model_client=stub2)
    second = await tailor2.ensure_tailored(make_job())
    assert second.from_cache is False
    assert second.score == 96
    assert stub2.calls  # model actually consulted again


@pytest.mark.asyncio
async def test_below_threshold_artifact_is_not_cache_reused(tmp_path, monkeypatch):
    config = make_config(tmp_path, max_iterations=0)
    tailor = ResumeTailor(config, model_client=StubModelClient([score_json(70)]))
    monkeypatch.setattr(ResumeTailor, "render_pdf", _passthrough_render)
    first = await tailor.ensure_tailored(make_job())
    assert first.status == "below_threshold"

    # Next run re-scores (cache only serves artifacts that met the threshold).
    stub2 = StubModelClient([score_json(71)])
    tailor2 = ResumeTailor(config, model_client=stub2)
    second = await tailor2.ensure_tailored(make_job())
    assert second.status == "below_threshold"
    assert stub2.calls


# ---------------------------------------------------------------------------
# 5. Dummy-resume refusal
# ---------------------------------------------------------------------------
def test_is_dummy_resume():
    assert is_dummy_resume("tests/dummy_resume.pdf")
    assert is_dummy_resume("/anywhere/else/DUMMY_RESUME.PDF")
    assert not is_dummy_resume("/home/user/resume.pdf")
    assert not is_dummy_resume("")


@pytest.mark.asyncio
async def test_gate_refuses_dummy_resume_fixture(tmp_path, state):
    dummy = tmp_path / "dummy_resume.pdf"
    dummy.write_text("fixture")
    config = {"resume": {"enabled": True}, "local_resume_path": str(dummy)}
    tailor = StubTailor(None, baseline="")  # no baseline → fallback path
    decision = await evaluate_resume_gate(make_job(), config, state, tailor=tailor)
    assert decision.proceed is False
    assert decision.status == "dummy_resume_blocked"

    extra = parse_extra_json(state.get_job("job-1")["extra_json"])
    assert extra["apply_last_status"] == "dummy_resume_blocked"


@pytest.mark.asyncio
async def test_gate_refuses_dummy_even_when_tailoring_disabled(tmp_path, state):
    dummy = tmp_path / "dummy_resume.pdf"
    dummy.write_text("fixture")
    config = {"resume": {"enabled": False}, "local_resume_path": str(dummy)}
    decision = await evaluate_resume_gate(make_job(), config, state)
    assert decision.proceed is False
    assert decision.status == "dummy_resume_blocked"


# ---------------------------------------------------------------------------
# Misc: markdown renderer and blocker classification
# ---------------------------------------------------------------------------
def test_markdown_to_html_basics():
    html = _markdown_to_html("# Jane\n\n## Skills\n- **AWS** & GCP\n- Python\n\nSummary <line>")
    assert "<h1>Jane</h1>" in html
    assert "<h2>Skills</h2>" in html
    assert "<li><strong>AWS</strong> &amp; GCP</li>" in html
    assert "&lt;line&gt;" in html  # HTML injection escaped


def test_new_statuses_classified():
    from src.blocker_classifier import classify, BlockerClass

    assert classify("needs_resume_review") is BlockerClass.NEEDS_HUMAN
    assert classify("dummy_resume_blocked") is BlockerClass.PERMANENT
    assert classify("resume_tailor_error") is BlockerClass.TRANSIENT


@pytest.mark.asyncio
async def test_pdf_baseline_extracted_once(tmp_path, monkeypatch):
    """A .pdf baseline is text-extracted into an editable markdown file."""
    pdf_path = tmp_path / "resume.pdf"
    pdf_path.write_bytes(b"%PDF-fake")
    config = make_config(tmp_path, baseline_path=str(pdf_path))

    class FakePage:
        def extract_text(self):
            return BASELINE_MD

    class FakeReader:
        def __init__(self, _):
            self.pages = [FakePage()]

    import src.resume_tailor as rt
    monkeypatch.setattr("pypdf.PdfReader", FakeReader)
    tailor = rt.ResumeTailor(config, model_client=StubModelClient([]))
    text = tailor.load_baseline()
    assert "Acme Corporation" in text
    extracted = Path(config["resume"]["output_dir"]) / "baseline_extracted.md"
    assert extracted.is_file()

    # Second load must reuse the extracted file (no PdfReader needed).
    monkeypatch.setattr("pypdf.PdfReader", None)
    tailor2 = rt.ResumeTailor(config, model_client=StubModelClient([]))
    assert "Acme Corporation" in tailor2.load_baseline()


def test_gate_decision_defaults():
    d = GateDecision(True)
    assert d.proceed and d.resume_path == "" and d.status == ""


def test_resume_score_defaults():
    s = ResumeScore(total=50)
    assert s.subscores == {} and s.missing_keywords == []
