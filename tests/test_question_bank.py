import pytest
from src.answers.question_bank import AnswerBank

def test_work_authorization_matching():
    profile = {
        "disclosures": {
            "authorized_to_work": True,
            "requires_sponsorship": False
        }
    }
    bank = AnswerBank(profile)
    
    # Test work auth
    assert bank.get_answer_for_question("Are you legally authorized to work in the United States?") == "Yes"
    assert bank.get_answer_for_question("Do you have the legal right to work in the US?") == "Yes"
    assert bank.get_answer_for_question("authorized to work", field_type="boolean") is True

    # Test visa sponsorship
    assert bank.get_answer_for_question("Will you now or in the future require visa sponsorship?") == "No"
    assert bank.get_answer_for_question("require visa sponsorship", field_type="boolean") is False

def test_salary_matching():
    profile = {
        "disclosures": {
            "desired_salary": "$180,000"
        }
    }
    bank = AnswerBank(profile)
    
    assert bank.get_answer_for_question("What is your desired salary?") == "$180,000"
    assert bank.get_answer_for_question("expected salary", field_type="number") == 180000

def test_voluntary_disclosures_matching():
    profile = {
        "disclosures": {
            "gender": "Male",
            "race": "White",
            "veteran": "No",
            "disability": "Decline to Self-Identify"
        }
    }
    bank = AnswerBank(profile)
    
    assert bank.get_answer_for_question("Please identify your gender") == "Male"
    assert bank.get_answer_for_question("race/ethnicity") == "White"
    assert bank.get_answer_for_question("Are you a protected veteran?") == "No"
    assert bank.get_answer_for_question("disability status") == "Decline to Self-Identify"

def test_years_of_experience_matching():
    profile = {
        "skills": [
            {"name": "Python", "years": 7},
            "AWS",
            {"name": "Kubernetes", "years": 4}
        ]
    }
    bank = AnswerBank(profile)
    
    assert bank.get_answer_for_question("How many years of experience do you have with Python?") == "7"
    assert bank.get_answer_for_question("years of experience with Python", field_type="number") == 7
    assert bank.get_answer_for_question("years of experience with AWS") == "5"  # Fallback for string list items
    assert bank.get_answer_for_question("years of experience with Kubernetes", field_type="number") == 4
    assert bank.get_answer_for_question("years of experience with Docker", field_type="number") == 5  # Global default fallback
