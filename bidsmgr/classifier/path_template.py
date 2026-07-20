"""User-supplied source-path template: BIDS entities from folder layout.

Some DICOM/EEG sources arrive partially pre-sorted, e.g.
``<subject>/<datatype>/<session>/<run>/*.dcm``, where the folder names are
not BIDS-prefixed and file headers (PatientID, SeriesDescription) may be
unreliable or inconsistently anonymised. A :class:`PathTemplate` lets the
user say "subject lives here, session there, datatype there, run there" as
a single regex with named groups, matched against the relative *source
folder* path (not the filename — DICOM filenames often embed digit
sequences like ``IM_0001.dcm`` that could spuriously match a
loosely-anchored pattern).

Recognised named groups:

* ``subject`` / ``session`` — identity, consumed by
  ``inventory/mri_dicom.py`` and ``inventory/eeg_meg.py`` at scan time.
* ``datatype`` / ``suffix`` — classification fields (``anat``/``func``/...,
  ``T1w``/``bold``/...), consumed by ``cli/scan.py`` (MRI only — EEG/MEG
  datatype is reliably auto-detected from the file format itself).
* Any BIDS entity **short code** as it appears in a filename (``task``,
  ``run``, ``acq``, ``ce``, ``rec``, ``dir``, ``echo``, ``part``, ...) —
  the full, schema-driven list, not a hardcoded guess (see
  :func:`allowed_entity_short_codes`).

When the template matches, path-derived values always take priority over
header/classifier-derived values for whichever fields the pattern covers —
fields it doesn't cover still fall back to the existing detection. An
invalid resulting combination (e.g. path says ``anat`` but the suffix is
``bold``) is not silently dropped: it surfaces as a ``proposed_issues``
warning through the existing schema-validation machinery, same as any
other schema violation.

Pure data + matcher, Qt-free — same shape convention as
:mod:`bidsmgr.classifier.user_rules`: :func:`from_json`/:func:`to_json` are
the single shared (de)serialiser the GUI and CLI both use. Reuses the
schema engine for the entity/datatype vocabulary rather than hand-curating
a list (``classifier/dcm2niix_bidsguess.py`` already imports ``schema``
from this layer, so this is an established pattern).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .. import schema
from ..util.label import sanitize_bids_label

# Pseudo-fields: not BIDS entities, but recognised named groups with their
# own handling (identity at scan time; classification fields in cli/scan.py).
IDENTITY_GROUPS: tuple[str, ...] = ("subject", "session")
CLASSIFICATION_GROUPS: tuple[str, ...] = ("datatype", "suffix")


@dataclass(frozen=True)
class PathTemplate:
    """A single regex matched against a relative source folder path."""

    pattern: str


@lru_cache(maxsize=1)
def _entity_short_to_long() -> dict[str, str]:
    """Map each BIDS entity's filename short code (``acq``) to its schema
    long name (``acquisition``) — the key shape ``candidate_entities``/
    ``schema.build_basename`` expect. Users write short codes because
    that's what they see in actual BIDS filenames."""
    return {schema.entity_info(e).name: e for e in schema.entity_order()}


@lru_cache(maxsize=1)
def allowed_entity_short_codes() -> frozenset[str]:
    """Every BIDS entity short code the installed schema recognises, other
    than ``sub``/``ses`` — those are superseded by the dedicated
    ``subject``/``session`` identity groups (see :data:`IDENTITY_GROUPS`),
    which get scan-time clustering/session handling that a bare entity
    group would silently skip."""
    return frozenset(_entity_short_to_long()) - {"sub", "ses"}


@lru_cache(maxsize=1)
def _allowed_group_names() -> frozenset[str]:
    return frozenset(IDENTITY_GROUPS) | frozenset(CLASSIFICATION_GROUPS) | allowed_entity_short_codes()


def validate_path_template(pattern: str) -> Optional[str]:
    """Return an error string if ``pattern`` is unusable, else ``None``.

    A usable pattern compiles and defines at least one named group, all of
    them recognised (``subject``, ``session``, ``datatype``, ``suffix``, or
    a BIDS entity short code); an unrecognised group name fails loudly at
    config time instead of silently matching nothing at scan time.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return "Pattern is empty."
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return str(exc)
    names = set(compiled.groupindex)
    if not names:
        return (
            "Pattern must contain at least one named group, e.g. "
            "(?P<subject>...), (?P<session>...), (?P<datatype>...), "
            "(?P<task>...), (?P<run>...)."
        )
    unknown = names - _allowed_group_names()
    if unknown:
        return (
            f"Unknown named group(s): {', '.join(sorted(unknown))}. "
            f"Recognised: subject, session, datatype, suffix, and BIDS "
            f"entity short codes ({', '.join(sorted(allowed_entity_short_codes()))})."
        )
    return None


def match_path_template(
    template: Optional[PathTemplate], rel_dir: str
) -> dict[str, str]:
    """Match ``template`` against ``rel_dir``; return every matched,
    non-empty named group, keyed by the group name as written in the
    pattern (``subject``, ``datatype``, or a BIDS entity short code like
    ``acq``).

    ``rel_dir`` should be a relative *folder* path (posix or native
    separators are both fine — matched as given, so a pattern anchoring on
    ``/`` should be written against a posix-normalised caller-side string).
    Returns ``{}`` when there is no template, no match, or the pattern
    fails to compile (a bad regex never aborts a scan; it simply
    contributes nothing).
    """
    if template is None or not template.pattern:
        return {}
    try:
        m = re.search(template.pattern, rel_dir)
    except re.error:
        return {}
    if not m:
        return {}
    return {k: v for k, v in m.groupdict().items() if v}


def inject_entities(matched: dict[str, str], entities: dict[str, str]) -> dict[str, str]:
    """Merge every matched BIDS-entity group (excluding the identity and
    classification pseudo-fields) into ``entities``, path always winning on
    key collision. Short codes (``acq``) are translated to the schema's
    long-name keys (``acquisition``) that ``candidate_entities``/
    ``schema.build_basename`` expect. Values are sanitised to BIDS-safe
    labels; downstream schema validation (already run by
    ``cli/scan._propose_basename``) catches remaining format problems
    (e.g. a non-numeric value for an index-type entity like ``run``) and
    surfaces them as a ``proposed_issues`` warning.
    """
    out = dict(entities)
    short_to_long = _entity_short_to_long()
    skip = set(IDENTITY_GROUPS) | set(CLASSIFICATION_GROUPS)
    for key, value in matched.items():
        if key in skip:
            continue
        sanitized = sanitize_bids_label(value)
        if sanitized:
            out[short_to_long.get(key, key)] = sanitized
    return out


def apply_datatype_suffix_override(
    matched: dict[str, str], *, datatype: str, suffix: str,
) -> tuple[str, str, list[str]]:
    """Override ``datatype``/``suffix`` from matched path groups when they
    name a schema-recognised value; otherwise the classifier's original
    value is kept and a note is returned (log-only — never inject a
    datatype/suffix the schema doesn't know about).

    ``suffix`` is validated against the *resolved* datatype (i.e. after a
    successful datatype override), so ``.../anat/T1w/...`` resolves
    correctly even though ``suffix`` alone can't imply a datatype.
    """
    notes: list[str] = []

    path_datatype = matched.get("datatype")
    if path_datatype:
        sanitized = sanitize_bids_label(path_datatype)
        if sanitized in schema.list_datatypes():
            datatype = sanitized
        else:
            notes.append(
                f"path_template: ignored unrecognised datatype {path_datatype!r} "
                f"from source path"
            )

    path_suffix = matched.get("suffix")
    if path_suffix:
        sanitized = sanitize_bids_label(path_suffix)
        if sanitized in schema.list_suffixes(datatype):
            suffix = sanitized
        else:
            notes.append(
                f"path_template: ignored suffix {path_suffix!r} from source "
                f"path — not valid for datatype {datatype!r}"
            )

    return datatype, suffix, notes


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
    "IDENTITY_GROUPS",
    "CLASSIFICATION_GROUPS",
    "PathTemplate",
    "allowed_entity_short_codes",
    "validate_path_template",
    "match_path_template",
    "inject_entities",
    "apply_datatype_suffix_override",
    "to_json",
    "from_json",
]
