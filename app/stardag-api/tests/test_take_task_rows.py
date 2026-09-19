"""``take_task_rows`` speaks each backend's own ON CONFLICT dialect.

PostgreSQL names the constraint and takes row locks on conflict; SQLite has
neither row locks nor ``ON CONSTRAINT``, so its statement names the conflict
target instead. Letting the PostgreSQL construct compile for SQLite gave a
target-less ``ON CONFLICT DO UPDATE`` that only SQLite 3.35+ accepts.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.dialects import postgresql, sqlite

from stardag_api.routes.builds import _lock_probe_row, take_task_rows


def _rows() -> list[dict[str, object]]:
    return [
        _lock_probe_row("abc", environment_id=uuid4(), now=datetime.now(timezone.utc))
    ]


def test_sqlite_names_the_conflict_target():
    sql = str(
        take_task_rows(_rows(), dialect_name="sqlite").compile(dialect=sqlite.dialect())
    )
    assert "ON CONFLICT (environment_id, task_id) DO UPDATE" in sql
    assert "RETURNING task_id" in sql


def test_postgresql_names_the_constraint():
    sql = str(
        take_task_rows(_rows(), dialect_name="postgresql").compile(
            dialect=postgresql.dialect()
        )
    )
    assert "ON CONFLICT ON CONSTRAINT uq_task_environment_taskid DO UPDATE" in sql
    assert "WHERE false" in sql
