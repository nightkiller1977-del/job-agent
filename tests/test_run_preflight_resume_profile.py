"""Regression tests for the resume/profile run preflight (issue #24).

The committed default `config.json` points `local_resume_path` at
`~/resume.pdf`. When that file is absent, `resolve_resume_path()` silently
auto-discovers a substitute — in this repo's own checkout that resolves to
`tests/dummy_resume.pdf`, the test fixture. The preflight must fail fast and
name the misconfigured path instead of letting a run upload the fixture.
"""
import io
import json

import pytest
from pypdf import PdfReader

import src.main as main_mod


@pytest.fixture(autouse=True)
def _clear_resume_env(monkeypatch):
    """Keep these tests hermetic: a developer or CI runner with either env var
    set would otherwise send the validators down a different branch. Tests that
    exercise env precedence set them explicitly on top of this."""
    monkeypatch.delenv("LOCAL_RESUME_PATH", raising=False)
    monkeypatch.delenv("RESUME_PATH", raising=False)


def _write_text_pdf(path, text="Hello Resume") -> None:
    """Write a minimal, genuinely readable PDF with a text layer.

    Built by hand rather than via a mock so pypdf exercises the real parse path.
    """
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>",
    ]
    stream = b"BT /F1 12 Tf 20 150 Td (" + text.encode() + b") Tj ET"
    objs.append(b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream")
    objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_pos = out.tell()
    count = len(objs) + 1
    out.write(f"xref\n0 {count}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<</Size {count}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    path.write_bytes(out.getvalue())
    assert PdfReader(str(path)).pages[0].extract_text().strip() == text


def _write_profile(path, *, personal_info=None, raw=None) -> None:
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return
    data = {"personal_info": personal_info if personal_info is not None else {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"}}
    path.write_text(json.dumps(data), encoding="utf-8")


# ── resume validation ────────────────────────────────────────────────────────

def test_missing_configured_resume_fails_and_names_path(tmp_path):
    """The core hazard: a configured-but-absent resume must fail, not silently
    resolve to an auto-discovered file."""
    config = {"local_resume_path": str(tmp_path / "does_not_exist.pdf")}

    problems = main_mod._validate_resume_for_run(config)

    assert problems, "a missing configured resume must be reported"
    assert "does_not_exist.pdf" in problems[0]
    assert "does not exist" in problems[0]


def test_missing_configured_resume_never_falls_back_to_dummy(tmp_path, monkeypatch):
    """Even with the repo's dummy fixture discoverable, a bad configured path fails."""
    fixture = tmp_path / "tests" / "dummy_resume.pdf"
    fixture.parent.mkdir()
    fixture.write_bytes(b"%PDF-1.4\n")
    monkeypatch.chdir(tmp_path)

    config = {"local_resume_path": str(tmp_path / "nope.pdf")}
    problems = main_mod._validate_resume_for_run(config)

    assert problems
    assert "dummy_resume.pdf" not in problems[0]


def test_dummy_resume_is_rejected(tmp_path):
    fixture = tmp_path / "dummy_resume.pdf"
    _write_text_pdf(fixture)

    problems = main_mod._validate_resume_for_run({"local_resume_path": str(fixture)})

    assert problems
    assert any("test fixture" in p for p in problems)


def test_unreadable_pdf_is_rejected(tmp_path):
    bad = tmp_path / "resume.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot a real pdf")

    problems = main_mod._validate_resume_for_run({"local_resume_path": str(bad)})

    assert any("readable text layer" in p or "could not be read" in p for p in problems)


def test_unsupported_extension_is_rejected(tmp_path):
    txt = tmp_path / "resume.txt"
    txt.write_text("plain text")

    problems = main_mod._validate_resume_for_run({"local_resume_path": str(txt)})

    assert any("unsupported extension" in p for p in problems)


def test_valid_pdf_passes(tmp_path):
    good = tmp_path / "resume.pdf"
    _write_text_pdf(good)

    assert main_mod._validate_resume_for_run({"local_resume_path": str(good)}) == []


def test_no_resume_configured_and_nothing_found_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # resolve_resume_path() scans the project tree and would find this repo's
    # tests/dummy_resume.pdf, so stub it empty to exercise the genuinely-empty case.
    import src.resume_helper as resume_helper

    monkeypatch.setattr(
        resume_helper, "resolve_resume_path", lambda config=None, preferred="": ""
    )

    problems = main_mod._validate_resume_for_run({})

    assert problems
    assert "no resume" in problems[0]


def test_doc_extension_is_rejected_by_preflight(tmp_path):
    """Issue #24 defines the supported formats as .pdf/.docx. RESUME_EXTENSIONS
    also carries .doc for discovery, but preflight must reject it as promised."""
    doc = tmp_path / "resume.doc"
    doc.write_bytes(b"\xd0\xcf\x11\xe0legacy word")

    problems = main_mod._validate_resume_for_run({"local_resume_path": str(doc)})

    assert problems
    assert any("unsupported extension" in p for p in problems)


def test_tailoring_only_setup_passes_without_a_static_resume(tmp_path):
    """With tailoring enabled and a readable baseline, apply uploads a per-job
    tailored PDF. The default missing ~/resume.pdf must not block that setup."""
    baseline = tmp_path / "baseline.md"
    baseline.write_text("# Ada\n\nExperience...", encoding="utf-8")
    config = {
        "local_resume_path": str(tmp_path / "missing.pdf"),
        "resume": {"enabled": True, "baseline_path": str(baseline)},
    }

    assert main_mod._validate_resume_for_run(config) == []


def test_broken_tailoring_baseline_is_reported(tmp_path):
    """A baseline that doesn't exist would silently disable tailoring and fall
    back to the static resume, so it must fail fast."""
    config = {
        "local_resume_path": str(tmp_path / "missing.pdf"),
        "resume": {"enabled": True, "baseline_path": str(tmp_path / "nope_baseline.md")},
    }

    problems = main_mod._validate_resume_for_run(config)

    assert problems
    assert any("baseline_path does not exist" in p for p in problems)


def test_static_resume_is_still_required_when_tailoring_disabled(tmp_path):
    """With resume.enabled=false the static path is the active source, so a
    missing file must still be reported even if a baseline is configured."""
    config = {
        "local_resume_path": str(tmp_path / "missing.pdf"),
        "resume": {"enabled": False, "baseline_path": str(tmp_path / "baseline.md")},
    }

    problems = main_mod._validate_resume_for_run(config)

    assert problems
    assert any("does not exist" in p for p in problems)


def test_readable_pdf_baseline_is_accepted(tmp_path):
    baseline = tmp_path / "baseline.pdf"
    _write_text_pdf(baseline, text="Ada Lovelace Experience")

    config = {
        "local_resume_path": str(tmp_path / "missing.pdf"),
        "resume": {"enabled": True, "baseline_path": str(baseline)},
    }

    assert main_mod._validate_resume_for_run(config) == []


def test_textless_pdf_baseline_is_rejected(tmp_path):
    """A PDF baseline with no text layer would silently disable tailoring."""
    baseline = tmp_path / "baseline.pdf"
    baseline.write_bytes(b"%PDF-1.4\nnot a real pdf")

    config = {
        "local_resume_path": str(tmp_path / "missing.pdf"),
        "resume": {"enabled": True, "baseline_path": str(baseline)},
    }

    problems = main_mod._validate_resume_for_run(config)

    assert problems
    assert any("baseline_path PDF" in p for p in problems)


# ── profile validation ───────────────────────────────────────────────────────

def test_missing_profile_fails(tmp_path):
    problems = main_mod._validate_profile_for_run(tmp_path / "profile.json")

    assert problems
    assert "not found" in problems[0]


def test_profile_missing_required_fields_fails(tmp_path):
    profile = tmp_path / "profile.json"
    _write_profile(profile, personal_info={"first_name": "Ada"})

    problems = main_mod._validate_profile_for_run(profile)

    assert problems
    assert any("name" in p for p in problems)
    assert any("email" in p for p in problems)


def test_profile_full_name_without_first_last_passes(tmp_path):
    """generic ATS adapters read personal_info.full_name, so that alone is usable."""
    profile = tmp_path / "profile.json"
    _write_profile(profile, personal_info={"full_name": "Ada Lovelace", "email": "ada@example.com"})

    assert main_mod._validate_profile_for_run(profile) == []


def test_profile_missing_personal_info_fails(tmp_path):
    profile = tmp_path / "profile.json"
    _write_profile(profile, raw=json.dumps({"skills": ["python"]}))

    problems = main_mod._validate_profile_for_run(profile)

    assert problems
    assert "personal_info" in problems[0]


def test_profile_invalid_json_fails(tmp_path):
    profile = tmp_path / "profile.json"
    _write_profile(profile, raw="{not json")

    problems = main_mod._validate_profile_for_run(profile)

    assert problems
    assert "not valid JSON" in problems[0]


def test_complete_profile_passes(tmp_path):
    profile = tmp_path / "profile.json"
    _write_profile(profile)

    assert main_mod._validate_profile_for_run(profile) == []


# ── combined preflight ───────────────────────────────────────────────────────

def test_combined_preflight_passes_with_good_inputs(tmp_path):
    resume = tmp_path / "resume.pdf"
    _write_text_pdf(resume)
    profile = tmp_path / "profile.json"
    _write_profile(profile)

    ok = main_mod.preflight_resume_profile_check(
        {"local_resume_path": str(resume)}, profile_path=profile
    )

    assert ok is True


def test_combined_preflight_reports_both_problems(tmp_path):
    ok = main_mod.preflight_resume_profile_check(
        {"local_resume_path": str(tmp_path / "missing.pdf")},
        profile_path=tmp_path / "absent_profile.json",
    )

    assert ok is False


def test_combined_preflight_can_skip_each_check(tmp_path):
    resume = tmp_path / "resume.pdf"
    _write_text_pdf(resume)
    profile = tmp_path / "profile.json"
    _write_profile(profile)

    # Resume-only and profile-only subsets both pass independently.
    assert main_mod.preflight_resume_profile_check(
        {"local_resume_path": str(resume)}, check_profile=False
    )
    assert main_mod.preflight_resume_profile_check(
        {}, check_resume=False, profile_path=profile
    )


def test_resolve_profile_path_finds_project_root_state(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "project_root", tmp_path)
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    (tmp_path / "state").mkdir()
    profile = tmp_path / "state" / "profile.json"
    _write_profile(profile)

    assert main_mod._resolve_profile_path() == profile


# ── CLI wiring ───────────────────────────────────────────────────────────────

def test_apply_command_enforces_resume_profile_preflight(monkeypatch):
    """`apply` is the employer-facing flow, so a failing resume/profile
    preflight must abort the command before any browser is launched."""
    calls = []
    monkeypatch.setattr(main_mod, "load_env", lambda: None)
    monkeypatch.setattr(main_mod, "check_api_key", lambda: True)
    monkeypatch.setattr(main_mod, "preflight_env_check", lambda sources: True)
    monkeypatch.setattr(
        main_mod, "_apply_queue_scope", lambda **kw: (["linkedin"], True)
    )
    monkeypatch.setattr(
        main_mod, "_load_config_from_project", lambda: {"local_resume_path": "/nope.pdf"}
    )
    monkeypatch.setattr(
        main_mod,
        "preflight_resume_profile_check",
        lambda config, **kw: (calls.append(config), False)[1],
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["main.py", "apply", "--auto-submit"])

    with pytest.raises(SystemExit) as exc:
        main_mod.main()

    assert exc.value.code == 1
    assert calls and calls[0]["local_resume_path"] == "/nope.pdf"


def test_apply_preflight_runs_for_credless_queued_jobs(monkeypatch):
    """Legacy 'external' jobs need a resume but no source credentials, so the
    resume check must key off the queue being non-empty, not off cred sources."""
    calls = []
    monkeypatch.setattr(main_mod, "load_env", lambda: None)
    monkeypatch.setattr(main_mod, "check_api_key", lambda: True)
    monkeypatch.setattr(main_mod, "preflight_env_check", lambda sources: True)
    monkeypatch.setattr(main_mod, "_apply_queue_scope", lambda **kw: ([], True))
    monkeypatch.setattr(main_mod, "_load_config_from_project", lambda: {})
    monkeypatch.setattr(
        main_mod,
        "preflight_resume_profile_check",
        lambda config, **kw: (calls.append(config), False)[1],
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["main.py", "apply", "--auto-submit"])

    with pytest.raises(SystemExit) as exc:
        main_mod.main()

    assert exc.value.code == 1
    assert calls, "resume/profile preflight must run when a credless job is queued"


def test_apply_preflight_skipped_for_empty_queue(monkeypatch):
    """An empty queue means nothing to apply — and nothing to validate."""
    monkeypatch.setattr(main_mod, "load_env", lambda: None)
    monkeypatch.setattr(main_mod, "check_api_key", lambda: True)
    monkeypatch.setattr(main_mod, "preflight_env_check", lambda sources: True)
    monkeypatch.setattr(main_mod, "_apply_queue_scope", lambda **kw: ([], False))
    monkeypatch.setattr(
        main_mod,
        "preflight_resume_profile_check",
        lambda *a, **kw: pytest.fail("empty queue must not trigger the preflight"),
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["main.py", "apply"])

    async def _noop(args):
        return 0

    monkeypatch.setattr(main_mod, "main_async", _noop)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()

    assert exc.value.code == 0


def test_discover_command_skips_resume_profile_preflight(monkeypatch):
    """`discover` launches a browser but never uploads a resume, so it must not
    be blocked by resume/profile problems."""
    monkeypatch.setattr(main_mod, "load_env", lambda: None)
    monkeypatch.setattr(main_mod, "check_api_key", lambda: True)
    monkeypatch.setattr(main_mod, "preflight_env_check", lambda sources: True)
    monkeypatch.setattr(
        main_mod,
        "preflight_resume_profile_check",
        lambda *a, **kw: pytest.fail("discover must not run the resume/profile preflight"),
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["main.py", "discover", "--source", "themuse"])

    async def _noop(args):
        return 0

    monkeypatch.setattr(main_mod, "main_async", _noop)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()

    assert exc.value.code == 0


# ── apply queue scoping ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self, jobs):
        self._jobs = jobs

    def get_approved_unapplied(self):
        return list(self._jobs)


def _patch_state(monkeypatch, jobs):
    import src.state_manager as state_mod

    monkeypatch.setattr(state_mod, "StateManager", lambda *a, **kw: _FakeState(jobs))


def _job(job_id, source, company="Acme", score=90):
    return {"job_id": job_id, "source": source, "company": company, "score": score}


def test_queue_scope_ignores_jobs_excluded_by_source(monkeypatch):
    """`apply --source linkedin` with only Jobright jobs queued selects nothing,
    so it must not require LinkedIn's credentials."""
    _patch_state(monkeypatch, [_job("j1", "jobright")])

    sources, has_jobs = main_mod._apply_queue_scope(source="linkedin")

    assert sources == []
    assert has_jobs is False


def test_queue_scope_ignores_unmatched_job_id(monkeypatch):
    """`apply --job-id missing` with an unrelated job queued attempts nothing."""
    _patch_state(monkeypatch, [_job("j1", "linkedin")])

    sources, has_jobs = main_mod._apply_queue_scope(job_id="does-not-exist")

    assert sources == []
    assert has_jobs is False


def test_queue_scope_selects_matching_job(monkeypatch):
    _patch_state(monkeypatch, [_job("j1", "linkedin"), _job("j2", "jobright")])

    sources, has_jobs = main_mod._apply_queue_scope(source="linkedin")

    assert sources == ["linkedin"]
    assert has_jobs is True


def test_queue_scope_respects_limit(monkeypatch):
    """A --limit that selects no job must not validate, mirroring the run."""
    _patch_state(monkeypatch, [_job("j1", "linkedin")])

    sources, has_jobs = main_mod._apply_queue_scope(limit=0)

    assert sources == []
    assert has_jobs is False


def test_queue_scope_excludes_below_min_apply_score(monkeypatch):
    """apply_approved() skips low-score jobs before attempting anything, so the
    preflight must not be triggered by a job the run would skip."""
    _patch_state(monkeypatch, [_job("j1", "linkedin", score=10)])

    sources, has_jobs = main_mod._apply_queue_scope(
        config={"search_settings": {"min_apply_score": 50}}
    )

    assert sources == []
    assert has_jobs is False


def test_queue_scope_keeps_jobs_at_min_apply_score(monkeypatch):
    _patch_state(monkeypatch, [_job("j1", "linkedin", score=50)])

    sources, has_jobs = main_mod._apply_queue_scope(
        config={"search_settings": {"min_apply_score": 50}}
    )

    assert sources == ["linkedin"]
    assert has_jobs is True


# ── shared profile path (validated path == read path) ───────────────────────

def test_profile_readers_resolve_to_the_preflight_path(tmp_path, monkeypatch):
    """The preflight and the apply-stack readers must agree on which profile
    file is in use; a CWD-only reader would otherwise fill forms with nothing."""
    import src.resume_helper as resume_helper

    # Simulate a checkout whose only profile is <root>/state/profile.json and
    # whose CWD has no state/profile.json at all.
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "checkout"
    (root / "src").mkdir(parents=True)
    (root / "state").mkdir()
    profile = root / "state" / "profile.json"
    _write_profile(profile)

    monkeypatch.setattr(main_mod, "project_root", root)
    monkeypatch.setattr(resume_helper, "__file__", str(root / "src" / "resume_helper.py"))

    assert main_mod._resolve_profile_path() == profile
    # ResumeFieldFixer resolves through the same helper, so it reads that file.
    fixer = resume_helper.ResumeFieldFixer()
    assert fixer.profile.get("personal_info", {}).get("email") == "ada@example.com"
