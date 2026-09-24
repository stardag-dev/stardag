"""The settings hash: uuid5 over canonical JSON, in its own fixed namespace.

The registry computes the same value from the posted body (its copy lives
in ``stardag_api.services.deployments``); the constants below are pinned on
both sides, so a change to either shows up as a failing test.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from stardag._core.task_id import (
    _DEFAULT_TASK_UUID5_NAMESPACE,
    _get_task_id_from_jsonable,
    instance_hash_of_body,
    task_uuid5_namespace_provider,
)
from stardag.build._settings import (
    EMPTY_SETTINGS_HASH,
    SETTINGS_HASH_NAMESPACE,
    settings_hash,
)


def test_namespace_is_derived_from_the_default_task_namespace():
    assert SETTINGS_HASH_NAMESPACE == uuid5(
        _DEFAULT_TASK_UUID5_NAMESPACE, "stardag.settings_hash.v1"
    )


def test_empty_settings_have_one_well_known_hash():
    assert settings_hash({}) == EMPTY_SETTINGS_HASH
    assert EMPTY_SETTINGS_HASH == UUID("11406eac-39d0-5b1b-9423-cfb4a1454543")


def test_hash_is_over_canonical_json():
    assert settings_hash({"B": "2", "A": "1"}) == settings_hash({"A": "1", "B": "2"})
    assert settings_hash({"A": "1"}) == uuid5(SETTINGS_HASH_NAMESPACE, '{"A":"1"}')
    assert settings_hash({"A": "é"}) == uuid5(SETTINGS_HASH_NAMESPACE, '{"A":"é"}')
    assert settings_hash({"A": "1"}) != settings_hash({"A": "2"})


def test_never_coincides_with_a_task_id_or_instance_hash_of_the_same_json():
    body = {"A": "1"}
    assert settings_hash(body) != _get_task_id_from_jsonable(body)
    assert settings_hash(body) != instance_hash_of_body('{"A":"1"}')


def test_a_task_namespace_override_does_not_move_it():
    """The registry cannot see a client-side override, so the settings hash
    must not follow one."""
    before = settings_hash({"A": "1"})
    with task_uuid5_namespace_provider.override(
        UUID("00000000-0000-0000-0000-000000000001")
    ):
        assert settings_hash({"A": "1"}) == before
