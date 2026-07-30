"""TOML configuration loading and validation."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from agent_lab.contracts import ExperimentSpec, ModelProfile


def load_toml(path: Path) -> dict[str, Any]:
    """Read a TOML document from disk."""

    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_experiment(path: Path) -> ExperimentSpec:
    """Load and validate an experiment profile."""

    return ExperimentSpec.model_validate(load_toml(path))


def load_model_profile(path: Path) -> ModelProfile:
    """Load and validate a model profile."""

    return ModelProfile.model_validate(load_toml(path))
