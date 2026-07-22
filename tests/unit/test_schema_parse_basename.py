"""Tests for ``schema.parse_basename`` — the reverse of ``build_basename``.

Also covers that ``validate_basename`` still behaves identically after
being refactored to share the token-parsing logic with ``parse_basename``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bidsmgr import schema


def test_round_trip_simple() -> None:
    p = Path("/data/sub-01/anat/sub-01_T1w.nii.gz")
    parsed = schema.parse_basename(p)
    assert parsed.datatype == "anat"
    assert parsed.suffix == "T1w"
    assert parsed.extension == ".nii.gz"
    assert parsed.entities == {"subject": "01"}
    assert parsed.subject == "01"
    assert parsed.session == ""
    assert parsed.warnings == []

    name = schema.build_basename(
        parsed.entities, parsed.datatype, parsed.suffix, parsed.extension,
    )
    assert name == p.name


def test_round_trip_with_session_and_run() -> None:
    p = Path("/data/sub-01/ses-pre/anat/sub-01_ses-pre_run-1_T1w.nii.gz")
    parsed = schema.parse_basename(p)
    assert parsed.entities == {"subject": "01", "session": "pre", "run": "1"}
    assert parsed.subject == "01"
    assert parsed.session == "pre"

    name = schema.build_basename(
        parsed.entities, parsed.datatype, parsed.suffix, parsed.extension,
    )
    assert name == p.name


def test_double_extension_tsv_gz() -> None:
    p = Path("/data/sub-01/beh/sub-01_task-rest_physio.tsv.gz")
    parsed = schema.parse_basename(p)
    assert parsed.extension == ".tsv.gz"
    assert parsed.suffix == "physio"
    assert parsed.entities == {"subject": "01", "task": "rest"}


def test_unrecognised_datatype_folder_is_blank() -> None:
    p = Path("/data/sub-01/not_a_datatype/sub-01_T1w.nii.gz")
    parsed = schema.parse_basename(p)
    assert parsed.datatype == ""
    # entity/suffix parsing is unaffected by an unresolvable datatype.
    assert parsed.suffix == "T1w"


def test_malformed_token_becomes_warning_not_exception() -> None:
    p = Path("/data/sub-01/anat/sub-01_bad token_T1w.nii.gz")
    parsed = schema.parse_basename(p)
    assert len(parsed.warnings) == 1
    assert "bad token" in parsed.warnings[0]
    # The well-formed subject token still parses.
    assert parsed.entities == {"subject": "01"}


def test_unknown_entity_short_form_becomes_warning() -> None:
    p = Path("/data/sub-01/anat/sub-01_frob-x_T1w.nii.gz")
    parsed = schema.parse_basename(p)
    assert any("frob" in w for w in parsed.warnings)
    assert "frob" not in parsed.entities


def test_no_suffix_component() -> None:
    p = Path("/data/sub-01/anat/sub-01.nii.gz")
    parsed = schema.parse_basename(p)
    assert parsed.suffix == ""
    assert parsed.warnings == ["Basename has no suffix component"]


def test_unrecognised_extension_kept_whole() -> None:
    p = Path("/data/sub-01/anat/sub-01_T1w.xyz")
    parsed = schema.parse_basename(p)
    assert parsed.extension == ""
    # Without a recognised extension, "T1w.xyz" is treated as the suffix.
    assert parsed.suffix == "T1w.xyz"


# ---------------------------------------------------------------------------
# validate_basename parity after the parse_basename refactor
# ---------------------------------------------------------------------------


def test_validate_basename_still_round_trips() -> None:
    name = schema.build_basename(
        {"subject": "001", "task": "rest"}, "func", "bold", ".nii.gz",
    )
    assert schema.validate_basename(name, "func") == []


def test_validate_basename_malformed_token_rule_id_preserved() -> None:
    verdicts = schema.validate_basename("sub-01_bad token_T1w.nii.gz", "anat")
    rule_ids = {v.rule_id for v in verdicts}
    assert "basename.malformed_token" in rule_ids


def test_validate_basename_unknown_entity_rule_id_preserved() -> None:
    verdicts = schema.validate_basename("sub-01_frob-x_T1w.nii.gz", "anat")
    rule_ids = {v.rule_id for v in verdicts}
    assert "entity.unknown" in rule_ids


def test_validate_basename_no_suffix_rule_id_preserved() -> None:
    verdicts = schema.validate_basename("sub-01.nii.gz", "anat")
    assert len(verdicts) == 1
    assert verdicts[0].rule_id == "basename.no_suffix"
