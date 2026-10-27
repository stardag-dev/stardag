"""``ModalTaskExecutor.probe_detached`` classifies a call positively: only
an answer that can come from a finished call counts as one (STA-94/135).

No Modal account needed: ``modal.FunctionCall.from_id`` is faked."""

from __future__ import annotations

import pytest

try:
    import modal
    from modal.exception import (
        FunctionTimeoutError,
        InternalFailure,
        OutputExpiredError,
        RemoteError,
    )
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag.exceptions import ExecutionCancelled
from stardag.integration.modal import ModalTaskExecutor
from stardag.integration.modal._metadata import MODAL_EXECUTOR_NAME


class _Remote(ValueError):
    """A task's own exception as Modal re-raises it from the container."""

    def __init__(self) -> None:
        super().__init__("task failed")
        self.__line_cache__ = {}


def _answering(monkeypatch, outcome):
    class _Get:
        async def aio(self, *, timeout=None):
            assert timeout == 0
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    class _Call:
        get = _Get()

    monkeypatch.setattr(modal.FunctionCall, "from_id", lambda ref: _Call())


@pytest.mark.parametrize(
    "outcome, state",
    [
        pytest.param(TimeoutError(), "running", id="no-output-yet"),
        pytest.param(None, "returned", id="returned-none"),
        pytest.param(
            ExecutionCancelled("stopped"), "returned", id="stopped-before-run"
        ),
        pytest.param(FunctionTimeoutError("t"), "raised", id="backend-timeout"),
        pytest.param(InternalFailure("i"), "raised", id="internal-failure"),
        pytest.param(RemoteError("r"), "raised", id="remote-error"),
        pytest.param(_Remote(), "raised", id="task-exception"),
        pytest.param(OutputExpiredError(), "unknown", id="output-expired"),
        pytest.param(ConnectionError("no route"), "unknown", id="client-error"),
        pytest.param(ValueError("no marker"), "unknown", id="unmarked-exception"),
    ],
)
async def test_a_call_is_classified_positively(monkeypatch, outcome, state):
    _answering(monkeypatch, outcome)
    executor = ModalTaskExecutor(modal_app_name="app", worker_selector=lambda t: "w")
    assert await executor.probe_detached(MODAL_EXECUTOR_NAME, "fc-1") == state


async def test_another_executor_is_unknown(monkeypatch):
    _answering(monkeypatch, None)
    executor = ModalTaskExecutor(modal_app_name="app", worker_selector=lambda t: "w")
    assert await executor.probe_detached("elsewhere", "fc-1") == "unknown"
    assert executor.reconciles(MODAL_EXECUTOR_NAME)
    assert not executor.reconciles("elsewhere")
