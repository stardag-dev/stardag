"""add task.tags and task.description

Annotations of a completion (STA-149): free-form tags for discovery and a
description, set by ``PATCH /tasks/{task_id}`` and filtered on by ``GET
/tasks?tag=&q=``. Mutable, and outside every hash and every registration
payload: registration inserts ``task`` rows with ``ON CONFLICT DO NOTHING``
and never names these columns, so it can neither set nor reset them.

``tags`` is ``text[] NOT NULL DEFAULT '{}'``: a constant default, so adding
it to existing rows is a catalog-only change (Postgres 11+), and every
existing task reads as untagged. ``description`` is a nullable ``text``.

Four indexes serve ``GET /tasks``: a GIN index on ``tags`` (containment),
a multicolumn trigram GIN index over ``task_name``, ``task_namespace`` and
``description`` (``q``, an ``ILIKE '%x%'`` per column; needs ``pg_trgm``,
created here if missing — a trusted extension since Postgres 13, so the
database owner may create it), a ``text_pattern_ops`` btree on
``(environment_id, task_id)`` (``id_prefix``, a ``~>=~``/``~<~`` range: the
unique index cannot serve it under a non-C collation), and
``(environment_id, status_at, id)`` for the listing's order and keyset.

Locks. The columns are added by one ``ALTER TABLE`` in a short
transaction: it takes ``ACCESS EXCLUSIVE`` on ``task`` (blocking reads and
writes) only for a catalog change, under a ``lock_timeout`` so that a
long-running transaction holding the table makes the migration fail fast
instead of queueing every other query behind it. The indexes are then
built ``CONCURRENTLY`` in an autocommit block, which holds only ``SHARE
UPDATE EXCLUSIVE``: reads and writes continue while they build. That block
commits the column change first, so the upgrade is written to be re-run
after a failure: ``ADD COLUMN IF NOT EXISTS``, ``CREATE INDEX CONCURRENTLY
IF NOT EXISTS``, and an index a failed concurrent build left INVALID is
dropped and rebuilt rather than skipped.

When the caller runs the migration inside its own transaction (the test
suite migrates scratch schemas that way), no autocommit is possible; the
indexes are then built with a plain ``CREATE INDEX IF NOT EXISTS`` in that
transaction, which blocks writes to ``task`` while they build.

Additive in both directions of a rolling deploy: an old server never reads
or writes either column, a new one reads the defaults on old rows.
Downgrade drops the indexes and both columns, and the annotations with
them. It leaves ``pg_trgm`` installed: ``IF NOT EXISTS`` cannot record
whether this revision created it, and something else may use it.

Revision ID: 7584698b441f
Revises: 3c8e1a7d5b42
Create Date: 2026-10-27 16:04:31.646196

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7584698b441f"
down_revision: Union[str, Sequence[str], None] = "3c8e1a7d5b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: How long the ``ALTER TABLE`` waits for ``task``'s lock before failing.
LOCK_TIMEOUT = "5s"

#: (name, definition after ``ON task``), in build order. ``{trgm}`` is the
#: schema-qualified ``gin_trgm_ops``.
INDEXES = (
    ("ix_task_tags", "USING gin (tags)"),
    (
        "ix_task_search_trgm",
        "USING gin (task_name {trgm}, task_namespace {trgm}, description {trgm})",
    ),
    (
        "ix_task_environment_task_id_prefix",
        "(environment_id, task_id text_pattern_ops)",
    ),
    ("ix_task_environment_status_at", "(environment_id, status_at, id)"),
)


def _in_external_transaction() -> bool:
    """Whether the caller handed Alembic a connection already inside a
    transaction it controls (the tests' scratch-schema migrations). Alembic
    records exactly this at configure time; there is no public accessor."""
    return bool(getattr(op.get_context(), "_in_external_transaction", False))


def _drop_if_invalid(bind: sa.Connection, name: str, concurrently: bool) -> None:
    """Drop index ``name`` of the current schema if a failed concurrent
    build left it INVALID, so the ``IF NOT EXISTS`` build below does not
    skip it."""
    invalid = bind.scalar(
        sa.text(
            "SELECT NOT i.indisvalid FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname = :name AND c.relnamespace = current_schema()::regnamespace"
        ),
        {"name": name},
    )
    if invalid:
        keyword = "CONCURRENTLY " if concurrently else ""
        bind.execute(sa.text(f'DROP INDEX {keyword}IF EXISTS "{name}"'))


def _create_indexes(concurrently: bool) -> None:
    bind = op.get_bind()
    # Qualified with the extension's schema: if it was already installed,
    # that schema need not be on ``search_path``.
    trgm_schema = bind.scalar(
        sa.text(
            "SELECT n.nspname FROM pg_extension e"
            " JOIN pg_namespace n ON n.oid = e.extnamespace"
            " WHERE e.extname = 'pg_trgm'"
        )
    )
    trgm = f'"{trgm_schema}".gin_trgm_ops'
    keyword = "CONCURRENTLY " if concurrently else ""
    for name, definition in INDEXES:
        _drop_if_invalid(bind, name, concurrently)
        bind.execute(
            sa.text(
                f'CREATE INDEX {keyword}IF NOT EXISTS "{name}" ON task '
                + definition.format(trgm=trgm)
            )
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute(
        "ALTER TABLE task"
        " ADD COLUMN IF NOT EXISTS tags text[] NOT NULL DEFAULT '{}',"
        " ADD COLUMN IF NOT EXISTS description text"
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    if _in_external_transaction():
        _create_indexes(concurrently=False)
        return
    with op.get_context().autocommit_block():
        _create_indexes(concurrently=True)


def downgrade() -> None:
    """Downgrade schema. ``pg_trgm`` stays installed (module docstring).

    Plain drops in one short transaction under the same ``lock_timeout``:
    dropping an index or a column is a catalog change."""
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    for name, _ in reversed(INDEXES):
        op.execute(f'DROP INDEX IF EXISTS "{name}"')
    op.execute(
        "ALTER TABLE task DROP COLUMN IF EXISTS description, DROP COLUMN IF EXISTS tags"
    )
