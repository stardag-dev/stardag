"""The transaction boundary of every v2 service call, and its deadlock retry.

:func:`transaction` commits or rolls back one transaction.
:func:`retry_on_deadlock` re-runs a whole service call whose transaction
Postgres aborted as a deadlock victim (SQLSTATE ``40P01``).

Why a retry rather than a proof. Every v2 writer takes its locks in a
documented order, but that order is a convention the database can break on
its own: a foreign key takes ``FOR KEY SHARE`` nobody wrote, and a
``SELECT … ORDER BY … FOR UPDATE`` orders its locks by plan shape, which
Postgres does not guarantee. Postgres detects a cycle (after
``deadlock_timeout``, one second by default) and aborts one side; without
the retry that side is a 500. With it, deadlock-freedom stops being a
global property over every writer, present and future, and becomes a local
one per service call: **safe to re-run after a rollback**.

What makes a call safe to re-run, and what every decorated call satisfies:

- Its only effects are rows written inside its transaction(s), which the
  rollback discards. No call reaches S3, Modal, email or any in-process
  state (the rate limit is charged by the route guard, before the service
  is called, and is not charged again).
- It does not mutate its arguments, so the second attempt starts from the
  same request.
- It re-reads everything it decides on inside the transaction (state is
  never carried from a previous attempt), and ids it mints are minted again.

The request around the call is unaffected by the rollback: the auth
dependency commits what it writes (an API key's ``last_used_at``) and
detaches the rows it loaded (``auth/dependencies.py``), so neither is
dropped or expired by a retry.

A call that opens more than one transaction is re-run only while it has
committed none of them: once one has committed, a deadlock in a later one
propagates as before, since re-running the whole call would repeat the
committed part. :func:`transaction` records the commit in the retry scope.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import ParamSpec, TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.services.errors import RecordedConflict

logger = logging.getLogger(__name__)

#: Postgres' SQLSTATE for a transaction aborted to break a deadlock.
DEADLOCK_DETECTED = "40P01"

#: Attempts per call, the first included.
MAX_ATTEMPTS = 3

#: Backoff before attempt ``n + 1`` is uniform in ``[0, BASE * 2**(n - 1))``
#: seconds (full jitter), so the two sides of a cycle do not meet again in
#: step. Small next to ``deadlock_timeout``, which the victim already waited.
BACKOFF_BASE_SECONDS = 0.05


@dataclass
class _RetryScope:
    committed: bool = False


_scope: ContextVar[_RetryScope | None] = ContextVar("tx_retry_scope", default=None)


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[None]:
    """Commit on success; roll back on any exception, except a
    :class:`RecordedConflict`, whose record is committed before it is
    re-raised — a refusal never erases the history of having received it."""
    try:
        yield
        await session.commit()
    except RecordedConflict:
        await session.commit()
        _mark_committed()
        raise
    except BaseException:
        await session.rollback()
        raise
    _mark_committed()


def _mark_committed() -> None:
    scope = _scope.get()
    if scope is not None:
        scope.committed = True


def is_deadlock(exc: BaseException) -> bool:
    """Whether ``exc`` is Postgres aborting this transaction as a deadlock
    victim (asyncpg's SQLSTATE, as SQLAlchemy's translation carries it)."""
    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == DEADLOCK_DETECTED


P = ParamSpec("P")
R = TypeVar("R")


def retry_on_deadlock(
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Re-run the service call ``fn`` when Postgres aborts its transaction
    as a deadlock victim; see the module docstring for the contract ``fn``
    must meet. ``fn`` takes the session as its first parameter, ``session``.

    At most :data:`MAX_ATTEMPTS` attempts, with jittered backoff between
    them, each logged; the last attempt's deadlock propagates. A decorated
    call made from inside another one is not retried on its own (the outer
    call owns the transaction and is the one re-run)."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if _scope.get() is not None:
            return await fn(*args, **kwargs)
        # The session is the first parameter, named ``session``, passed
        # positionally or by keyword.
        session = args[0] if args else kwargs.get("session")
        assert isinstance(session, AsyncSession), "the session comes first"
        attempt = 1
        while True:
            scope = _RetryScope()
            token = _scope.set(scope)
            try:
                return await fn(*args, **kwargs)
            except DBAPIError as exc:
                if not is_deadlock(exc) or scope.committed or attempt >= MAX_ATTEMPTS:
                    raise
                # ``transaction`` has rolled back; this covers a deadlock
                # surfacing where it could not (the commit of a recorded
                # refusal), so the next attempt starts on a clean session.
                await session.rollback()
                delay = random.uniform(0, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
                logger.warning(
                    "deadlock detected in %s (attempt %d of %d); retrying in %.3fs",
                    fn.__qualname__,
                    attempt,
                    MAX_ATTEMPTS,
                    delay,
                )
                attempt += 1
                await asyncio.sleep(delay)
            finally:
                _scope.reset(token)

    return wrapper
