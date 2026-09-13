"""Truthfulness regression for src/sources/adapters/receipt.py::_RECEIPT_JS.

Shells out to `node -e` with the PRODUCTION _RECEIPT_JS (imported, never
copied) so the browser-JS matcher is exercised byte-for-byte. Also covers
the Python-side _URL_CONFIRM_RE against unchanged-SPA-URL and posting-slug
false-positive traps.

Missing Node or malformed harness output must fail this suite — never
silently skip a safety check. The dedicated CI step `Set up Node` ensures
`node` is present on PATH; a local run without Node exits with a clear
error naming the missing dependency.

The harness only tests string-level matcher behavior. DOM-aware cases
(stale panels, form-gone, delayed acceptance across polls) live in
tests/test_receipt_dom_truthfulness.py; Node has no browser DOM.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from src.sources.adapters.receipt import _RECEIPT_JS, _URL_CONFIRM_RE


# --------------------------------------------------------------------------- #
# harness — invoke the PRODUCTION _RECEIPT_JS under node
# --------------------------------------------------------------------------- #
def _run_receipt_js(body_text: str) -> str | None:
    """Run _RECEIPT_JS against `body_text` under node and return its result.

    Returns the signal string when the matcher fires, or None when it does not.
    Raises on any harness failure (missing node, non-zero exit, invalid JSON,
    timeout) so a broken environment surfaces as a red test, not a false negative.
    """
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "node executable not found on PATH; the receipt-matcher regression "
            "tests require Node to execute _RECEIPT_JS truthfully. In CI this is "
            "provisioned by actions/setup-node@v4 in .github/workflows/ci.yml."
        )

    # Read body text from stdin as JSON so no shell quoting hazards apply, no
    # matter what characters appear in the fixture (quotes, backticks, etc.).
    # `_RECEIPT_JS` reads `document.body.innerText`; node has no `document`, so
    # the wrapper defines a minimal stand-in before invoking the matcher.
    wrapper = (
        "const fs = require('fs');\n"
        "const body = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "if (typeof body !== 'string') {\n"
        "  process.stderr.write('harness error: input was not a string\\n');\n"
        "  process.exit(2);\n"
        "}\n"
        "// Minimal stand-in for the DOM globals _RECEIPT_JS reads.\n"
        "globalThis.document = { body: { innerText: body } };\n"
        f"const receiptFn = ({_RECEIPT_JS});\n"
        "const result = receiptFn();\n"
        "// Emit JSON so `null` and empty-string are unambiguous on the wire.\n"
        "process.stdout.write(JSON.stringify(result));\n"
    )

    proc = subprocess.run(  # noqa: S603 — arguments controlled, no shell
        [node, "-e", wrapper],
        input=json.dumps(body_text),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"node harness exited {proc.returncode} for body={body_text!r}\n"
            f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        )
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"node harness stdout was not valid JSON for body={body_text!r}: {exc}\n"
            f"stdout was: {proc.stdout!r}"
        )
    if parsed is not None and not isinstance(parsed, str):
        pytest.fail(
            f"node harness returned non-string non-null result for body={body_text!r}: "
            f"{parsed!r} (type={type(parsed).__name__})"
        )
    return parsed


# --------------------------------------------------------------------------- #
# CI-config guard
# --------------------------------------------------------------------------- #
def test_node_is_available_for_receipt_regex_harness():
    """Baseline: `node` must be on PATH so the JS harness can run at all.

    If this fails locally, install Node 20+. In CI, the `Set up Node` step
    provisions it before pytest runs — a failure here means CI is misconfigured.
    """
    assert shutil.which("node") is not None


# --------------------------------------------------------------------------- #
# Valid confirmations — must be recognized
# --------------------------------------------------------------------------- #
VALID_CONFIRMATIONS = [
    ("plain_submitted", "Application submitted."),
    ("received_perfect", "Your application has been received."),
    ("successfully_submitted", "Application successfully submitted."),
    ("thanks_for_applying_specific",
     "Thanks for applying to Scan.com! We will be in touch soon."),
    ("weve_received", "We've received your application and will review it shortly."),
    ("your_app_was_submitted", "Your application was submitted."),
    ("thank_you_for_applying", "Thank you for applying to our team."),
    ("your_app_was_sent", "Your application was sent."),
    ("we_have_received_no_contraction",
     "We have received your application and will be in touch."),
    ("reference_id_fallback",
     "Thanks. Confirmation number: ABC12345. Keep this for your records."),
]


@pytest.mark.parametrize("label,body", VALID_CONFIRMATIONS, ids=lambda x: x if isinstance(x, str) else "")
def test_valid_confirmation_wording_is_recognized(label, body):
    """These strings appear on real ATS success screens. The matcher MUST fire.

    A failure here means an application acceptance would be reported as
    `submission_unverified` — a false negative that stalls real submissions.

    Asserts the SIGNAL PREFIX (`t:` for a text match, `ref:` for the reference-id
    fallback) so an accidental match via the wrong path doesn't make a text
    test look correct — the reference_id_fallback case in particular MUST
    fire through the ref path, not the text patterns, and text cases MUST NOT
    accidentally succeed via the reference-id regex on a stray "id" token.
    """
    signal = _run_receipt_js(body)
    assert signal is not None and signal != "", (
        f"[{label}] valid confirmation body was not recognized: {body!r}\n"
        f"This is a false negative — the applier would drop a real acceptance."
    )
    expected_prefix = "ref:" if label == "reference_id_fallback" else "t:"
    assert signal.startswith(expected_prefix), (
        f"[{label}] recognized via wrong path — expected prefix {expected_prefix!r} "
        f"but got signal {signal!r}. If a text case only matches via the ref-id "
        f"path (or vice versa), the corpus is not exercising what it claims."
    )


# --------------------------------------------------------------------------- #
# False-positive traps — MUST NOT be recognized
# --------------------------------------------------------------------------- #
# Each of these strings has been observed (or is plausible) as body text on
# form pages, error pages, or instructional overlays that share vocabulary
# with real success screens. Matching any of them would silently promote a
# non-acceptance to `applied`.
FALSE_POSITIVE_TRAPS = [
    ("instructional_forward_reference",
     "You will see 'Application submitted.' after completing the form."),
    ("negated_no_application",
     "No application submitted. Please complete all required fields."),
    ("explicit_failure_message",
     "Your application was not successfully submitted. Please try again."),
    ("prevention_warning",
     "Do not click submit again if your application has already been submitted."),
    ("conditional_negative",
     "If your application is not submitted, please refresh and try again."),
    ("previous_application_notice",
     "You have already submitted an application to this posting."),
    ("job_description_thanks",
     "This role helps candidates who thank you for applying feel supported."),
    ("job_description_received",
     "You will help ensure every application received is reviewed within 48 hours."),
    ("posting_body_success_word",
     "Successfully submitted candidates receive an offer letter within two weeks."),
    # Explicit negations around the verbs the matcher keys on.
    ("negated_we_have_not_received",
     "We have not received your application. Please check that you clicked submit."),
    ("negated_no_application_received",
     "No application received for this job. If you completed the form, please retry."),
    # Form-page labels and headers that share vocabulary with success screens.
    ("form_field_label_application_received",
     "Application received date: (leave blank for auto-fill)"),
    ("history_panel_previously_submitted",
     "Previously submitted applications appear in your candidate dashboard."),
    # Instructional/conditional wording that reads like success but describes the future.
    ("instructional_application_can_be_submitted",
     "Your application can be submitted after all required questions are answered."),
    # False-positive risk from bare 'thank you for applying' as a partial phrase.
    ("thank_you_for_applying_these_filters",
     "Thank you for applying these filters. Showing 12 matching roles."),
]


@pytest.mark.parametrize("label,body", FALSE_POSITIVE_TRAPS, ids=lambda x: x if isinstance(x, str) else "")
def test_false_positive_traps_are_not_recognized_as_receipts(label, body):
    """None of these bodies represent an accepted submission. The matcher
    MUST NOT return a truthy signal.

    A failure here is a SAFETY defect: it would promote a non-acceptance
    (or a warning about a NON-submission) to `applied`, inflating the
    dashboard while the actual application was never accepted.
    """
    signal = _run_receipt_js(body)
    assert signal is None, (
        f"[{label}] FALSE POSITIVE — matcher fired on non-acceptance body: {body!r}\n"
        f"Returned signal: {signal!r}. This would report a fake success."
    )


# --------------------------------------------------------------------------- #
# Unrelated content — must be ignored
# --------------------------------------------------------------------------- #
UNRELATED_BODIES = [
    ("empty", ""),
    ("plain_form_prompt", "Please fill out all required fields before submitting."),
    ("captcha_prompt", "Verify you are human by completing the challenge."),
    ("login_wall", "Sign in to your account to continue your application."),
]


@pytest.mark.parametrize("label,body", UNRELATED_BODIES, ids=lambda x: x if isinstance(x, str) else "")
def test_unrelated_body_is_not_recognized(label, body):
    signal = _run_receipt_js(body)
    assert signal is None, (
        f"[{label}] matcher fired on unrelated body: {body!r} → {signal!r}"
    )


# --------------------------------------------------------------------------- #
# Python-side URL regex — _URL_CONFIRM_RE
# --------------------------------------------------------------------------- #
URL_POSITIVES = [
    "https://acme.com/apply/thank-you",
    "https://acme.com/apply/confirmation",
    "https://boards.greenhouse.io/acme/jobs/1/thank_you",
    "https://acme.com/status?stage=thanks",
]


@pytest.mark.parametrize("url", URL_POSITIVES)
def test_url_confirm_regex_matches_thank_you_paths(url):
    assert _URL_CONFIRM_RE.search(url.lower()), (
        f"URL matcher should fire on confirmation path: {url}"
    )


URL_NEGATIVES = [
    # Unchanged Ashby SPA URL — no navigation on success
    "https://jobs.ashbyhq.com/scan-com/12345678-abcd-4000-8000-abcdef012345",
    "https://jobs.ashbyhq.com/scan-com/12345678-abcd-4000-8000-abcdef012345/application",
    # Job title slugs containing 'success'/'applied'/'submitted' words
    "https://acme.com/jobs/customer-success-manager",
    "https://acme.com/jobs/applied-scientist",
    "https://acme.com/jobs/submitted-samples-analyst",
    # Bare form pages
    "https://boards.greenhouse.io/acme/jobs/1",
    "https://acme.com/apply",
]


@pytest.mark.parametrize("url", URL_NEGATIVES)
def test_url_confirm_regex_does_not_false_fire(url):
    assert not _URL_CONFIRM_RE.search(url.lower()), (
        f"URL matcher must NOT fire on non-confirmation URL: {url}"
    )
