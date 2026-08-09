"""Tests for the CLI's human-duration grammar (``--older-than 24h``)."""

import pytest

from stardag._cli._duration import format_duration, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("30", 30),  # bare number = seconds
            ("90s", 90),
            ("90m", 90 * 60),
            ("24h", 24 * 3600),
            ("3d", 3 * 86400),
            ("2w", 14 * 86400),
            ("1H", 3600),  # case insensitive
            ("  24h  ", 24 * 3600),  # surrounding whitespace ignored
        ],
    )
    def test_accepted(self, value, expected):
        assert parse_duration(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "h",
            "-1h",
            "1.5h",  # fractions are out of grammar (see the module docstring)
            "1h30m",  # compound forms too
            "24 h",  # internal whitespace
            "24hours",
            "24y",  # not a fixed-length unit
            "abc",
            "1e3",
        ],
    )
    def test_rejected(self, value):
        with pytest.raises(ValueError) as excinfo:
            parse_duration(value)
        # The message names the offending input and restates the grammar, so
        # a CLI can print it as-is.
        assert repr(value) in str(excinfo.value) or value.strip() in str(excinfo.value)

    @pytest.mark.parametrize("value", ["0", "0s", "0h"])
    def test_zero_rejected(self, value):
        """A zero threshold matches everything, including live builds."""
        with pytest.raises(ValueError, match="greater than zero"):
            parse_duration(value)


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0s"),
            (45, "45s"),
            (59, "59s"),
            (60, "1m"),
            (12 * 60 + 30, "12m"),  # coarse on purpose
            (3600, "1h"),
            (5 * 3600 + 59 * 60, "5h"),
            (86400, "1d"),
            (9 * 86400, "9d"),
            (14 * 86400, "14d"),  # days are the ceiling, weeks are not shown
        ],
    )
    def test_largest_nonzero_unit(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_negative_is_named_not_rendered(self):
        """Clock skew must read as skew, not as a negative age."""
        assert format_duration(-5) == "in the future"
