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
        main_mod, "_apply_queue_scope", lambda company: (["linkedin"], True)
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
    monkeypatch.setattr(main_mod, "_apply_queue_scope", lambda company: ([], True))
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
    monkeypatch.setattr(main_mod, "_apply_queue_scope", lambda company: ([], False))
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
