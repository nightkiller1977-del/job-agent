"""Data-only module containing CSS selectors, XPath selectors, and regex matching patterns
for common ATS platforms (Greenhouse, Lever, Ashby, Workday, Microsoft, BrassRing,
SmartRecruiters, Teamtailor).

The Microsoft/BrassRing/SmartRecruiters/Teamtailor entries are heuristic, attribute-based
selectors (name/id/aria/type contains) — robust across portal variants but best refined
against live DOM. They let the CTA adapters (vendor_cta.py) fill + auto-submit via the
shared GenericAtsAdapter path once a form is reached.
"""

SELECTORS = {
    "greenhouse": {
        "form": "#application_form",
        "submit_button": [
            "#submit_app",
            "#apply_button",
            "input[type='submit']",
            "button[type='submit']"
        ],
        "file_inputs": {
            "resume": "input[type='file'][name*='resume' i], input[type='file'][id*='resume' i]",
            "cover_letter": "input[type='file'][name*='cover_letter' i], input[type='file'][id*='cover_letter' i]"
        },
        "fields": {
            "first_name": "input[name*='first_name' i], input[id*='first_name' i]",
            "last_name": "input[name*='last_name' i], input[id*='last_name' i]",
            "email": "input[name*='email' i], input[id*='email' i]",
            "phone": "input[name*='phone' i], input[id*='phone' i]",
            "linkedin": "input[name*='linkedin' i], input[id*='linkedin' i]",
            "github": "input[name*='github' i], input[id*='github' i]",
            "portfolio": "input[name*='portfolio' i], input[name*='website' i], input[name*='urls[portfolio]' i]"
        }
    },
    "lever": {
        "form": "#application-form",
        "submit_button": [
            "#post-submit-btn",
            "[data-qa='btn-submit']",
            "button[type='submit']"
        ],
        "file_inputs": {
            "resume": "input[type='file'][id*='resume' i], input[type='file'][name*='resume' i]"
        },
        "fields": {
            "name": "input[name='name' i]",
            "email": "input[name='email' i]",
            "phone": "input[name='phone' i]",
            "org": "input[name='org' i]",
            "linkedin": "input[name*='linkedin' i]",
            "github": "input[name*='github' i]",
            "portfolio": "input[name*='portfolio' i], input[name*='urls[portfolio]' i], input[name*='website' i]"
        }
    },
    "ashby": {
        "form": "form",
        "submit_button": [
            "button[type='submit']",
            "//button[contains(text(), 'Submit')]",
            "//button[contains(text(), 'Apply')]"
        ],
        "file_inputs": {
            "resume": "input[type='file'][accept*='pdf' i], input[type='file']"
        },
        "fields": {
            "name": "input[placeholder*='name' i], input[aria-label*='name' i]",
            "email": "input[type='email']",
            "phone": "input[type='tel']",
            "linkedin": "input[placeholder*='linkedin' i], input[aria-label*='linkedin' i]",
            "github": "input[placeholder*='github' i], input[aria-label*='github' i]",
            "portfolio": "input[placeholder*='portfolio' i], input[placeholder*='website' i], input[aria-label*='portfolio' i]"
        }
    },
    "workday": {
        "submit_button": [
            "[data-automation-id='bottom-navigation-next-button']",
            "[data-automation-id='submitButton']",
            "button[data-automation-id='nextButton']",
            "button[data-automation-id='bottomNavigationSubmitButton']"
        ],
        "file_inputs": {
            "resume": "div[data-automation-id='file-upload-drop-zone'] input[type='file'], input[type='file']"
        },
        "fields": {
            "first_name": "input[data-automation-id='legalNameSection_firstName']",
            "last_name": "input[data-automation-id='legalNameSection_lastName']",
            "email": "input[data-automation-id='email']",
            "phone": "input[data-automation-id='phone-number']",
            "address": "input[data-automation-id='addressSection_addressLine1']",
            "city": "input[data-automation-id='addressSection_city']",
            "postal_code": "input[data-automation-id='addressSection_postalCode']"
        }
    },
    "teamtailor": {
        "form": "form[action*='application' i], form.form, form",
        "submit_button": [
            "button[type='submit']",
            "input[type='submit']",
            "//button[contains(., 'Submit application')]",
            "//button[contains(., 'Send application')]"
        ],
        "file_inputs": {
            "resume": "input[type='file'][name*='resume' i], input[type='file'][name*='cv' i], input[type='file']"
        },
        "fields": {
            "first_name": "input[name*='first' i], input[id*='first' i]",
            "last_name": "input[name*='last' i], input[id*='last' i]",
            "name": "input[id*='full-name' i], input[name*='full_name' i]",
            "email": "input[type='email'], input[name*='email' i], input[id*='email' i]",
            "phone": "input[type='tel'], input[name*='phone' i], input[id*='phone' i]",
            "linkedin": "input[name*='linkedin' i], input[id*='linkedin' i]"
        }
    },
    "smartrecruiters": {
        "form": "form[data-test='application-form'], form",
        "submit_button": [
            "button[data-test='button-primary']",
            "#submitButton",
            "button[type='submit']",
            "//button[contains(., 'Submit')]",
            "//button[contains(., 'Send application')]"
        ],
        "file_inputs": {
            "resume": "input[type='file'][name*='resume' i], input[type='file']"
        },
        "fields": {
            "first_name": "#firstName, input[name*='firstName' i], input[name*='first_name' i], input[id*='first' i]",
            "last_name": "#lastName, input[name*='lastName' i], input[name*='last_name' i], input[id*='last' i]",
            "email": "#email, input[type='email'], input[name*='email' i]",
            "phone": "#phoneNumber, input[type='tel'], input[name*='phone' i]",
            "linkedin": "input[name*='linkedin' i], input[id*='linkedin' i]"
        }
    },
    "brassring": {
        "submit_button": [
            "input[type='submit']",
            "button[type='submit']",
            "//input[@value='Submit']",
            "//a[contains(., 'Submit')]",
            "//button[contains(., 'Submit')]"
        ],
        "file_inputs": {
            "resume": "input[type='file'][id*='resume' i], input[type='file'][name*='resume' i], input[type='file']"
        },
        "fields": {
            "first_name": "input[id*='FirstName' i], input[name*='FirstName' i], input[id*='first' i]",
            "last_name": "input[id*='LastName' i], input[name*='LastName' i], input[id*='last' i]",
            "email": "input[id*='Email' i], input[name*='Email' i], input[type='email']",
            "phone": "input[id*='Phone' i], input[name*='Phone' i], input[type='tel']"
        }
    },
    "microsoft": {
        "submit_button": [
            "button[aria-label*='Submit' i]",
            "button[type='submit']",
            "//button[contains(., 'Submit')]",
            "//button[contains(., 'Apply')]"
        ],
        "file_inputs": {
            "resume": "input[type='file']"
        },
        "fields": {
            "first_name": "input[aria-label*='First' i], input[name*='first' i], input[id*='first' i]",
            "last_name": "input[aria-label*='Last' i], input[name*='last' i], input[id*='last' i]",
            "email": "input[type='email'], input[aria-label*='Email' i], input[name*='email' i]",
            "phone": "input[type='tel'], input[aria-label*='Phone' i], input[name*='phone' i]"
        }
    }
}

QUESTION_PATTERNS = {
    "work_auth": [
        r"authorized to work",
        r"legally authorized",
        r"legal right to work",
        r"eligible to work",
        r"work in the (us|united states)"
    ],
    "sponsorship": [
        r"require.*sponsorship",
        r"visa sponsorship",
        r"require.*visa",
        r"sponsorship.*now or in the future",
        r"h-1b"
    ],
    "salary": [
        r"desired salary",
        r"expected salary",
        r"target salary",
        r"compensation expectation",
        r"salary expectation"
    ],
    "eeo_gender": [
        r"\bgender\b",
        r"\bsex\b",
        r"please identify your gender"
    ],
    "eeo_race": [
        r"race",
        r"ethnicity",
        r"hispanic or latino"
    ],
    "eeo_veteran": [
        r"veteran",
        r"military service",
        r"discharge status"
    ],
    "eeo_disability": [
        r"disability",
        r"disabled",
        r"physical or mental impairment"
    ]
}
