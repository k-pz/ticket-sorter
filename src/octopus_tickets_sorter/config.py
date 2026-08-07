"""Loads config.toml, prompting for and creating it on first use."""
import tomllib
from pathlib import Path


def load_name(config_path: Path) -> str:
    """The name used for the Google Drive folder, Sheet, and Calendar."""
    if config_path.exists():
        with config_path.open("rb") as f:
            return tomllib.load(f)["name"]

    name = input("Name to use for the Google Drive folder, Sheet, and Calendar: ").strip()
    while not name:
        name = input("Name can't be empty, try again: ").strip()

    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    config_path.write_text(f'name = "{escaped}"\n')
    return name
