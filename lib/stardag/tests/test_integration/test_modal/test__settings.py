"""Unit tests for :mod:`stardag.integration.modal._settings`.

The module exists because ``FunctionSettings`` is *not* a pass-through to
``modal.App.function()``: Modal renamed three container-scaling parameters
and moved input concurrency into a decorator, and its client raises on the
old spellings rather than warning. So a settings dict is a small
translation problem, and this is where the translation is pinned.
"""

from __future__ import annotations

import typing

import pytest

try:
    import modal
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from unittest.mock import MagicMock  # noqa: E402

from stardag.integration.modal import FunctionSettings  # noqa: E402
from stardag.exceptions import StardagError  # noqa: E402
from stardag.integration.modal._settings import (  # noqa: E402
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

    def test_a_target_alone_is_refused(self):
        """Modal requires ``max_inputs`` whenever the decorator is applied,
        and refuses at *registration* with a ``TypeError`` naming a
        parameter the user never wrote. Caught here instead, in the names
        they did write — and not papered over by merging stardag's own
        tick default underneath, which would invent a ceiling nobody asked
        for and could still be below the target."""
        with pytest.raises(StardagError, match="without max_concurrent_inputs"):
            _concurrency(
                FunctionSettings(image=MagicMock(), target_concurrent_inputs=6)
            )

    def test_a_target_above_the_max_is_refused(self):
        with pytest.raises(StardagError, match="above max_concurrent_inputs"):
            _concurrency(
                FunctionSettings(
                    image=MagicMock(),
                    max_concurrent_inputs=4,
                    target_concurrent_inputs=6,
                )
            )

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


class TestModalAcceptsWhatWeProduce:
    """Every concurrency dict this module can emit, through the real Modal.

    The app-level tests stub ``modal.concurrent`` out with a recording
    identity decorator so they can invoke the wrappers, which means nothing
    over there ever reaches Modal's own validation — and a declaration that
    fails only at registration would sail through the whole suite and break
    a deploy. So the shapes are checked against the real client here, the
    same way the legacy-name premise is.
    """

    @pytest.mark.parametrize(
        "settings",
        [
            FunctionSettings(image=MagicMock(), max_concurrent_inputs=10),
            FunctionSettings(
                image=MagicMock(),
                max_concurrent_inputs=10,
                target_concurrent_inputs=8,
            ),
            FunctionSettings(image=MagicMock(), allow_concurrent_inputs=3),
        ],
    )
    def test_the_declaration_registers(self, settings):
        concurrency = _concurrency(settings)
        assert concurrency is not None

        app = modal.App("settings-roundtrip")

        async def body(x):
            return x

        # Decorator order matters and this is the order ``register`` uses:
        # ``@app.function()`` on the outside, ``@modal.concurrent`` under
        # it. The reverse raises ``InvalidError``.
        app.function(image=modal.Image.debian_slim(), serialized=True, name="f")(
            modal.concurrent(**concurrency)(body)
        )

    def test_the_stardag_tick_default_registers(self):
        """The one concurrency stardag applies on its own initiative."""
        from stardag.integration.modal._app import _TICK_CONCURRENCY

        app = modal.App("settings-roundtrip-default")

        async def body(x):
            return x

        app.function(image=modal.Image.debian_slim(), serialized=True, name="tick")(
            modal.concurrent(**_TICK_CONCURRENCY)(body)
        )
