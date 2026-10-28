"""``provision up`` provisions again when, and only when, the registry recycled.

Pure logic over stubs, so it runs in ordinary CI; see the note atop
``test_registry_live_diagnostics``. What it pins is the discriminator: a
recycle identified off the boot id is retried, a bounded number of times,
and a failure under an unchanged boot id is raised as it is -- a retry that
started covering ordinary provisioning failures would hide real ones.

It needs none of this directory's docker-compose services.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from stardag_integration_tests.registry_live import provision
from stardag_integration_tests.registry_live._harness import Deployment

DEPLOYMENT = Deployment(
    api_url="https://registry.example",
    modal_environment="dev-unit",
    workspace_slug="registry-live",
    environment_slug="main",
    workspace_id="w",
    environment_id="e",
    api_key="k",
    boot_id="boot-1",
)


class _Stack:
    """Stubs for everything ``_up_once`` reaches, scripted per test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # One entry per read of the boot id, in order; the last repeats. An
        # exception entry is raised, as an unanswered read.
        self.boot_ids: list[str | Exception] = ["boot-1"]
        self.deploy_apps_error: BaseException | None = None
        self.deploys = 0
        self._reads = 0
        monkeypatch.setattr(provision, "_require_disposable_name", lambda *a: None)
        monkeypatch.setattr(provision, "_require_expected_workspace", lambda: None)
        monkeypatch.setattr(provision, "_ensure_modal_environment", lambda name: None)
        monkeypatch.setattr(provision, "_repo_root", lambda: tmp_path)
        monkeypatch.setattr(provision, "deploy_registry", self._deploy_registry)
        monkeypatch.setattr(provision, "read_boot_id", self._read_boot_id)
        monkeypatch.setattr(provision, "connect", self._connect)
        monkeypatch.setattr(provision, "_deploy_dag_apps", self._deploy_dag_apps)
        monkeypatch.setattr(
            provision, "coordinates_path", lambda env: tmp_path / f"{env}.json"
        )
        # ``_up_once`` writes these into ``os.environ``; setting them here
        # first is what makes monkeypatch put the originals back.
        for name in provision.sdk_environment(DEPLOYMENT):
            monkeypatch.setenv(name, "")
        monkeypatch.setenv("STARDAG_PROFILE", "")
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.setattr(provision, "PROVISION_BOOT_READ_PAUSE_SECONDS", 0.0)

    def _deploy_registry(self, *args, **kwargs) -> str:
        self.deploys += 1
        return DEPLOYMENT.api_url

    def _read_boot_id(self, api_url: str, **kwargs) -> str:
        index = min(self._reads, len(self.boot_ids) - 1)
        self._reads += 1
        value = self.boot_ids[index]
        if isinstance(value, Exception):
            raise value
        return value

    def _connect(self, api_url: str, **kwargs) -> Deployment:
        return replace(DEPLOYMENT, boot_id=self._read_boot_id(api_url))

    def _deploy_dag_apps(self, *args) -> None:
        if self.deploy_apps_error is not None:
            error, self.deploy_apps_error = self.deploy_apps_error, None
            raise error


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Stack:
    return _Stack(monkeypatch, tmp_path)


def test_a_stable_boot_id_provisions_once(stack: _Stack) -> None:
    assert provision.up("dev-unit").boot_id == "boot-1"
    assert stack.deploys == 1


def test_a_failure_under_a_stable_boot_id_is_raised_not_retried(
    stack: _Stack,
) -> None:
    stack.deploy_apps_error = SystemExit("Failed to deploy 'registry-live-dag'")
    with pytest.raises(SystemExit, match="registry-live-dag"):
        provision.up("dev-unit")
    assert stack.deploys == 1


def test_a_failure_after_a_recycle_provisions_again(stack: _Stack) -> None:
    # First boot read, connect's read, then the failure's check sees a new
    # container; the second attempt runs against it throughout.
    stack.boot_ids = ["boot-1", "boot-1", "boot-2"]
    stack.deploy_apps_error = SystemExit("InvalidAPIKeyError")
    assert provision.up("dev-unit").boot_id == "boot-2"
    assert stack.deploys == 2


def test_a_recycle_with_no_failure_still_provisions_again(stack: _Stack) -> None:
    # Everything succeeded, but against a database that is gone by the end.
    stack.boot_ids = ["boot-1", "boot-1", "boot-2"]
    assert provision.up("dev-unit").boot_id == "boot-2"
    assert stack.deploys == 2


def test_recycles_are_retried_a_bounded_number_of_times(stack: _Stack) -> None:
    stack.boot_ids = [f"boot-{i}" for i in range(100)]
    with pytest.raises(provision.RegistryRecycledWhileProvisioning):
        provision.up("dev-unit")
    assert stack.deploys == provision.PROVISION_ATTEMPTS


def test_a_recycle_still_starting_is_identified_once_it_answers(
    stack: _Stack,
) -> None:
    # The replacement does not answer the first check after the failure.
    unanswered = TimeoutError("read timed out")
    stack.boot_ids = ["boot-1", "boot-1", unanswered, unanswered, "boot-2"]
    stack.deploy_apps_error = SystemExit("InvalidAPIKeyError")
    assert provision.up("dev-unit").boot_id == "boot-2"
    assert stack.deploys == 2


def test_a_failure_with_the_registry_unanswerable_is_raised_not_retried(
    stack: _Stack,
) -> None:
    stack.boot_ids = ["boot-1", "boot-1", TimeoutError("read timed out")]
    stack.deploy_apps_error = SystemExit("Failed to deploy 'registry-live-dag'")
    with pytest.raises(SystemExit, match="registry-live-dag"):
        provision.up("dev-unit")
    assert stack.deploys == 1


def test_success_needs_the_boot_id_to_answer_at_the_end(stack: _Stack) -> None:
    stack.boot_ids = ["boot-1", "boot-1", TimeoutError("read timed out")]
    with pytest.raises(SystemExit, match="did not answer"):
        provision.up("dev-unit")
    assert stack.deploys == 1
