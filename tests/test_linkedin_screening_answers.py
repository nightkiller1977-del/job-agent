"""Regression coverage (Codex P1, PR #80 review): LinkedIn's select/radio/text
Easy Apply fillers must resolve screening questions from AnswerBank / verified
profile disclosures, never from a hardcoded guess. Authorization, citizenship,
clearance, and sponsorship answers are legally consequential — submitting a
blanket "Yes"/"No" regardless of the real applicant's profile is a false
statement, and must fail closed (leave the field unanswered) when the profile
has no verified answer.
"""
from unittest.mock import AsyncMock

import pytest

from src.sources.linkedin import LinkedInScraper


class FakeOption:
    def __init__(self, value, text):
        self._value = value
        self._text = text

    async def get_attribute(self, name):
        return self._value if name == "value" else None

    async def inner_text(self):
        return self._text


class FakeSelect:
    def __init__(self, label, options):
        self.label = label
        self._options = options
        self.selected_value = None

    async def query_selector_all(self, selector):
        assert selector == "option"
        return self._options

    async def select_option(self, value):
        self.selected_value = value


class FakeRadio:
    def __init__(self, value, checked=False):
        self._value = value
        self._checked = checked
        self.clicked = False

    async def get_attribute(self, name):
        return self._value if name == "value" else None

    async def is_checked(self):
        return self._checked

    async def click(self):
        self.clicked = True
        self._checked = True


class FakeLabelElem:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text


class FakeRadioGroup:
    def __init__(self, label, radios):
        self._label = FakeLabelElem(label)
        self._radios = radios

    async def query_selector(self, selector):
        return self._label

    async def query_selector_all(self, selector):
        return self._radios


class FakePage:
    def __init__(self, selects=None, radio_groups=None):
        self._selects = selects or []
        self._radio_groups = radio_groups or []

    async def query_selector_all(self, selector):
        if selector == "select":
            return self._selects
        if selector == 'fieldset, div[class*="question"]':
            return self._radio_groups
        return []


def _scraper(monkeypatch, profile):
    scraper = LinkedInScraper(config={})
    monkeypatch.setattr(scraper, "_delay", AsyncMock())
    monkeypatch.setattr(scraper, "_load_profile", lambda: profile)
    return scraper


async def _label_for(page, element):
    return element.label


@pytest.mark.asyncio
async def test_radio_authorization_uses_profile_not_blanket_yes(monkeypatch):
    """A "Yes" answer is only submitted when the profile actually says so."""
    scraper = _scraper(monkeypatch, {"disclosures": {"authorized_to_work": True}})
    group = FakeRadioGroup(
        "Are you legally authorized to work in the United States?",
        [FakeRadio("Yes"), FakeRadio("No")],
    )
    page = FakePage(radio_groups=[group])

    await scraper._fill_radio_fields(page)

    assert group._radios[0].clicked is True
    assert group._radios[1].clicked is False


@pytest.mark.asyncio
async def test_radio_clearance_question_fails_closed_without_profile_data(monkeypatch):
    """Regression: the old code grouped "cleared" into the same bucket as
    authorized/citizen/eligible and always clicked Yes. A clearance question
    must never be auto-answered Yes when the profile has no clearance data —
    that would submit a false federal employment disclosure."""
    scraper = _scraper(monkeypatch, {"disclosures": {}})
    group = FakeRadioGroup(
        "Do you currently hold an active security clearance?",
        [FakeRadio("Yes"), FakeRadio("No")],
    )
    page = FakePage(radio_groups=[group])

    await scraper._fill_radio_fields(page)

    assert group._radios[0].clicked is False
    assert group._radios[1].clicked is False


@pytest.mark.asyncio
async def test_radio_sponsorship_reflects_real_disclosure_not_hardcoded_no(monkeypatch):
    """Regression: the old code hardcoded sponsorship to "No" unconditionally.
    An applicant who actually requires sponsorship must not have that denied
    on their behalf."""
    scraper = _scraper(monkeypatch, {"disclosures": {"requires_sponsorship": True}})
    group = FakeRadioGroup(
        "Will you now or in the future require visa sponsorship?",
        [FakeRadio("Yes"), FakeRadio("No")],
    )
    page = FakePage(radio_groups=[group])

    await scraper._fill_radio_fields(page)

    assert group._radios[0].clicked is True
    assert group._radios[1].clicked is False


@pytest.mark.asyncio
async def test_select_experience_fails_closed_instead_of_picking_highest(monkeypatch):
    """Regression: the old code always picked the highest experience bucket
    (>=10, preferring 20+) regardless of the applicant's real experience."""
    scraper = _scraper(monkeypatch, {})
    select = FakeSelect(
        "Years of experience with distributed systems",
        [FakeOption("0", "0-2 years"), FakeOption("1", "3-5 years"), FakeOption("2", "20+ years")],
    )
    page = FakePage(selects=[select])
    monkeypatch.setattr(scraper, "_get_field_label", _label_for)

    await scraper._fill_select_fields(page)

    assert select.selected_value is None


@pytest.mark.asyncio
async def test_select_authorization_uses_profile_answer(monkeypatch):
    scraper = _scraper(monkeypatch, {"disclosures": {"us_citizen": False, "authorized_to_work": False}})
    select = FakeSelect(
        "Are you authorized to work in the US?",
        [FakeOption("y", "Yes"), FakeOption("n", "No")],
    )
    page = FakePage(selects=[select])
    monkeypatch.setattr(scraper, "_get_field_label", _label_for)

    await scraper._fill_select_fields(page)

    assert select.selected_value == "n"


@pytest.mark.asyncio
async def test_text_notice_period_pulled_from_profile_not_left_blank(monkeypatch):
    scraper = _scraper(monkeypatch, {"disclosures": {"notice_period": "2 weeks"}})

    class FakeTextInput:
        def __init__(self):
            self.filled = None

        async def input_value(self):
            return ""

        async def fill(self, value):
            self.filled = value

    inp = FakeTextInput()

    async def _text_page_query(selector):
        return [inp]

    page = FakePage()
    page.query_selector_all = _text_page_query
    monkeypatch.setattr(scraper, "_get_field_label", AsyncMock(return_value="Notice period (weeks)"))

    await scraper._fill_text_questions(page)

    assert inp.filled == "2 weeks"
