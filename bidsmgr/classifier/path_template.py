"""User-supplied source-path template: subject / session from folder layout.

Some DICOM sources arrive partially pre-sorted, e.g.
``<subject>/<session>/<sequence>/*.dcm``, where the folder names are not
BIDS-prefixed and file headers (PatientID, SeriesDescription) may be
unreliable or inconsistently anonymised. A :class:`PathTemplate` lets the
user say "subject lives here, session lives there" as a single regex with
named groups, matched against the relative *source folder* path (not the
filename — DICOM filenames often embed digit sequences like ``IM_0001.dcm``
that could spuriously match a loosely-anchored pattern).

When the template matches, path-derived subject/session always take
priority over header-derived values (see ``inventory/mri_dicom.py`` and
``inventory/eeg_meg.py``) — fields the template doesn't cover (no
``subject``/``session`` group, or the group didn't match this row) still
fall back to the existing header-based detection.

Pure data + matcher, Qt-free — same shape convention as
:mod:`bidsmgr.classifier.user_rules`: :func:`from_json`/:func:`to_json` are
the single shared (de)serialiser the GUI and CLI both use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

NAMED_GROUPS: tuple[str, ...] = ("subject", "session")


@dataclass(frozen=True)
class PathTemplate:
    """A single regex matched against a relative source folder path."""

    pattern: str


def validate_path_template(pattern: str) -> Optional[str]:
    """Return an error string if ``pattern`` is unusable, else ``None``.

    A usable pattern compiles and defines at least one of the recognised
    named groups (``subject``, ``session``); any other named group is
    rejected so a typo (e.g. ``subj``) fails loudly at config time instead
    of silently matching nothing at scan time.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return "Pattern is empty."
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return str(exc)
    names = set(compiled.groupindex)
    if not names & set(NAMED_GROUPS):
        return (
            "Pattern must contain at least one named group: "
            "(?P<subject>...) or (?P<session>...)."
        )
    unknown = names - set(NAMED_GROUPS)
    if unknown:
        return (
            f"Unknown named group(s): {', '.join(sorted(unknown))}. "
            f"Only 'subject' and 'session' are recognised."
        )
    return None


def match_path_template(
    template: Optional[PathTemplate], rel_dir: str
) -> tuple[Optional[str], Optional[str]]:
    """Match ``template`` against ``rel_dir`` and return ``(subject, session)``.

    ``rel_dir`` should be a relative *folder* path (posix or native
    separators are both fine — matched as given, so a pattern anchoring on
    ``/`` should be written against a posix-normalised caller-side string).
    Returns ``(None, None)`` when there is no template, no match, or the
    pattern fails to compile (a bad regex never aborts a scan; it simply
    contributes nothing).
    """
    if template is None or not template.pattern:
        return None, None
    try:
        m = re.search(template.pattern, rel_dir)
    except re.error:
        return None, None
    if not m:
        return None, None
    gd = m.groupdict()
    return gd.get("subject"), gd.get("session")


# ---------------------------------------------------------------------------
# JSON (de)serialisation - the single shared form used by the GUI + CLI
# ---------------------------------------------------------------------------


def to_json(template: Optional[PathTemplate]) -> dict:
    return {"path_template": template.pattern if template else ""}


def from_json(obj: Optional[dict]) -> Optional[PathTemplate]:
    """Parse ``{"path_template": "<pattern>"}`` into a :class:`PathTemplate`.

    Tolerant of a missing key or blank value (returns ``None``); never
    raises on shape, so a hand-edited rules file degrades gracefully.
    """
    obj = obj or {}
    pattern = str(obj.get("path_template", "") or "").strip()
    return PathTemplate(pattern=pattern) if pattern else None


__all__ = [
    "NAMED_GROUPS",
    "PathTemplate",
    "validate_path_template",
    "match_path_template",
    "to_json",
    "from_json",
]
