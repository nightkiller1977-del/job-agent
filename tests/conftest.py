import pytest

def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run live integration tests against real sites"
    )

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: mark test as requiring a live site and active session/credentials"
    )

def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="need --run-live option to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
