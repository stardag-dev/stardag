"""Shared fixtures for Modal integration tests.

Test tiers in this directory:

- **Unit tier** (default): everything not marked ``modal_live``. Uses fakes
  and monkeypatching; requires the ``modal`` package but no credentials.
- **Live tier** (``pytest -m modal_live``): modules marked with
  ``pytestmark = pytest.mark.modal_live`` that hit a real Modal workspace.
  Gated by :func:`stardag.testing.modal.live_modal_guard` — see its module
  docstring for the ``STARDAG_MODAL_LIVE_TESTS`` /
  ``STARDAG_MODAL_TEST_PROFILE`` environment variables.
"""

import pytest


@pytest.fixture
def modal_function_stub(monkeypatch):
    """Patch ``modal.Function.from_name`` with a recording stub.

    Returns a dict that captures the interaction:

    - ``captured["from_name"]``: kwargs of the last ``Function.from_name``
    - ``captured["op"]``: ``"spawn"`` or ``"remote"``
    - ``captured["kwargs"]``: kwargs passed to the spawn/remote call

    ``spawn`` returns ``"spawn-handle"`` and ``remote`` returns
    ``"remote-result"`` so tests can assert pass-through of the handle.
    """
    import modal

    captured: dict = {}

    class _Stub:
        def spawn(self, **kwargs):
            captured["op"] = "spawn"
            captured["kwargs"] = kwargs
            return "spawn-handle"

        def remote(self, **kwargs):
            captured["op"] = "remote"
            captured["kwargs"] = kwargs
            return "remote-result"

    def _from_name(**kwargs):
        captured["from_name"] = kwargs
        return _Stub()

    monkeypatch.setattr(modal.Function, "from_name", staticmethod(_from_name))
    return captured
