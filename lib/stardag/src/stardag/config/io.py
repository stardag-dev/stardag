"""File I/O utilities for TOML and JSON configuration files."""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --- TOML loading ---


def load_toml_file(path: Path) -> dict[str, Any]:
    """Load a TOML config file, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.debug(f"Could not load {path}: {e}")
        return {}


def save_toml_file(path: Path, data: dict[str, Any]) -> None:
    """Save data to a TOML file."""
    try:
        import tomli_w  # type: ignore[import-not-found]

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump(data, f)
    except ImportError:
        raise ImportError(
            "tomli-w is required to write TOML files. Install with: pip install tomli-w"
        )


def load_json_file(path: Path) -> dict[str, Any]:
    """Load a JSON config file, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug(f"Could not load {path}: {e}")
        return {}


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    """Save data to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
