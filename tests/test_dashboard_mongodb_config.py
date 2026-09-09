import ast
from pathlib import Path


def test_dashboard_primary_backend_is_mongodb():
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


def test_legacy_postgres_is_migration_only():
    source = Path("dashboard/main.py").read_text()
    assert "migrate_legacy_postgres" in source
    assert 'os.environ.get("DATABASE_URL"' in source
    assert 'sslmode="require"' in source
    assert 'marker_id = "render-postgres-v1"' in source
    assert '"status": "complete"' in source


def test_render_blueprint_has_no_postgres_resource():
    render_yaml = Path("render.yaml").read_text()
    assert "MONGODB_URI" in render_yaml
    assert "JOB_AGENT_DB" in render_yaml
    assert "fromDatabase:" not in render_yaml
    assert "databases:" not in render_yaml
