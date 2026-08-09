"""Take a run's input from Google Drive: the ticket PDFs dropped in the `unsorted` folder
inside the folder everything is sorted into, and the config.toml kept beside them.

Together with $TICKET_SORTER_TOKEN this is what lets a run happen somewhere that holds none
of it — a GitHub Actions runner — with Drive as the only thing that outlives the run.
"""
import shutil
import sys
from pathlib import Path

from googleapiclient.discovery import build

from .drive_upload import CONFIG_NAME, FOLDER_MIME, UNSORTED, execute, find_child, get_credentials
from .main import free_path

PDF_MIME = "application/pdf"


def _drive(credentials_path: Path, token_path: Path):
    return build("drive", "v3", credentials=get_credentials(credentials_path, token_path))


def _read(drive, file_id: str, what: str) -> bytes:
    return execute(drive.files().get_media(fileId=file_id), what)


def _local_name(name: str) -> str:
    """A Drive name as a file name. Drive is not a filesystem: "/" is an ordinary character
    in a name there, and ".." is a name like any other rather than the way out of a folder."""
    cleaned = Path(name.replace("/", "-")).name
    return "ticket.pdf" if cleaned in ("", ".", "..") else cleaned


def download_unsorted(name: str, dest_dir: Path, credentials_path: Path, token_path: Path) -> dict[Path, dict]:
    """Copy every PDF in `<name>/unsorted` into dest_dir, and say which Drive file each copy
    came from, keyed by where it landed.

    The originals stay where they are. That folder is where tickets get dropped, and a run
    that emptied it would take the drop point away with them; sorting the same PDFs again
    costs a download and changes nothing else, since every step after this one is a sync.
    """
    drive = _drive(credentials_path, token_path)
    root_id = find_child(drive, name, FOLDER_MIME, None)
    folder_id = find_child(drive, UNSORTED, FOLDER_MIME, root_id) if root_id else None
    if not folder_id:
        raise RuntimeError(
            f"No '{name}/{UNSORTED}' folder in Google Drive. Create a folder called '{UNSORTED}' "
            f"inside '{name}' and put the ticket PDFs in it."
        )

    listed = execute(
        drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            # md5Checksum is Drive's own hash of what a file holds, so a copy of a file
            # already listed can be told for one without being downloaded first.
            fields="files(id,name,mimeType,md5Checksum)",
            orderBy="name",
            pageSize=1000,
        ),
        "unsorted listing",
    ).get("files", [])
    pdfs, seen = [], set()
    for entry in listed:
        if entry["mimeType"] != PDF_MIME:
            print(f"skip (not a PDF): {entry['name']}", file=sys.stderr)
            continue
        # Same name, same contents: one download dropped in twice, not two tickets. Sorting
        # it again would file the second copy as a duplicate of the first, so it isn't
        # downloaded at all. This is the only place that can tell — the copies land under
        # names of their own locally, which is exactly what the scan there goes by — so a
        # file Drive gave no checksum for stays a file of its own rather than a guess.
        checksum = entry.get("md5Checksum")
        if checksum and (entry["name"], checksum) in seen:
            print(f"skip (a copy of one already listed): {entry['name']}", file=sys.stderr)
            continue
        seen.add((entry["name"], checksum))
        pdfs.append(entry)
    # Sorting nothing is not a no-op: it would empty the Drive folder, the Sheet and the
    # calendars of everything an earlier run put there, which is a lot to lose to a folder
    # someone renamed. Sorting no tickets on purpose is what deleting the folder is for.
    if not pdfs:
        raise RuntimeError(
            f"No PDFs in '{name}/{UNSORTED}', so this run would sort nothing — and a run that "
            "sorts nothing takes every ticket, row and event an earlier one created back out "
            "of Drive. Put the ticket PDFs in that folder and run again."
        )

    shutil.rmtree(dest_dir, ignore_errors=True)  # like output/: this run's copy, not a store
    dest_dir.mkdir(parents=True)
    print(f"Downloading {len(pdfs)} PDFs from '{name}/{UNSORTED}'...")
    downloaded = {}
    for n, entry in enumerate(pdfs, 1):
        # One Drive folder may hold any number of files under one name, and tickets all
        # arrive called something like `bilety.pdf`; a directory may hold one of them, so
        # the rest are downloaded beside it under names of their own rather than skipped.
        path = free_path(dest_dir, _local_name(entry["name"]))
        print(f"  [{n}/{len(pdfs)}] {entry['name']}")
        path.write_bytes(_read(drive, entry["id"], "ticket download"))
        downloaded[path] = entry
    return downloaded


def download_config(name: str, dest: Path, credentials_path: Path, token_path: Path) -> bool:
    """Replace `dest` with the config.toml kept in the Drive folder, and say whether there
    was one to replace it with. There isn't on the first run, which is not a failure: the
    local file (or the lack of one) stands, and the run writes it back at the end."""
    drive = _drive(credentials_path, token_path)
    root_id = find_child(drive, name, FOLDER_MIME, None)
    file_id = find_child(drive, CONFIG_NAME, None, root_id) if root_id else None
    if not file_id:
        print(f"No {CONFIG_NAME} in '{name}' yet; this run will put one there.")
        return False

    print(f"Using the {CONFIG_NAME} kept in '{name}'.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_read(drive, file_id, "config download"))
    return True


def rename_unsorted(new_names: dict[str, str], credentials_path: Path, token_path: Path) -> None:
    """Rename the Drive files this run read its input from, by file id. That's what
    --rename-input renames when the input came from Drive: renaming the downloaded copies
    would rename what gets thrown away at the end of the run."""
    if not new_names:
        return
    drive = _drive(credentials_path, token_path)
    for file_id, name in new_names.items():
        print(f"  renaming to {name}")
        execute(drive.files().update(fileId=file_id, body={"name": name}), "rename")
