"""The user-level registry of repositories, so that one timer can tick them all."""

import os
import tomllib
from pathlib import Path

import tomli_w

from lanes.models import Registry


def path() -> Path:
    """`repos.toml` under `$XDG_CONFIG_HOME`, or `~/.config` when that is unset."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "lanes" / "repos.toml"


def load() -> Registry:
    """The registry, empty when no repository has been registered yet."""
    file = path()
    return Registry.model_validate(tomllib.loads(file.read_text(encoding="utf-8")) if file.exists() else {})


def register(repo: Path) -> None:
    """Add `repo` to the registry unless it is already there."""
    registry, entry, file = load(), str(repo.resolve()), path()
    if entry not in registry.repos:
        registry.repos.append(entry)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(tomli_w.dumps(registry.model_dump()), encoding="utf-8")
