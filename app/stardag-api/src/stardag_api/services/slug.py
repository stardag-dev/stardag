"""Memorable slug generation for runs."""

import random

ADJECTIVES = [
    "brave",
    "calm",
    "swift",
    "bright",
    "bold",
    "clever",
    "eager",
    "gentle",
    "happy",
    "keen",
    "lively",
    "merry",
    "noble",
    "proud",
    "quick",
    "quiet",
    "rapid",
    "sharp",
    "smart",
    "steady",
    "strong",
    "warm",
    "wise",
    "witty",
    "agile",
    "cosmic",
    "golden",
    "silver",
    "mighty",
    "nimble",
]

NOUNS = [
    "tiger",
    "river",
    "falcon",
    "mountain",
    "ocean",
    "forest",
    "phoenix",
    "dragon",
    "eagle",
    "wolf",
    "hawk",
    "panther",
    "storm",
    "thunder",
    "comet",
    "meteor",
    "nebula",
    "galaxy",
    "quasar",
    "pulsar",
    "aurora",
    "cascade",
    "crystal",
    "diamond",
    "ember",
    "flame",
    "glacier",
    "horizon",
    "island",
    "jungle",
]


def generate_run_slug() -> str:
    """Generate a memorable slug for a run.

    Format: adjective-noun-number (e.g., "brave-tiger-42")
    """
    adj = random.choice(ADJECTIVES)
    noun = random.choice(NOUNS)
    num = random.randint(10, 99)
    return f"{adj}-{noun}-{num}"
