# Single source of truth for authentication routing and credentials

HUMAN_SOURCES = {"usajobs"}
AUTOMATED_SOURCES = {"jobright", "linkedin", "indeed"}

REAUTH_CREDS = {
    "jobright": ("JOBRIGHT_EMAIL", "JOBRIGHT_PASSWORD"),
    "linkedin": ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD"),
    "indeed": ("INDEED_EMAIL", "INDEED_PASSWORD"),
}
