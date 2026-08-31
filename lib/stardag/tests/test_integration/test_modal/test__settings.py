"""Unit tests for :mod:`stardag.integration.modal._settings`.

The module exists because ``FunctionSettings`` is *not* a pass-through to
``modal.App.function()``: Modal renamed three container-scaling parameters
and moved input concurrency into a decorator, and its client raises on the
old spellings rather than warning. So a settings dict is a small
translation problem, and this is where the translation is pinned.
"""

from __future__ import annotations

import typing
from unittest.mock import MagicMock

import modal
import pytest

from stardag.integration.modal import FunctionSettings
from stardag.integration.modal._settings import (
    _RENAMED_SETTINGS,
    _prepare_function_settings,
)


def _prepare(settings) -> dict:
    """The ``modal.App.function()`` kwargs half of the prepared result."""
    return _prepare_function_settings(
        settings, extra_secrets=[], auto_volumes={}
    ).kwargs


def _concurrency(settings):
    """The ``modal.concurrent`` half."""
    return _prepare_function_settings(
        settings, extra_secrets=[], auto_volumes={}
    ).concurrency


class TestLegacyNamesAreTranslated:
    """Every legacy name Modal rejects is accepted here and rewritten.

    Not a nicety: each of these raises ``DeprecationError`` from
    ``modal.App.function()``, so an app that sets one does not deploy at
    all. Nobody can be relying on the current behaviour, which is what
    makes translating rather than rejecting the right call.
    """

    @pytest.mark.parametrize(("legacy", "current"), sorted(_RENAMED_SETTINGS.items()))
    def test_each_legacy_name_is_rewritten(self, legacy, current, caplog):
        # Built as a plain dict rather than a TypedDict literal: the key is
        # parametrized, which no literal can express — and a legacy name is
        # something copied out of an older Modal doc anyway.
        settings = typing.cast(FunctionSettings, {"image": MagicMock(), legacy: 4})
        with caplog.at_level("WARNING", logger="stardag.integration.modal._settings"):
            prepared = _prepare(settings)

        concurrency = _concurrency(settings)
        # Concurrency settings leave via the decorator, the rest via kwargs.
        if current in ("max_concurrent_inputs", "target_concurrent_inputs"):
            assert concurrency == {"max_inputs": 4}
            assert current not in prepared
        else:
            assert prepared[current] == 4
        assert legacy not in prepared
        assert "renamed" in caplog.text

    @pytest.mark.parametrize("legacy", sorted(_RENAMED_SETTINGS))
    def test_modal_itself_still_rejects_the_legacy_name(self, legacy):
        """The premise, asserted rather than assumed.

        If a future Modal client accepted these again, translating them
        would become optional and this module could shrink — so the test
        that would fail first belongs here.
        """
        app = modal.App("settings-premise")
        kwargs: dict[str, typing.Any] = {"image": modal.Image.debian_slim(), legacy: 2}
        with pytest.raises(Exception, match="deprecated|renamed|Deprecated"):
            app.function(**kwargs)(lambda: None)

    def test_the_current_name_wins_when_both_are_given(self, caplog):
        """Resolving by dict order would be a rule nobody should have to
        know; the explicit, current spelling is the intentional one."""
        with caplog.at_level("WARNING", logger="stardag.integration.modal._settings"):
            prepared = _prepare(
                FunctionSettings(
                    image=MagicMock(), concurrency_limit=2, max_containers=9
                )
            )

        assert prepared["max_containers"] == 9
        assert "ignoring" in caplog.text

    def test_a_declaration_using_only_current_names_is_left_alone(self, caplog):
        with caplog.at_level("WARNING", logger="stardag.integration.modal._settings"):
            prepared = _prepare(
                FunctionSettings(
                    image=MagicMock(), max_containers=9, scaledown_window=30
                )
            )

        assert prepared["max_containers"] == 9
        assert prepared["scaledown_window"] == 30
        assert caplog.text == ""


class TestInputConcurrencyIsLiftedOutOfTheKwargs:
    """``max_concurrent_inputs`` is a decorator, not a ``function()`` kwarg."""

    def test_it_is_removed_from_the_function_kwargs(self):
        prepared = _prepare(
            FunctionSettings(
                image=MagicMock(), max_concurrent_inputs=8, target_concurrent_inputs=6
            )
        )
        assert "max_concurrent_inputs" not in prepared
        assert "target_concurrent_inputs" not in prepared

    def test_it_is_returned_under_modals_own_argument_names(self):
        assert _concurrency(
            FunctionSettings(
                image=MagicMock(), max_concurrent_inputs=8, target_concurrent_inputs=6
            )
        ) == {"max_inputs": 8, "target_inputs": 6}

    def test_a_target_alone_is_honoured(self):
        assert _concurrency(
            FunctionSettings(image=MagicMock(), target_concurrent_inputs=6)
        ) == {"target_inputs": 6}

    def test_no_declaration_is_none_rather_than_a_concurrency_of_one(self):
        """``@modal.concurrent`` changes how a container serves inputs at
        all, so a function that never asked for it must not be wrapped."""
        assert _concurrency(FunctionSettings(image=MagicMock())) is None


class TestSecretsAndVolumesStillMerge:
    """The behaviour that was here before the translation layer."""

    def test_secrets_are_deduped_by_name(self):
        secret = modal.Secret.from_name("shared")
        prepared = _prepare_function_settings(
            FunctionSettings(image=MagicMock(), secrets=[secret]),
            extra_secrets=[modal.Secret.from_name("shared")],
            auto_volumes={},
        )
        assert len(prepared.kwargs["secrets"]) == 1

    def test_user_volumes_win_over_auto_mounted_ones(self):
        user_volume, auto_volume = MagicMock(), MagicMock()
        prepared = _prepare_function_settings(
            FunctionSettings(image=MagicMock(), volumes={"/mnt/x": user_volume}),
            extra_secrets=[],
            auto_volumes={"/mnt/x": auto_volume, "/mnt/y": auto_volume},
        )
        assert prepared.kwargs["volumes"] == {
            "/mnt/x": user_volume,
            "/mnt/y": auto_volume,
        }
