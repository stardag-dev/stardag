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


@pytest.fixture(autouse=True)
def hermetic_modal_executor_metadata(monkeypatch):
    """Keep the unit tier hermetic: pin the workspace/environment used in
    executor metadata so no test performs the (network) Modal token
    workspace lookup or depends on the developer's local Modal config.

    Tests exercising the resolution logic itself override these
    monkeypatches explicitly.
    """
    from stardag.integration.modal import _app as modal_app_module

    async def _fake_workspace_aio():
        return "test-workspace"

    async def _fake_app_id_aio(app_name, environment_name=None):
        return "ap-test-app"

    async def _fake_function_id_aio(function):
        return "fu-test-fn"

    # The real (pre-patch) functions, for tests exercising the resolution
    # logic itself.
    originals = {
        "get_modal_workspace_aio": modal_app_module._get_modal_workspace_aio,
        "get_modal_app_id_aio": modal_app_module._get_modal_app_id_aio,
        "get_modal_function_id_aio": modal_app_module._get_modal_function_id_aio,
    }

    monkeypatch.setattr(
        modal_app_module, "_get_modal_workspace_aio", _fake_workspace_aio
    )
    monkeypatch.setattr(
        modal_app_module, "_get_modal_workspace", lambda: "test-workspace"
    )
    monkeypatch.setattr(modal_app_module, "_get_modal_environment", lambda: "test-env")
    # Pin the app-id / function-id resolution too, so the unit tier never
    # attempts a real ``modal.App.lookup`` / handle hydration over the
    # network. Tests exercising these helpers override them explicitly.
    monkeypatch.setattr(modal_app_module, "_get_modal_app_id_aio", _fake_app_id_aio)
    monkeypatch.setattr(
        modal_app_module, "_get_modal_function_id_aio", _fake_function_id_aio
    )
    return originals
