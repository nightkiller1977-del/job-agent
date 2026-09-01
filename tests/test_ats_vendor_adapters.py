import pytest
from src.sources.adapters.context import AtsApplyContext, AtsApplyResult
from src.sources.adapters.greenhouse import GreenhouseAdapter
from src.sources.adapters.lever import LeverAdapter
from src.sources.adapters.ashby import AshbyAdapter
from src.sources.adapters.registry import AtsAdapterRegistry

def _ctx(url):
    return AtsApplyContext(page=None, job={"url": url}, profile=None, url=url)

@pytest.mark.asyncio
async def test_vendor_adapters_matching():
    gh = GreenhouseAdapter()
    lv = LeverAdapter()
    ash = AshbyAdapter()
    
    # Greenhouse match
    assert await gh.can_handle(_ctx("https://boards.greenhouse.io/acme/jobs/1")) == 0.9
    assert await gh.can_handle(_ctx("https://jobs.lever.co/acme/1")) == 0.0
    
    # Lever match
    assert await lv.can_handle(_ctx("https://jobs.lever.co/acme/1")) == 0.9
    assert await lv.can_handle(_ctx("https://boards.greenhouse.io/acme/jobs/1")) == 0.0
    
    # Ashby match
    assert await ash.can_handle(_ctx("https://jobs.ashbyhq.com/acme/1")) == 0.9
    assert await ash.can_handle(_ctx("https://boards.greenhouse.io/acme/jobs/1")) == 0.0

@pytest.mark.asyncio
async def test_registry_picking_with_vendors():
    reg = AtsAdapterRegistry()
    reg.register(GreenhouseAdapter())
    reg.register(LeverAdapter())
    reg.register(AshbyAdapter())
    
    # Check Greenhouse
    chosen = await reg.pick(_ctx("https://boards.greenhouse.io/acme/jobs/1"))
    assert chosen.name == "greenhouse"
    
    # Check Lever
    chosen = await reg.pick(_ctx("https://jobs.lever.co/acme/1"))
    assert chosen.name == "lever"
    
    # Check Ashby
    chosen = await reg.pick(_ctx("https://jobs.ashbyhq.com/acme/1"))
    assert chosen.name == "ashby"


# ---------------------------------------------------------------------------
# AshbyAdapter fill/EEO/wizard/submit logic (ACES-68) — not just can_handle.
# ---------------------------------------------------------------------------

ASHBY_URL = "https://jobs.ashbyhq.com/acme/1"


class _AshbyEl:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    async def fill(self, v):
        self.page.filled[self.sel] = v

    async def click(self):
        self.page.clicks.append(self.sel)


class FakeAshbyPage:
    """Fake Playwright page for AshbyAdapter. Dispatches page.evaluate(...) results
    by a distinctive substring in the script, same technique as the existing
    test_ats_generic_adapter.py / test_vendor_cta_adapters.py fakes. `steps` models
    a (possibly multi-page) wizard: each entry is the set of selectors considered
    "present" on that step; a successful Next click advances to the next entry."""

    def __init__(self, steps=None, blocker=None, questions=None,
                 question_fill_results=None, receipt=None, form_gone=False,
                 infinite_next=False):
        self.url = ASHBY_URL
        self.steps = steps if steps is not None else [set()]
        self._step_index = 0
        self.present = set(self.steps[0])
        self.filled, self.clicks, self.uploaded = {}, [], {}
        self.evaluated = []  # (script, arg) pairs, for assertions
        self._blocker = blocker
        self._questions = questions or []
        self._question_fill_results = question_fill_results or {}
        self._receipt = receipt
        self._form_gone = form_gone
        self._infinite_next = infinite_next
        self.next_click_count = 0

    async def query_selector(self, sel):
        return _AshbyEl(self, sel) if sel in self.present else None

    async def set_input_files(self, sel, path):
        if sel not in self.present:
            raise RuntimeError("no file input")
        self.uploaded[sel] = path

    async def evaluate(self, script, *args):
        arg = args[0] if args else None
        self.evaluated.append((script, arg))
        if "ashby-question-scan" in script:
            return self._questions
        if "ashby-question-fill" in script:
            return self._question_fill_results.get((arg or {}).get("id"), False)
        if "ashby-click-next" in script:
            self.next_click_count += 1
            if self._infinite_next:
                return True
            if self._step_index + 1 < len(self.steps):
                self._step_index += 1
                self.present = set(self.steps[self._step_index])
                return True
            return False
        if "ashby-form-gone" in script:
            return self._form_gone
        if "captcha" in script:            # generic blocker probe
            return self._blocker
        if "label" in script:              # generic label-driven question scan
            return []
        if "thank you for" in script:      # generic receipt probe
            return self._receipt
        return None


ASHBY_PROFILE = {
    "personal_info": {"first_name": "Ada", "last_name": "Lovelace",
                      "email": "ada@example.com", "phone": "555-0100"},
    "social_links": {"linkedin": "https://linkedin.com/in/ada"},
    "disclosures": {},
}


def _ashby_ctx(page, profile=None, auto_submit=False, resume="/tmp/r.pdf"):
    return AtsApplyContext(page=page, job={"url": page.url}, profile=profile or ASHBY_PROFILE,
                           resume_path=resume, auto_submit=auto_submit, url=page.url,
                           attempt_id="a1", extra={})


def _ashby_adapter():
    a = AshbyAdapter()
    a._step_delay = 0  # skip real wall-clock waits between wizard steps in tests
    return a


ASHBY_NAME_SEL = "input[placeholder*='name' i], input[aria-label*='name' i]"
ASHBY_EMAIL_SEL = "input[type='email']"
ASHBY_RESUME_SEL = "input[type='file'][accept*='pdf' i], input[type='file']"
ASHBY_SUBMIT_SEL = "button[type='submit']"


@pytest.mark.asyncio
async def test_ashby_fills_identity_resume_and_withholds_submit_by_default():
    page = FakeAshbyPage(steps=[{ASHBY_NAME_SEL, ASHBY_EMAIL_SEL, ASHBY_RESUME_SEL, ASHBY_SUBMIT_SEL}])
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=False))

    assert page.filled.get(ASHBY_NAME_SEL) == "Ada Lovelace"  # combined-name field
    assert page.filled.get(ASHBY_EMAIL_SEL) == "ada@example.com"
    assert page.uploaded.get(ASHBY_RESUME_SEL) == "/tmp/r.pdf"
    assert res.submitted is False
    assert res.status == "review_ready"
    assert page.clicks == []  # never clicked submit


@pytest.mark.asyncio
async def test_ashby_blocker_short_circuits_before_any_fill():
    page = FakeAshbyPage(steps=[{ASHBY_SUBMIT_SEL}], blocker="captcha")
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=True))
    assert res.submitted is False and res.status == "captcha"
    assert page.filled == {} and page.clicks == []


@pytest.mark.asyncio
async def test_ashby_eeo_native_select_answered_via_answer_bank():
    """Gender rendered as a native <select> — resolved through AnswerBank, never
    hardcoded, and filled via the select-specific JS path."""
    profile = {**ASHBY_PROFILE, "disclosures": {"gender": "male"}}
    page = FakeAshbyPage(
        steps=[{ASHBY_SUBMIT_SEL}],
        questions=[{"kind": "select", "id": "ashby-q-0", "text": "Gender"}],
        question_fill_results={"ashby-q-0": True},
    )
    res = await _ashby_adapter().apply(_ashby_ctx(page, profile=profile, auto_submit=False))

    assert "q:Gender" in res.analytics["evidence"]["evidence_fields_filled"]
    fill_calls = [a for s, a in page.evaluated if "ashby-question-fill:select" in s]
    assert fill_calls and fill_calls[0]["id"] == "ashby-q-0"
    assert "male" in fill_calls[0]["keywords"]


@pytest.mark.asyncio
async def test_ashby_eeo_decline_expands_to_synonym_keywords():
    """A 'decline' disclosure must try more than AnswerBank's own literal string,
    since real Ashby companies word the decline option differently."""
    profile = {**ASHBY_PROFILE, "disclosures": {"race": "prefer not to say"}}
    page = FakeAshbyPage(
        steps=[{ASHBY_SUBMIT_SEL}],
        questions=[{"kind": "listbox", "id": "ashby-q-1", "text": "Race/Ethnicity"}],
        question_fill_results={"ashby-q-1": True},
    )
    res = await _ashby_adapter().apply(_ashby_ctx(page, profile=profile, auto_submit=False))

    assert "q:Race/Ethnicity" in res.analytics["evidence"]["evidence_fields_filled"]
    fill_calls = [a for s, a in page.evaluated if "ashby-question-fill:listbox" in s]
    assert fill_calls and fill_calls[0]["id"] == "ashby-q-1"
    assert "decline" in fill_calls[0]["keywords"]
    assert any("prefer not" in k for k in fill_calls[0]["keywords"])


@pytest.mark.asyncio
async def test_ashby_unanswered_question_is_left_alone():
    """No disclosure on file -> AnswerBank returns None -> no fill attempted."""
    page = FakeAshbyPage(
        steps=[{ASHBY_SUBMIT_SEL}],
        questions=[{"kind": "select", "id": "ashby-q-0", "text": "Gender"}],
        question_fill_results={"ashby-q-0": True},  # would succeed if ever called
    )
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=False))
    assert not any("ashby-question-fill" in s for s, _ in page.evaluated)
    assert "q:Gender" not in res.analytics["evidence"]["evidence_fields_filled"]


@pytest.mark.asyncio
async def test_ashby_multistep_wizard_advances_then_submits():
    """Step 1 has no submit control (forces a Next click); step 2 does."""
    page = FakeAshbyPage(steps=[set(), {ASHBY_SUBMIT_SEL}], receipt="t:thank you for applying")
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=True))

    assert page.next_click_count == 1
    assert ASHBY_SUBMIT_SEL in page.clicks
    assert res.submitted is True and res.verified is True and res.status == "applied"


@pytest.mark.asyncio
async def test_ashby_bounded_wizard_gives_up_and_reports_submit_not_found():
    """A wizard that always offers Next but never a submit control must not hang —
    the adapter gives up after _MAX_WIZARD_STEPS and reports submit_not_found."""
    from src.sources.adapters import ashby as ashby_module

    page = FakeAshbyPage(steps=[set()], infinite_next=True)
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=True))

    assert page.next_click_count == ashby_module._MAX_WIZARD_STEPS
    assert res.submitted is False and res.status == "submit_not_found"


@pytest.mark.asyncio
async def test_ashby_submit_click_without_receipt_falls_back_to_form_gone():
    """Ashby's SPA often swaps the form for a bare confirmation panel with no
    distinctive copy — the generic receipt check misses it, and the Ashby-specific
    form-removed signal is a diagnostic annotation only, never an upgrade to
    submitted/verified (a form disappearing is not a receipt)."""
    page = FakeAshbyPage(steps=[{ASHBY_SUBMIT_SEL}], receipt=None, form_gone=True)
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=True))

    assert ASHBY_SUBMIT_SEL in page.clicks
    assert res.submitted is False and res.verified is False and res.status == "submission_unverified"
    assert res.analytics.get("structural_signal") == "form_removed"


@pytest.mark.asyncio
async def test_ashby_submit_click_without_receipt_or_form_gone_is_unverified():
    page = FakeAshbyPage(steps=[{ASHBY_SUBMIT_SEL}], receipt=None, form_gone=False)
    res = await _ashby_adapter().apply(_ashby_ctx(page, auto_submit=True))

    assert ASHBY_SUBMIT_SEL in page.clicks
    assert res.submitted is False and res.status == "submission_unverified"


@pytest.mark.asyncio
async def test_ashby_policy_gate_can_withhold_submit():
    page = FakeAshbyPage(steps=[{ASHBY_SUBMIT_SEL}])

    class DenyPolicy:
        async def confirm_submit(self, ctx, evidence):
            return False

    ctx = _ashby_ctx(page, auto_submit=True)
    ctx.policy = DenyPolicy()
    res = await _ashby_adapter().apply(ctx)
    assert res.status == "submit_denied_by_policy"
    assert page.clicks == []


# ---------------------------------------------------------------------------
# GreenhouseAdapter — real fill/submit logic (ACES-68).
#
# Fake page follows the same pattern as tests/test_ats_generic_adapter.py and
# tests/test_vendor_cta_adapters.py: query_selector/set_input_files record
# calls against a `present` selector set, and evaluate() dispatches on a
# distinctive substring inside each JS template (no real JS engine needed).
# ---------------------------------------------------------------------------
from src.adapters_patterns.ats_selectors import SELECTORS
from src.sources.adapters.policy import AutoSubmitPolicy


class _GhElement:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def fill(self, value):
        self.page.filled[self.selector] = value

    async def click(self):
        self.page.clicked.append(self.selector)

    async def select_option(self, value=None, label=None):
        self.page.selected[self.selector] = value if value is not None else label


class GhPage:
    def __init__(self, present=(), selects=None, radio_groups=None,
                 label_questions=None, blocker=None, receipt=None):
        self.url = "https://boards.greenhouse.io/acme/jobs/1"
        self.present = set(present)
        self.selects = selects or []
        self.radio_groups = radio_groups or []
        self.label_questions = label_questions or []
        self.blocker = blocker
        self.receipt = receipt
        self.filled = {}
        self.clicked = []
        self.uploaded = {}
        self.selected = {}
        self.radio_clicks = []

    async def query_selector(self, sel):
        return _GhElement(self, sel) if sel in self.present else None

    async def set_input_files(self, sel, path):
        if sel not in self.present:
            raise RuntimeError("no file input")
        self.uploaded[sel] = path

    async def evaluate(self, script, *args):
        # order matters: the radio-click script and the radio-scan script both
        # contain the `input[type="radio"]` selector text, so the click's own
        # unique `target.click()` marker must be checked first.
        if "target.click()" in script:
            arg = args[0] if args else {}
            self.radio_clicks.append((arg.get("name"), arg.get("index")))
            return True
        if "captcha" in script:
            return self.blocker
        if "querySelectorAll('select')" in script:
            return self.selects
        if 'input[type="radio"]' in script:
            return self.radio_groups
        if "querySelectorAll('label')" in script:
            return self.label_questions
        if "thank you for" in script:
            return self.receipt
        return None


GH_PROFILE = {
    "personal_info": {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
    "social_links": {},
    "disclosures": {},
}


def _gh_ctx(page, auto_submit=False, profile=None, resume_path=None, cover_letter_path=None,
            policy=None, attempt_id="v1"):
    return AtsApplyContext(
        page=page, job={"url": page.url}, profile=profile if profile is not None else GH_PROFILE,
        resume_path=resume_path, cover_letter_path=cover_letter_path,
        auto_submit=auto_submit, url=page.url, attempt_id=attempt_id, extra={}, policy=policy,
    )


@pytest.mark.asyncio
async def test_greenhouse_fills_eeo_select_via_answer_bank():
    """EEO <select> (no <label for> text-fill path) is filled through AnswerBank,
    not skipped the way GenericAtsAdapter's plain label-scan would skip it."""
    select = {
        "id": "job_application_gender", "name": "", "label": "Gender",
        "options": [
            {"index": 0, "value": "", "text": "Please select"},
            {"index": 1, "value": "male", "text": "Male"},
            {"index": 2, "value": "female", "text": "Female"},
            {"index": 3, "value": "decline", "text": "Decline to Self-Identify"},
        ],
    }
    page = GhPage(present={"#job_application_gender"}, selects=[select])
    ctx = _gh_ctx(page, profile={**GH_PROFILE, "disclosures": {"gender": "Male"}})

    res = await GreenhouseAdapter().apply(ctx)

    assert page.selected.get("#job_application_gender") == "male"
    assert res.status == "review_ready"  # submit still withheld (auto_submit=False)


@pytest.mark.asyncio
async def test_greenhouse_eeo_select_avoids_male_female_substring_collision():
    """"male" is a raw substring of "female" — a naive substring match would pick
    the wrong option. Female is listed BEFORE Male here to prove the fix doesn't
    depend on option order."""
    select = {
        "id": "job_application_gender", "name": "", "label": "Gender",
        "options": [
            {"index": 0, "value": "", "text": "Please select"},
            {"index": 1, "value": "female", "text": "Female"},
            {"index": 2, "value": "male", "text": "Male"},
        ],
    }
    page = GhPage(present={"#job_application_gender"}, selects=[select])
    ctx = _gh_ctx(page, profile={**GH_PROFILE, "disclosures": {"gender": "Male"}})

    await GreenhouseAdapter().apply(ctx)

    assert page.selected.get("#job_application_gender") == "male"


@pytest.mark.asyncio
async def test_greenhouse_eeo_select_decline_matches_nonstandard_option_copy():
    """AnswerBank resolves a decline disclosure to its own canonical string
    ("Decline to Self-Identify"), which won't equal a company's own custom
    wording — this must still resolve via the decline-synonym fallback."""
    select = {
        "id": "job_application_gender", "name": "", "label": "Gender",
        "options": [
            {"index": 0, "value": "male", "text": "Male"},
            {"index": 1, "value": "female", "text": "Female"},
            {"index": 2, "value": "", "text": "I don't wish to answer"},
        ],
    }
    page = GhPage(present={"#job_application_gender"}, selects=[select])
    ctx = _gh_ctx(page, profile={**GH_PROFILE, "disclosures": {"gender": "decline"}})

    await GreenhouseAdapter().apply(ctx)

    assert page.selected.get("#job_application_gender") == "I don't wish to answer"


@pytest.mark.asyncio
async def test_greenhouse_eeo_radio_group_fallback():
    """Some Greenhouse companies configure EEO as radio groups instead of a
    <select> — matched/clicked the same AnswerBank-driven way, by resolved
    option index rather than Workday's buggy boolean-semantics guess."""
    group = {
        "name": "veteran_status", "label": "Veteran Status",
        "options": [
            {"index": 0, "value": "yes", "text": "Yes"},
            {"index": 1, "value": "no", "text": "No"},
            {"index": 2, "value": "decline", "text": "Decline to Self-Identify"},
        ],
    }
    page = GhPage(radio_groups=[group])
    ctx = _gh_ctx(page, profile={**GH_PROFILE, "disclosures": {"veteran": "No"}})

    await GreenhouseAdapter().apply(ctx)

    assert page.radio_clicks == [("veteran_status", 1)]


@pytest.mark.asyncio
async def test_greenhouse_uploads_cover_letter_when_path_provided():
    """SELECTORS["greenhouse"]["file_inputs"]["cover_letter"] exists but
    GenericAtsAdapter.apply() never reads ctx.cover_letter_path."""
    cl_sel = SELECTORS["greenhouse"]["file_inputs"]["cover_letter"]
    page = GhPage(present={cl_sel})
    ctx = _gh_ctx(page, cover_letter_path="/tmp/cover.pdf")

    await GreenhouseAdapter().apply(ctx)

    assert page.uploaded.get(cl_sel) == "/tmp/cover.pdf"


@pytest.mark.asyncio
async def test_greenhouse_no_cover_letter_upload_when_path_absent():
    cl_sel = SELECTORS["greenhouse"]["file_inputs"]["cover_letter"]
    page = GhPage(present={cl_sel})
    ctx = _gh_ctx(page, cover_letter_path=None)

    await GreenhouseAdapter().apply(ctx)

    assert page.uploaded == {}


@pytest.mark.asyncio
async def test_greenhouse_end_to_end_auto_submit_with_eeo_and_receipt():
    """Full pipeline through the inherited, unmodified _gated_submit: identity +
    EEO select fill, then a verified receipt resolves to `applied` — confirms
    subclassing GenericAtsAdapter didn't disturb the shared submit gate."""
    select = {
        "id": "job_application_gender", "name": "", "label": "Gender",
        "options": [
            {"index": 0, "value": "male", "text": "Male"},
            {"index": 1, "value": "female", "text": "Female"},
        ],
    }
    submit_sel = SELECTORS["greenhouse"]["submit_button"][0]  # "#submit_app"
    page = GhPage(
        present={"#job_application_gender", submit_sel},
        selects=[select],
        receipt="t:thank you for applying",
    )
    ctx = _gh_ctx(
        page, auto_submit=True, profile={**GH_PROFILE, "disclosures": {"gender": "Male"}},
        policy=AutoSubmitPolicy(allow=True),
    )

    res = await GreenhouseAdapter().apply(ctx)

    assert page.selected.get("#job_application_gender") == "male"
    assert submit_sel in page.clicked
    assert res.submitted is True and res.verified is True and res.status == "applied"


@pytest.mark.asyncio
async def test_greenhouse_auto_submit_without_receipt_is_unverified():
    submit_sel = SELECTORS["greenhouse"]["submit_button"][0]
    page = GhPage(present={submit_sel}, receipt=None)
    ctx = _gh_ctx(page, auto_submit=True, policy=AutoSubmitPolicy(allow=True))

    res = await GreenhouseAdapter().apply(ctx)

    assert submit_sel in page.clicked
    assert res.submitted is False and res.status == "submission_unverified"


# ---------------------------------------------------------------------------
# LeverAdapter — real fill/submit logic (ACES-68).
#
# Same fake-page-by-dispatch-substring pattern as the Greenhouse suite above
# and tests/test_ats_generic_adapter.py / tests/test_vendor_cta_adapters.py.
# Lever-specific evaluate() calls carry their own JS-only comment token
# ("lever_eeo_select_scan" / "lever_form_removed_check") so they never collide
# with the generic blocker/label/receipt dispatch checks.
# ---------------------------------------------------------------------------


class _LeverElement:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def fill(self, value):
        self.page.filled[self.selector] = value

    async def click(self):
        self.page.clicked.append(self.selector)

    async def select_option(self, value=None, label=None):
        self.page.selected[self.selector] = value if value is not None else label


class LeverFakePage:
    def __init__(self, present=(), selects=None, label_questions=None,
                 blocker=None, receipt=None, form_removed=False):
        self.url = "https://jobs.lever.co/acme/1"
        self.present = set(present)
        self.selects = selects or []
        self.label_questions = label_questions or []
        self.blocker = blocker
        self.receipt = receipt
        self.form_removed = form_removed
        self.filled = {}
        self.clicked = []
        self.uploaded = {}
        self.selected = {}

    async def query_selector(self, sel):
        return _LeverElement(self, sel) if sel in self.present else None

    async def set_input_files(self, sel, path):
        if sel not in self.present:
            raise RuntimeError("no file input")
        self.uploaded[sel] = path

    async def evaluate(self, script, *args):
        # Lever-specific scripts are checked first: _EEO_SELECT_JS mentions
        # "application-label" (contains "label"), so it must be matched before
        # the generic "label" substring check below.
        if "lever_eeo_select_scan" in script:
            return self.selects
        if "lever_form_removed_check" in script:
            return self.form_removed
        if "captcha" in script:
            return self.blocker
        if "label" in script:
            return self.label_questions
        if "thank you for" in script:
            return self.receipt
        return None


LEVER_PROFILE = {
    "personal_info": {"first_name": "Ada", "last_name": "Lovelace",
                       "email": "ada@example.com", "phone": "555-0100"},
    "social_links": {"linkedin": "https://linkedin.com/in/ada"},
    "disclosures": {"authorized_to_work": True, "requires_sponsorship": False},
    "work_history": [{"company_name": "Acme Corp", "job_title": "Engineer",
                       "end_date": "Present"}],
}


def _lever_ctx(page, auto_submit=False, resume_path="/tmp/r.pdf", profile=None,
               policy=None, attempt_id="v1"):
    return AtsApplyContext(
        page=page, job={"url": page.url},
        profile=profile if profile is not None else LEVER_PROFILE,
        resume_path=resume_path, auto_submit=auto_submit, url=page.url,
        attempt_id=attempt_id, extra={}, policy=policy,
    )


@pytest.mark.asyncio
async def test_lever_fills_org_from_current_employer():
    """SELECTORS["lever"]["fields"]["org"] exists but isn't in Generic's
    _IDENTITY_MAP, so GenericAtsAdapter's identity loop silently skips it."""
    org_sel = SELECTORS["lever"]["fields"]["org"]
    page = LeverFakePage(present={org_sel})
    ctx = _lever_ctx(page)

    await LeverAdapter().apply(ctx)

    assert page.filled.get(org_sel) == "Acme Corp"


@pytest.mark.asyncio
async def test_lever_leaves_org_blank_when_most_recent_role_has_ended():
    """Never guess a former employer into the "org" field."""
    org_sel = SELECTORS["lever"]["fields"]["org"]
    page = LeverFakePage(present={org_sel})
    profile = {**LEVER_PROFILE, "work_history": [
        {"company_name": "OldCo", "job_title": "Engineer", "end_date": "2023-01-01"},
    ]}
    ctx = _lever_ctx(page, profile=profile)

    await LeverAdapter().apply(ctx)

    assert org_sel not in page.filled


@pytest.mark.asyncio
async def test_lever_leaves_additional_info_blank_without_explicit_profile_value():
    """No auto-generated cover-note text — the box stays untouched by default."""
    page = LeverFakePage(present={
        "textarea[name='comments' i], textarea[name*='additional' i]",
    })
    ctx = _lever_ctx(page)

    await LeverAdapter().apply(ctx)

    assert page.filled == {}


@pytest.mark.asyncio
async def test_lever_fills_additional_info_when_profile_has_explicit_value():
    ai_sel = "textarea[name='comments' i], textarea[name*='additional' i]"
    page = LeverFakePage(present={ai_sel})
    profile = {**LEVER_PROFILE, "disclosures": {
        **LEVER_PROFILE["disclosures"], "additional_information": "Happy to relocate.",
    }}
    ctx = _lever_ctx(page, profile=profile)

    await LeverAdapter().apply(ctx)

    assert page.filled.get(ai_sel) == "Happy to relocate."


@pytest.mark.asyncio
async def test_lever_eeo_select_filled_via_answer_bank():
    """Native <select> EEO field: Generic's .fill()-based label scan throws on (and
    silently skips) a <select>; the Lever-specific select scan must fill it."""
    select = {
        "id": "eeo-gender", "name": "", "label": "Gender",
        "options": [
            {"value": "1", "text": "Male"},
            {"value": "2", "text": "Female"},
            {"value": "3", "text": "I don't wish to answer"},
        ],
    }
    page = LeverFakePage(present={"#eeo-gender"}, selects=[select])
    profile = {**LEVER_PROFILE, "disclosures": {**LEVER_PROFILE["disclosures"], "gender": "Male"}}
    ctx = _lever_ctx(page, profile=profile)

    await LeverAdapter().apply(ctx)

    assert page.selected.get("#eeo-gender") == "1"


@pytest.mark.asyncio
async def test_lever_eeo_select_avoids_male_female_substring_collision():
    """"male" is a raw substring of "female" — a naive substring match on option
    text would pick the wrong option. Female is listed BEFORE Male here to prove
    the word-boundary match doesn't depend on option order."""
    select = {
        "id": "eeo-gender", "name": "", "label": "Gender",
        "options": [
            {"value": "2", "text": "Female"},
            {"value": "1", "text": "Male"},
            {"value": "3", "text": "I don't wish to answer"},
        ],
    }
    page = LeverFakePage(present={"#eeo-gender"}, selects=[select])
    profile = {**LEVER_PROFILE, "disclosures": {**LEVER_PROFILE["disclosures"], "gender": "Male"}}
    ctx = _lever_ctx(page, profile=profile)

    await LeverAdapter().apply(ctx)

    assert page.selected.get("#eeo-gender") == "1"


@pytest.mark.asyncio
async def test_lever_eeo_select_decline_maps_to_decline_worded_option():
    """AnswerBank resolves a decline disclosure to its own canonical string
    ("Decline to Self-Identify"), which won't textually equal a company's own
    custom option copy — must still resolve via the decline-synonym fallback."""
    select = {
        "id": "eeo-gender", "name": "", "label": "Gender",
        "options": [
            {"value": "1", "text": "Male"},
            {"value": "2", "text": "Female"},
            {"value": "3", "text": "I don't wish to answer"},
        ],
    }
    page = LeverFakePage(present={"#eeo-gender"}, selects=[select])
    profile = {**LEVER_PROFILE, "disclosures": {**LEVER_PROFILE["disclosures"], "gender": "decline"}}
    ctx = _lever_ctx(page, profile=profile)

    await LeverAdapter().apply(ctx)

    assert page.selected.get("#eeo-gender") == "3"


@pytest.mark.asyncio
async def test_lever_eeo_select_skipped_when_profile_has_no_disclosure():
    select = {
        "id": "eeo-gender", "name": "", "label": "Gender",
        "options": [{"value": "1", "text": "Male"}, {"value": "2", "text": "Female"}],
    }
    page = LeverFakePage(present={"#eeo-gender"}, selects=[select])
    ctx = _lever_ctx(page)  # LEVER_PROFILE has no "gender" disclosure

    await LeverAdapter().apply(ctx)

    assert page.selected == {}


@pytest.mark.asyncio
async def test_lever_submit_annotated_via_form_removed_when_receipt_text_absent():
    """Secondary, structural signal: Lever's SPA removes #application-form on
    success even when a company's custom confirmation copy misses receipt.py's
    generic text patterns. This is a diagnostic annotation only, never an
    upgrade to submitted/verified -- a form disappearing is not itself a
    receipt, and a false positive would permanently mark the job applied in
    the idempotency ledger."""
    submit_sel = SELECTORS["lever"]["submit_button"][0]
    page = LeverFakePage(present={submit_sel}, receipt=None, form_removed=True)
    ctx = _lever_ctx(page, auto_submit=True, policy=AutoSubmitPolicy(allow=True))

    res = await LeverAdapter().apply(ctx)

    assert submit_sel in page.clicked
    assert res.submitted is False and res.verified is False and res.status == "submission_unverified"
    assert res.analytics.get("structural_signal") == "form_removed"


@pytest.mark.asyncio
async def test_lever_submit_stays_unverified_when_form_still_present():
    """No text receipt AND the form is still in the DOM -> genuinely unverified,
    not upgraded to success by the structural fallback."""
    submit_sel = SELECTORS["lever"]["submit_button"][0]
    page = LeverFakePage(present={submit_sel}, receipt=None, form_removed=False)
    ctx = _lever_ctx(page, auto_submit=True, policy=AutoSubmitPolicy(allow=True))

    res = await LeverAdapter().apply(ctx)

    assert submit_sel in page.clicked
    assert res.submitted is False and res.status == "submission_unverified"


@pytest.mark.asyncio
async def test_lever_end_to_end_identity_org_eeo_and_receipt_submit():
    """Full pipeline through the inherited, unmodified policy gate: identity +
    org + EEO select fill, then a verified text receipt resolves to `applied`."""
    org_sel = SELECTORS["lever"]["fields"]["org"]
    name_sel = SELECTORS["lever"]["fields"]["name"]
    resume_sel = SELECTORS["lever"]["file_inputs"]["resume"]
    submit_sel = SELECTORS["lever"]["submit_button"][0]
    select = {
        "id": "eeo-gender", "name": "", "label": "Gender",
        "options": [{"value": "1", "text": "Male"}, {"value": "2", "text": "Female"}],
    }
    page = LeverFakePage(
        present={org_sel, name_sel, resume_sel, submit_sel, "#eeo-gender"},
        selects=[select], receipt="t:thank you for applying",
    )
    profile = {**LEVER_PROFILE, "disclosures": {**LEVER_PROFILE["disclosures"], "gender": "Male"}}
    ctx = _lever_ctx(page, auto_submit=True, profile=profile, policy=AutoSubmitPolicy(allow=True))

    res = await LeverAdapter().apply(ctx)

    assert page.filled.get(name_sel) == "Ada Lovelace"
    assert page.filled.get(org_sel) == "Acme Corp"
    assert page.uploaded.get(resume_sel) == "/tmp/r.pdf"
    assert page.selected.get("#eeo-gender") == "1"
    assert submit_sel in page.clicked
    assert res.submitted is True and res.verified is True and res.status == "applied"
