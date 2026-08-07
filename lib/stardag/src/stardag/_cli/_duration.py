"""Human durations for CLI staleness flags (``--older-than 24h``).

The registry API deliberately speaks two different dialects of "how old":
``idle_for_seconds`` (a duration, because the build reaper's threshold is
a recurring *policy*) and ``status_older_than`` (an absolute ISO-8601
timestamp, because a paged scan must not have its cutoff drift underneath
it). Neither is something a human should type. This module is the single
conversion at the CLI boundary, so both flags accept the same grammar and
each converts to whatever its endpoint wants.

Grammar — one integer, one optional unit, nothing else::

    <digits>[s|m|h|d|w]

``30`` (bare, seconds), ``90s``, ``90m``, ``24h``, ``3d``, ``2w``. Case
insensitive, surrounding whitespace ignored.

Deliberately *not* supported, each for a reason:

- **Compound forms** (``1h30m``). They buy nothing here — thresholds are
  round numbers — and they force the parser to define whether ``1h30``
  means 90 minutes or is an error.
- **Fractions** (``1.5h``). Same reasoning, plus a rounding rule nobody
  would remember when the result is fed to an integer-seconds API.
- **Months / years.** Not fixed-length, so ``1M`` would have to pick a
  convention silently. ``30d`` says exactly what it means.

Rejections are :class:`ValueError` with the offending input and the
grammar, so callers can turn them into a CLI error without re-explaining.
"""

import re

__all__ = ["parse_duration", "format_duration"]

# Seconds per unit suffix. Anchored on "s" so a bare number is seconds,
# which is also the unit both registry endpoints ultimately take.
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}

_PATTERN = re.compile(r"^(\d+)([smhdw]?)$", re.IGNORECASE)

_GRAMMAR_HINT = (
    "expected <number><unit> with unit one of s/m/h/d/w "
    "(e.g. '90m', '24h', '3d'); a bare number is seconds"
)


def parse_duration(value: str) -> int:
    """Parse a human duration into whole seconds.

    Raises:
        ValueError: if ``value`` does not match the grammar, or is zero.
            Zero is rejected rather than accepted-and-ignored because
            ``--older-than 0`` reads as "everything", and a staleness
            filter that silently matches live work is the one mistake
            this whole surface exists to prevent.
    """
    text = value.strip()
    match = _PATTERN.match(text)
    if not match:
        raise ValueError(f"Invalid duration {value!r}: {_GRAMMAR_HINT}.")
    amount = int(match.group(1))
    if amount == 0:
        raise ValueError(
            f"Invalid duration {value!r}: must be greater than zero "
            "(a zero staleness threshold matches everything, including "
            "builds that are actively running)."
        )
    unit = (match.group(2) or "s").lower()
    return amount * _UNIT_SECONDS[unit]


def format_duration(seconds: float) -> str:
    """Render a number of seconds compactly for table cells and messages.

    Coarse on purpose — ``3d`` beats ``3d 4h 12m 6s`` when the reader's
    question is "is this stale?". Returns the largest unit that yields a
    non-zero whole number, e.g. ``45s``, ``12m``, ``5h``, ``97d``.

    Days are the ceiling even though the parser accepts weeks: an age is
    read comparatively ("this one is much older than that one") and ``2w``
    against ``97d`` forces the reader to convert. Anything above a day is
    stale by every threshold anyone sets anyway.
    """
    total = int(seconds)
    if total < 0:
        # A blocker/build timestamped in the future (clock skew) — say so
        # rather than print a confusing negative age.
        return "in the future"
    for unit in ("d", "h", "m"):
        size = _UNIT_SECONDS[unit]
        if total >= size:
            return f"{total // size}{unit}"
    return f"{total}s"
