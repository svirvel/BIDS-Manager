"""Rename a converted BIDS file (and its companions) on disk.

Pure logic, Qt-free — sibling to ``editor/types.py`` (the validator's
pure-data types), same "no GUI dependency" convention. Used by the Editor's
``EntityEditorPane`` (``bidsmgr/gui/widgets/entity_editor_pane.py``) to let
a user fix a misclassified file (wrong suffix, wrong session, ...) after
conversion, without re-running the whole convert step.

A "rename" here means: the user edited a file's BIDS entities, and every
file that shares its basename *stem* (the image + its ``.json`` sidecar +
``.bval``/``.bvec`` etc — see :func:`find_companions`) moves to the new
basename/location together, computed via ``schema.build_relative_path``
(handles a session/datatype change moving the file into a different
folder).

Known limitation: only same-stem companions are discovered. Multi-file
*suffix* families — fmap ``magnitude1``/``magnitude2``/``phasediff``
triplets, multi-echo ``echo-1``/``echo-2``/... groups — have DIFFERENT
basenames and are not renamed automatically; no schema-driven "sibling
suffix" table exists anywhere in this codebase to discover them safely.
:func:`is_multifile_suffix` lets a caller warn the user instead.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Optional

from .. import schema

RenamePlan = list[tuple[Path, Path]]

# Well-known BIDS suffixes that are always part of a related, same-series
# multi-file group. Not schema-derived (no such table exists) — a small,
# hand-maintained set covering the common fmap case, used only to warn
# the user that siblings won't move automatically, never to block a rename.
MULTIFILE_SUFFIXES: frozenset[str] = frozenset({
    "magnitude", "magnitude1", "magnitude2",
    "phase1", "phase2", "phasediff", "fieldmap",
})


def is_multifile_suffix(suffix: str) -> bool:
    return suffix in MULTIFILE_SUFFIXES


def _split_known_extension(name: str) -> tuple[str, str]:
    """Return ``(stem, extension)`` for a recognised extension, or
    ``(name, "")`` if none of ``schema.KNOWN_EXTENSIONS`` match."""
    for ext in schema.KNOWN_EXTENSIONS:
        if name.endswith(ext):
            return name[: -len(ext)], ext
    return name, ""


def _stem(path: Path) -> Optional[str]:
    """The basename stem for companion matching, or ``None`` if ``path``
    doesn't carry a recognised extension (never matches anything)."""
    stem, ext = _split_known_extension(path.name)
    return stem if ext else None


def find_companions(path: Path) -> list[Path]:
    """Return every sibling file sharing ``path``'s basename stem, sorted,
    excluding ``path`` itself. Empty if ``path``'s extension isn't
    recognised or its parent directory doesn't exist."""
    stem = _stem(path)
    if stem is None:
        return []
    parent = path.parent
    if not parent.is_dir():
        return []
    companions = []
    for candidate in parent.iterdir():
        if candidate == path or not candidate.is_file():
            continue
        if _stem(candidate) == stem:
            companions.append(candidate)
    return sorted(companions)


def plan_rename(
    path: Path,
    companions: list[Path],
    *,
    bids_root: Path,
    entities: dict[str, str],
    datatype: str,
    suffix: str,
) -> RenamePlan:
    """Compute ``(old, new)`` path pairs for ``path`` and its
    ``companions`` under the new entities/datatype/suffix. Each file keeps
    its own extension; only the basename + folder location change.
    """
    plan: RenamePlan = []
    for old in (path, *companions):
        _, extension = _split_known_extension(old.name)
        new = bids_root / schema.build_relative_path(entities, datatype, suffix, extension)
        plan.append((old, new))
    return plan


def reverse_plan(plan: RenamePlan) -> RenamePlan:
    """Swap every ``(old, new)`` pair — feed to :func:`apply_rename` to
    undo a previously-applied plan."""
    return [(new, old) for old, new in plan]


def apply_rename(plan: RenamePlan, bids_root: Path) -> RenamePlan:
    """Execute every ``(old, new)`` move in ``plan``.

    If a destination already exists, it's backed up first — same pattern
    as ``cli/convert.py:_merge_commit`` — to
    ``<bids_root>/.bidsmgr/backup/rename_<utc-stamp>/<relpath>`` rather
    than being clobbered. After the moves, any old parent directory left
    empty (a ``ses-*``/datatype folder the file just moved out of) is
    removed, stopping at the subject folder — a subject directory is never
    removed even if it ends up empty.

    Returns ``plan`` unchanged, so the caller can pass
    ``reverse_plan(plan)`` back into this same function to undo.
    """
    if not plan:
        return plan

    backup_root = bids_root / ".bidsmgr" / "backup" / f"rename_{_utc_stamp()}"
    for old, new in plan:
        if old == new:
            continue
        if new.exists():
            rel = new.relative_to(bids_root)
            backup_dst = backup_root / rel
            backup_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(new), str(backup_dst))
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))

    for old, new in plan:
        if old != new:
            _remove_if_empty_up_to_subject(old.parent, bids_root)

    return plan


def _remove_if_empty_up_to_subject(folder: Path, bids_root: Path) -> None:
    try:
        depth = len(folder.relative_to(bids_root).parts)
    except ValueError:
        return
    # depth == 1 means ``folder`` IS the subject directory — never remove it.
    while depth > 1 and folder.exists() and not any(folder.iterdir()):
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent
        depth -= 1


def _utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


__all__ = [
    "RenamePlan",
    "MULTIFILE_SUFFIXES",
    "is_multifile_suffix",
    "find_companions",
    "plan_rename",
    "reverse_plan",
    "apply_rename",
]
