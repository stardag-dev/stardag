"""The sync-to-async compatibility adapters on ``RegistryABC`` pass every
keyword through: a sync-only custom registry must see the scope and config a
resumed or started build carries, or it would run under a different
structure than the build it resumes."""

from __future__ import annotations

from uuid import uuid4

import pytest

from stardag.registry import NoOpRegistry


class _Recording(NoOpRegistry):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def build_start(
        self,
        root_tasks=None,
        description=None,
        executor_metadata=None,
        *,
        scope_key=None,
        build_config=None,
    ):
        self.calls.append(
            ("start", {"scope_key": scope_key, "build_config": build_config})
        )
        return uuid4()

    def build_resume(
        self, build_id, executor_metadata=None, *, scope_key=None, build_config=None
    ):
        self.calls.append(
            ("resume", {"scope_key": scope_key, "build_config": build_config})
        )


@pytest.mark.asyncio
async def test_the_async_adapters_forward_scope_and_config():
    registry = _Recording()
    config = {"ns.T": {"width": 3}}
    await registry.build_start_aio(
        root_tasks=[], scope_key="code:cfg", build_config=config
    )
    await registry.build_resume_aio(uuid4(), scope_key="code:cfg", build_config=config)
    assert registry.calls == [
        ("start", {"scope_key": "code:cfg", "build_config": config}),
        ("resume", {"scope_key": "code:cfg", "build_config": config}),
    ]
