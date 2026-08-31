"""Managed OMH identity skins for Hermes.

Mirrors ``tui_widget_pack``: the skins are managed artifacts OMH installs into
``$HERMES_HOME/skins/`` and may safely refresh because a manifest proves OMH
wrote them. Hermes' own skin engine is the only consumer — a YAML dropped in
that directory themes the classic CLI, the Ink TUI, and the desktop GUI at
once, with no Hermes patching involved. A file at the destination without a
matching manifest record is user-owned and never touched.

Every shipped theme is installed together, so `omh theme use <name>` is an
instant, offline config edit rather than a fetch. Only one of them is ever
selected at a time: selection is Hermes-side, through `display.skin`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import secrets

SKIN_NAME = "omh"
SKIN_FILENAME = "omh.yaml"
MANIFEST_FILENAME = ".omh-skin.manifest.json"
_MANIFEST_SCHEMA = "omh_skin_manifest/v2"
_LEGACY_MANIFEST_SCHEMA = "omh_skin_manifest/v1"


@dataclass(frozen=True)
class SkinTheme:
    """One shipped palette: its user-facing short name and its Hermes skin name.

    Two names on purpose. ``skin_name`` is what goes into `display.skin` and
    must stay namespaced so it never collides with a Hermes built-in or another
    product's skin; ``short_name`` is what a person types and reads.
    """

    short_name: str
    skin_name: str
    filename: str
    summary: str


SKIN_THEMES: tuple[SkinTheme, ...] = (
    SkinTheme("sky", "omh", "omh.yaml", "Sky turquoise on deep teal. The OMH default."),
    SkinTheme("amber", "omh-amber", "omh-amber.yaml", "Amber gold on deep bronze."),
    SkinTheme("crimson", "omh-crimson", "omh-crimson.yaml", "Ember red-orange on deep crimson."),
    SkinTheme("mono", "omh-mono", "omh-mono.yaml", "Neutral grayscale with white accents."),
)

# `default` is not a theme, it is a way of saying "the shipped default"; both
# aliases resolve to the same skin the fresh-config path writes.
_THEME_ALIASES: dict[str, str] = {"default": "sky", "omh-sky": "sky"}


class SkinInstallError(RuntimeError):
    """The managed Hermes skin destination is unsafe."""


def available_skins() -> tuple[str, ...]:
    """Hermes skin names for every theme this build ships, in display order."""
    return tuple(theme.skin_name for theme in SKIN_THEMES)


def default_theme() -> SkinTheme:
    return SKIN_THEMES[0]


def theme_for_name(value: str) -> SkinTheme | None:
    """Resolve a short name, an alias, or a full skin name to one theme."""
    candidate = value.strip().lower()
    candidate = _THEME_ALIASES.get(candidate, candidate)
    for theme in SKIN_THEMES:
        if candidate in (theme.short_name, theme.skin_name):
            return theme
    return None


def theme_for_skin_name(value: str) -> SkinTheme | None:
    """Resolve a `display.skin` value, matching skin names only.

    Distinct from `theme_for_name` on purpose: config values are exact, and a
    foreign skin literally named `amber` must not be read as our theme.
    """
    candidate = value.strip()
    for theme in SKIN_THEMES:
        if candidate == theme.skin_name:
            return theme
    return None


def is_omh_skin_name(value: str) -> bool:
    """True when `display.skin` names a skin OMH ships, not a foreign one.

    The predicate every "is the OMH identity active?" question goes through.
    Comparing against the single default name instead used to read an operator
    who chose `omh-crimson` as an operator who chose `ares`.
    """
    return value.strip() in available_skins()


def theme_names() -> tuple[str, ...]:
    """User-facing short names, for validation messages."""
    return tuple(theme.short_name for theme in SKIN_THEMES)


def skin_payload(name: str = SKIN_NAME) -> bytes:
    theme = theme_for_name(name)
    if theme is None:
        raise SkinInstallError(f"unknown OMH skin: {name}")
    return resources.files("omh.skins").joinpath(theme.filename).read_text(encoding="utf-8").encode()


def colors_from_text(text: str) -> dict[str, str]:
    """The `colors:` block of one skin document, as token -> `#RRGGBB`.

    A deliberately narrow reader rather than a YAML dependency: OMH ships with
    zero runtime dependencies, these documents are ours, and the block is a
    flat two-space mapping of quoted hex strings. The theme picker's preview is
    one consumer -- it must paint with the palette a theme actually declares,
    not a copy that drifts from the YAML. `omh theme repair` is the other: it
    diffs an installed document against the shipped one to say, in palette
    terms, what accepting the repair would change.
    """
    colors: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside = line.startswith("colors:")
            continue
        if not inside or not line.startswith("  ") or line.startswith("    "):
            continue
        key, separator, raw = line.strip().partition(":")
        value = raw.strip().strip('"')
        if separator and value.startswith("#"):
            colors[key] = value
    return colors


def skin_colors(name: str = SKIN_NAME) -> dict[str, str]:
    """The palette of one shipped theme, read straight off its YAML."""
    return colors_from_text(skin_payload(name).decode("utf-8"))


def hex_to_rgb(value: str) -> tuple[int, int, int] | None:
    """`#RRGGBB` -> channel triple, or None when the token is not a plain hex."""
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _reject_symlink_path(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise SkinInstallError(f"refusing symlinked skin path: {current}")
        if current == current.parent:
            return
        current = current.parent


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}-{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_manifest_records(manifest: Path) -> dict[str, str]:
    """Filename -> sha256 for every file the manifest claims OMH wrote.

    Reads both schema versions. A v1 manifest predates multiple themes and
    described exactly one file, so it migrates to the single-entry map rather
    than being discarded — discarding it would make an already-installed
    `omh.yaml` look user-authored and freeze it forever.
    """
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return {}
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(record, dict):
        return {}
    schema = record.get("schema_version")
    if schema == _LEGACY_MANIFEST_SCHEMA:
        filename = record.get("filename")
        digest = record.get("sha256")
        if isinstance(filename, str) and isinstance(digest, str):
            return {filename: digest}
        return {}
    if schema != _MANIFEST_SCHEMA:
        return {}
    files = record.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        str(name): str(entry.get("sha256", ""))
        for name, entry in files.items()
        if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
    }


def _manifest_document(records: dict[str, str]) -> bytes:
    return json.dumps(
        {
            "schema_version": _MANIFEST_SCHEMA,
            "files": {name: {"sha256": digest} for name, digest in sorted(records.items())},
        },
        sort_keys=True,
    ).encode()


def _is_managed_file(destination: Path, records: dict[str, str], payload: bytes) -> bool:
    """Does OMH own this file -- by manifest record, or by being our own content?

    Two proofs, either sufficient. The manifest record is the primary one. The
    second exists because a manifest can fall behind the file it describes: on
    a real install the owner's `omh.yaml` was byte-identical to the shipped
    template while the v1 manifest still recorded an older sha, because some
    past update refreshed the file without refreshing the record. Record-only
    ownership read that machine as user-authored, which would have frozen the
    skin as `kept_unmanaged` forever and meant no future template change ever
    reached it.

    Matching the current template is a safe second proof precisely because the
    bytes are ours: adopting the file cannot destroy anything a user wrote,
    since overwriting it with the template is a no-op. A file that matches
    neither the record nor the template is genuinely user-edited and stays
    kept_unmanaged, exactly as before.
    """
    try:
        digest = _sha256(destination.read_bytes())
    except OSError:
        return False
    if records.get(destination.name) == digest:
        return True
    return digest == _sha256(payload)


_INSTALL_STATUS_ORDER = ("installed", "would_install", "unchanged", "kept_unmanaged")
_UNINSTALL_STATUS_ORDER = ("removed", "would_remove", "kept_unmanaged", "absent")


def _aggregate_status(statuses: list[str], order: tuple[str, ...]) -> str:
    for candidate in order:
        if candidate in statuses:
            return candidate
    return order[-1]


def install_skin(hermes_home: Path, *, dry_run: bool = False) -> dict[str, object]:
    """Install or refresh every shipped theme file under `<hermes_home>/skins`.

    Each theme is decided on its own: a user-authored `omh-mono.yaml` is kept
    and must not stop `omh.yaml` from being refreshed. The aggregate `status`
    exists for the setup summary, which reports one line per managed artifact.
    """
    destination_dir = hermes_home / "skins"
    manifest = destination_dir / MANIFEST_FILENAME
    _reject_symlink_path(hermes_home)
    _reject_symlink_path(destination_dir)
    records = _read_manifest_records(manifest)
    updated = dict(records)
    entries: list[dict[str, str]] = []
    wrote_any = False
    for theme in SKIN_THEMES:
        destination = destination_dir / theme.filename
        _reject_symlink_path(destination)
        if destination.exists() and not destination.is_file():
            raise SkinInstallError(f"skin destination is not a regular file: {destination}")
        payload = skin_payload(theme.skin_name)
        if destination.exists() and not _is_managed_file(destination, records, payload):
            # A user-authored file wins over ours forever; identity is theirs
            # to override and update must not undo that.
            updated.pop(theme.filename, None)
            entries.append(
                {
                    "skin": theme.skin_name,
                    "filename": theme.filename,
                    "status": "kept_unmanaged",
                    "path": str(destination),
                }
            )
            continue
        status = "unchanged" if destination.exists() and destination.read_bytes() == payload else "installed"
        if dry_run and status == "installed":
            status = "would_install"
        elif status == "installed":
            destination_dir.mkdir(parents=True, exist_ok=True)
            _reject_symlink_path(destination_dir)
            _atomic_write_bytes(destination, payload)
            wrote_any = True
        if not dry_run:
            updated[theme.filename] = _sha256(payload)
        entries.append(
            {
                "skin": theme.skin_name,
                "filename": theme.filename,
                "status": status,
                "path": str(destination),
            }
        )
    if not dry_run and (wrote_any or updated != records) and updated:
        destination_dir.mkdir(parents=True, exist_ok=True)
        _reject_symlink_path(manifest)
        _atomic_write_bytes(manifest, _manifest_document(updated))
    return {
        "status": _aggregate_status([entry["status"] for entry in entries], _INSTALL_STATUS_ORDER),
        "path": str(destination_dir),
        "skin": SKIN_NAME,
        "skins": entries,
    }


def uninstall_skin(hermes_home: Path, *, dry_run: bool = False) -> dict[str, object]:
    """Remove only the theme files OMH owns -- by record, or by being our bytes.

    Symmetric with `install_skin` on purpose: a file the installer would adopt
    and refresh is a file the uninstaller must take away again, or a machine
    with a stale manifest would keep orphaned skin files after removal.
    """
    destination_dir = hermes_home / "skins"
    manifest = destination_dir / MANIFEST_FILENAME
    _reject_symlink_path(destination_dir)
    _reject_symlink_path(manifest)
    records = _read_manifest_records(manifest)
    remaining = dict(records)
    entries: list[dict[str, str]] = []
    for theme in SKIN_THEMES:
        destination = destination_dir / theme.filename
        _reject_symlink_path(destination)
        if not destination.exists():
            remaining.pop(theme.filename, None)
            entries.append(
                {
                    "skin": theme.skin_name,
                    "filename": theme.filename,
                    "status": "absent",
                    "path": str(destination),
                }
            )
            continue
        if not _is_managed_file(destination, records, skin_payload(theme.skin_name)):
            entries.append(
                {
                    "skin": theme.skin_name,
                    "filename": theme.filename,
                    "status": "kept_unmanaged",
                    "path": str(destination),
                }
            )
            continue
        if not dry_run:
            destination.unlink()
            remaining.pop(theme.filename, None)
        entries.append(
            {
                "skin": theme.skin_name,
                "filename": theme.filename,
                "status": "would_remove" if dry_run else "removed",
                "path": str(destination),
            }
        )
    if not dry_run and manifest.exists():
        if remaining:
            _atomic_write_bytes(manifest, _manifest_document(remaining))
        else:
            manifest.unlink()
    return {
        "status": _aggregate_status([entry["status"] for entry in entries], _UNINSTALL_STATUS_ORDER),
        "path": str(destination_dir),
        "skins": entries,
    }


_REPAIR_STATUS_ORDER = (
    "repaired",
    "would_repair",
    "installed",
    "would_install",
    "unmanaged",
    "missing",
    "managed",
)


def _palette_changes(before: bytes, after: bytes) -> list[dict[str, str]]:
    """Which palette tokens differ between an installed document and ours.

    The "what am I accepting?" line the repair command prints BEFORE it
    overwrites anything. Palette-only on purpose: a colour token moving is the
    part a person can actually see on screen, and a full text diff of a YAML we
    ship would bury that single line under comment reflows.
    """
    try:
        current = colors_from_text(before.decode("utf-8"))
    except UnicodeDecodeError:
        # A skin file that is not UTF-8 text is not one we can describe in
        # palette terms; the digest pair still tells the honest story.
        return []
    shipped = colors_from_text(after.decode("utf-8"))
    changes: list[dict[str, str]] = []
    for key in sorted(set(current) | set(shipped)):
        old = current.get(key, "")
        new = shipped.get(key, "")
        if old != new:
            changes.append({"key": key, "before": old, "after": new})
    return changes


def repair_skins(
    hermes_home: Path,
    *,
    adopt: frozenset[str] = frozenset(),
    dry_run: bool = False,
) -> dict[str, object]:
    """Report, and on explicit consent adopt, theme files OMH does not own.

    Closes the gap `_is_managed_file`'s self-heal cannot: a manifest that went
    stale while the shipped template ALSO moved on. The installed file is then
    ours in origin but matches neither ownership proof, so it reports
    `unmanaged` and is frozen on its old palette forever -- no later release
    can ever reach it. Observed exactly that way on the owner's machine, where
    `omh.yaml` still held the pre-brightening `ui_label`.

    No digest can separate that file from one a person wrote by hand, so the
    repair is consent-based rather than automatic: `adopt` holds the theme
    FILENAMES the caller explicitly named, and running the command with them IS
    the consent. It is empty for the reporting form, which is why the bare
    command writes nothing and is safe to run twice.

    Never call this from `install_skin`, `omh update`, `omh setup`, or
    `theme use`. Silently overwriting a skin someone wrote is the one outcome
    the whole managed-artifact rule exists to prevent.
    """
    destination_dir = hermes_home / "skins"
    manifest = destination_dir / MANIFEST_FILENAME
    _reject_symlink_path(hermes_home)
    _reject_symlink_path(destination_dir)
    records = _read_manifest_records(manifest)
    updated = dict(records)
    entries: list[dict[str, object]] = []
    wrote_any = False
    for theme in SKIN_THEMES:
        destination = destination_dir / theme.filename
        _reject_symlink_path(destination)
        if destination.exists() and not destination.is_file():
            raise SkinInstallError(f"skin destination is not a regular file: {destination}")
        payload = skin_payload(theme.skin_name)
        present = destination.is_file()
        current = destination.read_bytes() if present else b""
        if not present:
            state = "missing"
        elif _is_managed_file(destination, records, payload):
            state = "managed"
        else:
            state = "unmanaged"
        consented = theme.filename in adopt
        if state == "managed" or not consented:
            # Reported, never written: `managed` needs nothing, and anything
            # else without an explicit name is exactly the file we must not
            # touch.
            status = "managed" if state == "managed" else state
        elif dry_run:
            status = "would_repair" if state == "unmanaged" else "would_install"
        else:
            status = "repaired" if state == "unmanaged" else "installed"
            destination_dir.mkdir(parents=True, exist_ok=True)
            _reject_symlink_path(destination_dir)
            _atomic_write_bytes(destination, payload)
            updated[theme.filename] = _sha256(payload)
            wrote_any = True
        entries.append(
            {
                "skin": theme.skin_name,
                "theme": theme.short_name,
                "filename": theme.filename,
                "path": str(destination),
                "state": state,
                "status": status,
                "before_sha256": _sha256(current) if present else "",
                "after_sha256": _sha256(payload),
                "palette_changes": _palette_changes(current, payload) if present else [],
            }
        )
    if not dry_run and wrote_any:
        destination_dir.mkdir(parents=True, exist_ok=True)
        _reject_symlink_path(manifest)
        _atomic_write_bytes(manifest, _manifest_document(updated))
    return {
        "status": _aggregate_status([str(entry["status"]) for entry in entries], _REPAIR_STATUS_ORDER),
        "path": str(destination_dir),
        "dry_run": dry_run,
        "skins": entries,
    }


def installed_skin_report(hermes_home: Path) -> list[dict[str, str]]:
    """Read-only per-theme install state, for `omh theme list` and `status`."""
    destination_dir = hermes_home / "skins"
    records = _read_manifest_records(destination_dir / MANIFEST_FILENAME)
    report: list[dict[str, str]] = []
    for theme in SKIN_THEMES:
        destination = destination_dir / theme.filename
        if not destination.is_file():
            state = "missing"
        elif _is_managed_file(destination, records, skin_payload(theme.skin_name)):
            state = "managed"
        else:
            state = "unmanaged"
        report.append(
            {
                "skin": theme.skin_name,
                "theme": theme.short_name,
                "filename": theme.filename,
                "path": str(destination),
                "state": state,
            }
        )
    return report
