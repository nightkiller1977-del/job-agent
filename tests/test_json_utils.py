"""Regression tests for tolerant JSON extraction from model output.

Payloads are synthesized equivalents of failures observed in production runs:
fenced JSON, deepseek-style <think> preambles, prose-wrapped objects, and
truncated output.
"""
import json

import pytest

from src.json_utils import clean_model_json, extract_json, strip_model_noise


SCORE_OBJ = {
    "score": 92,
    "reason": "Strong fit for a senior leadership role.",
    "flags": "FEDERAL_ROLE",
    "recommended_action": "apply",
}


def test_plain_object():
    assert extract_json(json.dumps(SCORE_OBJ), expect="object") == SCORE_OBJ


def test_fenced_object():
    raw = "```json\n" + json.dumps(SCORE_OBJ) + "\n```"
    assert extract_json(raw, expect="object") == SCORE_OBJ


def test_think_block_then_object():
    # deepseek-r1 style reasoning preamble
    raw = (
        "<think>The salary meets the remote threshold and the title matches a "
        "target role, so this should score high.</think>\n" + json.dumps(SCORE_OBJ)
    )
    assert extract_json(raw, expect="object") == SCORE_OBJ


def test_prose_wrapped_object():
    raw = (
        "Sure! Here is the evaluation you asked for:\n"
        + json.dumps(SCORE_OBJ)
        + "\nLet me know if you need anything else."
    )
    assert extract_json(raw, expect="object") == SCORE_OBJ


def test_trailing_brace_in_prose():
    raw = json.dumps(SCORE_OBJ) + "\n(That covers rule {3} as well.)"
    # The stray '}' in the trailing prose must not break extraction.
    assert extract_json(raw, expect="object") == SCORE_OBJ


def test_truncated_object_returns_none():
    raw = '{"score": 92, "reason": "Strong fit for a se'
    assert extract_json(raw, expect="object") is None


def test_empty_and_non_json():
    assert extract_json("", expect="object") is None
    assert extract_json("The model declined to answer.", expect="object") is None
    assert extract_json(None, expect="object") is None


def test_array_extraction_for_skills():
    raw = "```\n[\"Team Leadership\", \"Cloud Architecture\", \"Budget Planning\"]\n```"
    assert extract_json(raw, expect="array") == [
        "Team Leadership",
        "Cloud Architecture",
        "Budget Planning",
    ]


def test_expected_type_enforced():
    assert extract_json("[1, 2, 3]", expect="object") is None
    assert extract_json('{"a": 1}', expect="array") is None


def test_clean_model_json_raises_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        clean_model_json("no json here at all")


def test_clean_model_json_parses_fenced():
    raw = '```json\n{"action": "click", "selector": "input#firstName"}\n```'
    assert clean_model_json(raw) == {"action": "click", "selector": "input#firstName"}


def test_strip_model_noise():
    assert strip_model_noise("<think>hmm</think>```json\n{}\n```") == "{}"


def test_extract_json_repairs_raw_newlines_inside_strings():
    """Prose-wrapped JSON whose string value contains literal newlines (the
    exact resume-tailor failure observed live) must still parse."""
    from src.json_utils import extract_json

    raw = (
        "Here is the rewritten resume in Markdown format:\n\n"
        '{\n  "resume_markdown": "\n# Resume\n\n## Work Experience\n- Director",\n'
        '  "notes": "kept\tfacts"\n}'
    )
    value = extract_json(raw, expect="object")
    assert value is not None
    assert value["resume_markdown"].startswith("\n# Resume")
    assert "Director" in value["resume_markdown"]
    assert value["notes"] == "kept\tfacts"


def test_extract_json_does_not_mangle_valid_escapes():
    from src.json_utils import extract_json

    raw = '{"a": "line1\\nline2", "b": "quote \\" inside"}'
    value = extract_json(raw, expect="object")
    assert value == {"a": "line1\nline2", "b": 'quote " inside'}
