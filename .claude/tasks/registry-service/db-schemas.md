# [Task Name]

## Status

active

## Goal

Get a state-of-the-art scalable setup of DB use.

## Instructions

This task will happen iteratively. There are several steps. Some mostly boilerplate,
other of an arcitectural nature. Starting with an overview list below:

**Use async sqlalchemy everywhere (+tests)**

- [ ] Refactore current implementation to use async sql alchemy functionality.
- [ ] Extend unit tests to also cover the task endpoint (high level functionality/smoke test)

**Set up standard migration management with alembic**

- [x] Setup standard alembic migrations
- [x] Tweak: Keep migration statements in plain SQL, use this template for the migrations/script.py.mako and set it up so that there is sister .sql file.

```
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    file_text = Path(__file__).with_suffix(".sql").read_text()
    if file_text:
        op.execute(file_text)
```

- [x] Make sure migrations are applied in tests (via conftest fixtures).
- [x] In docker-compose, add an additional `alembic` service that runs migrations
- [x] Add migration handling to a app/stardag-api/README.md (also add other basic info here, keep it concise)

## Context

There is just a quick and dirty MVP in place.

## Execution Plan

### Summary Of Preparatory Analysis

### Plan

1. Step one
2. Step two
3. ...

## Decisions

Key decisions made and their rationale.

## Progress

- [x] Completed item
- [ ] Pending item

## Notes

Any additional observations, blockers, or open questions.
