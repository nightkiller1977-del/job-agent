import ast
from pathlib import Path


def test_dashboard_runtime_is_mongodb_only():
    source = Path("dashboard/main.py").read_text()
    tree = ast.parse(source)
    top_level_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "psycopg2" not in top_level_imports
    assert "pymongo" in imported_from
    assert "MONGODB_URI" in source
    assert "JOB_AGENT_DB" in source
    assert "DATABASE_URL" not in source
    assert "migrate_legacy_postgres" not in source


def test_dashboard_requirements_have_no_postgres_driver():
    requirements = Path("dashboard/requirements.txt").read_text().lower()
    assert "pymongo" in requirements
    assert "dnspython" in requirements
    assert "psycopg2" not in requirements


def test_render_blueprint_has_no_postgres_resource():
    render_yaml = Path("render.yaml").read_text()
    assert "MONGODB_URI" in render_yaml
    assert "JOB_AGENT_DB" in render_yaml
    assert "DATABASE_URL" not in render_yaml
    assert "fromDatabase:" not in render_yaml
    assert "databases:" not in render_yaml
