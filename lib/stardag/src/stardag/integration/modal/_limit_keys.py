"""The type of a deployed app's ``limit_key_selector``.

A task's registry concurrency-limit keys are computed by a deployed-app
callable, applied by every tick to the instance it is about to run and sent
with the claiming start (limit-key selection may read non-significant
fields, so the keys are per instance and per claim). Ticks get the selector
through :class:`~stardag.integration.modal._tick._TickDeployment`.
"""

from __future__ import annotations

import typing

from stardag import BaseTask

LimitKeySelector = typing.Callable[[BaseTask], typing.Sequence[str]]
"""Maps a task to the named registry concurrency-limit keys it runs under."""
