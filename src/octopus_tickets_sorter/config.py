"""Loads config.toml, prompting for and creating it on first use."""
import re
import tomllib
from pathlib import Path

TABLE_HEADER = re.compile(r"^\[(?:aliases|duplicates)\]", re.M)


def _load(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as f:
        return tomllib.load(f)


def _toml_str(value: str) -> str:
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def load_name(config_path: Path) -> str:
    """The name used for the Google Drive folder, Sheet, and Calendar."""
    config = _load(config_path)
    if "name" in config:
        return config["name"]

    name = input("Name to use for the Google Drive folder, Sheet, and Calendar: ").strip()
    while not name:
        name = input("Name can't be empty, try again: ").strip()

    # Prepended, not written over: keeps any flags/aliases already in the file,
    # and a bare key has to come before the first table header anyway.
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    config_path.write_text(f"name = {_toml_str(name)}\n{existing}", encoding="utf-8")
    return name


def load_upload_flags(config_path: Path) -> dict:
    """Optional `upload_drive` / `create_calendar` booleans, used as defaults for the matching CLI flags."""
    config = _load(config_path)
    return {
        "upload_drive": config.get("upload_drive", False),
        "create_calendar": config.get("create_calendar", False),
    }


def load_aliases(config_path: Path) -> dict[str, str]:
    """The optional `[aliases]` table, mapping each name variant to the name it collapses into."""
    return _load(config_path).get("aliases", {})


def load_duplicates(config_path: Path) -> dict[str, str]:
    """The optional `[duplicates]` table, mapping the reservation code of a ticket to treat
    as a duplicate to a description of it. Only the codes are used; the value is a label so
    the file says what was marked."""
    return _load(config_path).get("duplicates", {})


def save_tables(config_path: Path, aliases: dict[str, str], duplicates: dict[str, str]) -> None:
    """Rewrite the `[aliases]` and `[duplicates]` tables, leaving everything above them
    untouched. Both are always written last, so everything from the first table header
    on can be replaced wholesale."""
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    parts = [TABLE_HEADER.split(text)[0].strip("\n")]
    for table, mapping in (("aliases", aliases), ("duplicates", duplicates)):
        if mapping:
            entries = (f"{_toml_str(k)} = {_toml_str(v)}" for k, v in sorted(mapping.items()))
            parts.append("\n".join([f"[{table}]", *entries]))
    config_path.write_text("\n\n".join(p for p in parts if p) + "\n", encoding="utf-8")
