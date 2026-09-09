import ast
from pathlib import Path


def test_dashboard_uses_mongodb_not_psycopg2():
    source = Path("dashboard/main.py").read_text()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "psycopg2" not in imported
    assert "pymongo" in imported_from
    assert "MONGODB_URI" in source
    assert "DATABASE_URL" not in source


def test_render_blueprint_has_no_postgres_resource():
    render_yaml = Path("render.yaml").read_text()
    assert "MONGODB_URI" in render_yaml
    assert "JOB_AGENT_DB" in render_yaml
    assert "fromDatabase:" not in render_yaml
    assert "databases:" not in render_yaml
