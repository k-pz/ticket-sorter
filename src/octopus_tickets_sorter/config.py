"""Loads config.toml, prompting for and creating it on first use."""
import tomllib
from pathlib import Path


def _load(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        return tomllib.load(f)


def load_name(config_path: Path) -> str:
    """The name used for the Google Drive folder, Sheet, and Calendar."""
    config = _load(config_path)
    if "name" in config:
        return config["name"]

    name = input("Name to use for the Google Drive folder, Sheet, and Calendar: ").strip()
    while not name:
        name = input("Name can't be empty, try again: ").strip()

    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    config_path.write_text(f'name = "{escaped}"\n')
    return name


def load_upload_flags(config_path: Path) -> dict:
    """Optional `upload_drive` / `create_calendar` booleans, used as defaults for the matching CLI flags."""
    config = _load(config_path)
    return {
        "upload_drive": config.get("upload_drive", False),
        "create_calendar": config.get("create_calendar", False),
    }
