"""Tests for the user-configurable directory-structure path template.

Covers the pure-data layer (``classifier/path_template``) and its
integration into: ``inventory/mri_dicom.py`` (path always wins over
PatientID-based clustering / SeriesDescription session tags),
``inventory/eeg_meg.py`` (path always wins over the built-in
``sub-``/``ses-`` folder convention, plus entity injection), and
``cli/scan.py`` (datatype/suffix/entity override applied to the winning
classification, MRI only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from bidsmgr.classifier import path_template as pt
from bidsmgr.cli.scan import run_scan
from bidsmgr.inventory import eeg_meg as eeg_meg_mod
from bidsmgr.inventory.eeg_meg import guess_subject_session_task, scan_eeg_meg
from bidsmgr.inventory.mri_dicom import scan_dicoms_long


# ---------------------------------------------------------------------------
# path_template: matcher + validator + JSON
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_pattern() -> None:
    assert pt.validate_path_template("") is not None


def test_validate_rejects_bad_regex() -> None:
    assert pt.validate_path_template("(") is not None


def test_validate_rejects_pattern_without_named_groups() -> None:
    assert pt.validate_path_template(r"[^/]+/[^/]+") is not None


def test_validate_rejects_unknown_named_group() -> None:
    err = pt.validate_path_template(r"(?P<subj>[^/]+)")
    assert err is not None
    assert "subj" in err


def test_validate_accepts_subject_only() -> None:
    assert pt.validate_path_template(r"(?P<subject>[^/]+)") is None


def test_validate_accepts_both_groups() -> None:
    assert pt.validate_path_template(r"(?P<subject>[^/]+)/(?P<session>[^/]+)") is None


def test_validate_accepts_datatype_and_suffix_groups() -> None:
    assert pt.validate_path_template(r"(?P<datatype>[^/]+)/(?P<suffix>[^/]+)") is None


def test_validate_accepts_entity_short_codes() -> None:
    for code in ("task", "run", "acq", "ce", "rec", "dir", "echo"):
        assert pt.validate_path_template(f"(?P<{code}>[^/]+)") is None


def test_validate_rejects_entity_long_name() -> None:
    # Long schema names (e.g. "acquisition") are internal; users write the
    # BIDS filename short code ("acq").
    err = pt.validate_path_template(r"(?P<acquisition>[^/]+)")
    assert err is not None


def test_allowed_entity_short_codes_matches_schema() -> None:
    codes = pt.allowed_entity_short_codes()
    assert {"task", "run", "acq", "ce", "rec", "dir", "echo", "desc"} <= codes
    assert "subject" not in codes and "session" not in codes  # identity, not entities
    # "sub"/"ses" are superseded by the dedicated subject/session groups —
    # writing (?P<sub>...) would silently skip identity clustering.
    assert "sub" not in codes and "ses" not in codes


def test_validate_rejects_sub_ses_short_codes() -> None:
    assert pt.validate_path_template(r"(?P<sub>[^/]+)") is not None
    assert pt.validate_path_template(r"(?P<ses>[^/]+)") is not None


def test_match_extracts_both_groups() -> None:
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)/(?P<session>[^/]+)/")
    matched = pt.match_path_template(tmpl, "101/baseline/t1_mprage")
    assert matched == {"subject": "101", "session": "baseline"}


def test_match_extracts_datatype_suffix_and_entities() -> None:
    tmpl = pt.PathTemplate(
        pattern=r"(?P<subject>[^/]+)/(?P<datatype>[^/]+)/(?P<session>[^/]+)/(?P<run>[^/]+)"
    )
    matched = pt.match_path_template(tmpl, "101/anat/baseline/2")
    assert matched == {"subject": "101", "datatype": "anat", "session": "baseline", "run": "2"}


def test_match_subject_only_omits_session_key() -> None:
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)")
    matched = pt.match_path_template(tmpl, "101/baseline")
    assert matched == {"subject": "101"}


def test_match_no_match_returns_empty_dict() -> None:
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>foo-[0-9]+)")
    assert pt.match_path_template(tmpl, "bar/baz") == {}


def test_match_with_no_template_returns_empty_dict() -> None:
    assert pt.match_path_template(None, "101/baseline") == {}


def test_match_never_raises_on_bad_regex() -> None:
    tmpl = pt.PathTemplate(pattern="(")
    assert pt.match_path_template(tmpl, "101/baseline") == {}


# ---------------------------------------------------------------------------
# inject_entities / apply_datatype_suffix_override
# ---------------------------------------------------------------------------


def test_inject_entities_translates_short_code_to_long_name() -> None:
    out = pt.inject_entities({"acq": "highres"}, {})
    assert out == {"acquisition": "highres"}


def test_inject_entities_overwrites_existing_key() -> None:
    out = pt.inject_entities({"run": "3"}, {"run": "1", "task": "rest"})
    assert out == {"run": "3", "task": "rest"}


def test_inject_entities_skips_identity_and_classification_groups() -> None:
    out = pt.inject_entities(
        {"subject": "101", "session": "pre", "datatype": "anat", "suffix": "T1w", "task": "rest"},
        {},
    )
    assert out == {"task": "rest"}


def test_inject_entities_does_not_mutate_input() -> None:
    entities = {"task": "rest"}
    pt.inject_entities({"run": "2"}, entities)
    assert entities == {"task": "rest"}


def test_apply_datatype_suffix_override_valid() -> None:
    dt, sfx, notes = pt.apply_datatype_suffix_override(
        {"datatype": "anat", "suffix": "T1w"}, datatype="func", suffix="bold",
    )
    assert (dt, sfx, notes) == ("anat", "T1w", [])


def test_apply_datatype_suffix_override_unrecognised_datatype_ignored() -> None:
    dt, sfx, notes = pt.apply_datatype_suffix_override(
        {"datatype": "not-a-real-datatype"}, datatype="func", suffix="bold",
    )
    assert dt == "func"
    assert len(notes) == 1
    assert "not-a-real-datatype" in notes[0]


def test_apply_datatype_suffix_override_suffix_invalid_for_datatype_ignored() -> None:
    dt, sfx, notes = pt.apply_datatype_suffix_override(
        {"datatype": "anat", "suffix": "bold"}, datatype="func", suffix="bold",
    )
    assert dt == "anat"
    assert sfx == "bold"  # unchanged: "bold" already equalled the input suffix
    assert len(notes) == 1
    assert "bold" in notes[0] and "anat" in notes[0]


def test_apply_datatype_suffix_override_noop_without_matches() -> None:
    dt, sfx, notes = pt.apply_datatype_suffix_override({}, datatype="func", suffix="bold")
    assert (dt, sfx, notes) == ("func", "bold", [])


def test_json_round_trip() -> None:
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)/(?P<session>[^/]+)/")
    again = pt.from_json(pt.to_json(tmpl))
    assert again == tmpl


def test_from_json_blank_is_none() -> None:
    assert pt.from_json({"path_template": ""}) is None
    assert pt.from_json(None) is None
    assert pt.from_json({}) is None


# ---------------------------------------------------------------------------
# MRI integration: path always wins over header-derived identity/session
# ---------------------------------------------------------------------------


def _write_dicom(
    path: Path,
    *,
    series_desc: str,
    series_uid: str,
    patient_id: str,
    patient_name: str = "Doe^Jane",
) -> None:
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = MRImageStorage
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=fm, preamble=b"\0" * 128)
    ds.PatientID = patient_id
    ds.PatientName = patient_name
    ds.Modality = "MR"
    ds.SeriesDescription = series_desc
    ds.SeriesInstanceUID = series_uid
    ds.StudyInstanceUID = generate_uid()
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.ImageType = ["ORIGINAL", "PRIMARY", "M", "ND"]
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.PixelData = (b"\x00\x00") * 16
    ds.save_as(str(path), enforce_file_format=True)


def test_path_template_splits_one_patient_id_into_two_subjects(tmp_path: Path) -> None:
    """Same header PatientID, different path-derived subject folders ->
    path wins -> two distinct BIDS subjects."""
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)")
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    _write_dicom(
        tmp_path / "alpha" / "a.dcm", series_desc="t1_mprage",
        series_uid="1.1", patient_id="SAMEID",
    )
    _write_dicom(
        tmp_path / "beta" / "b.dcm", series_desc="t1_mprage",
        series_uid="1.2", patient_id="SAMEID",
    )

    df = scan_dicoms_long(tmp_path, n_jobs=1, path_template=tmpl)
    assert df["BIDS_name"].nunique() == 2


def test_path_template_merges_two_patient_ids_into_one_subject(tmp_path: Path) -> None:
    """Different header PatientIDs (and names), same path-derived subject
    folder -> path wins -> one BIDS subject."""
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)")
    (tmp_path / "same_folder").mkdir()
    _write_dicom(
        tmp_path / "same_folder" / "a.dcm", series_desc="t1_mprage",
        series_uid="1.1", patient_id="ID_ONE", patient_name="Doe^Jane",
    )
    _write_dicom(
        tmp_path / "same_folder" / "b.dcm", series_desc="t2_space",
        series_uid="1.2", patient_id="ID_TWO", patient_name="Smith^John",
    )

    df = scan_dicoms_long(tmp_path, n_jobs=1, path_template=tmpl)
    assert df["BIDS_name"].nunique() == 1


def test_path_template_session_wins_over_series_description_tag(tmp_path: Path) -> None:
    """A ``ses-`` tag embedded in SeriesDescription must lose to the
    path-derived session when a template is configured."""
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)/(?P<session>[^/]+)")
    (tmp_path / "101" / "followup").mkdir(parents=True)
    _write_dicom(
        tmp_path / "101" / "followup" / "a.dcm",
        series_desc="t1_mprage ses-baseline",  # would normally win via SESSION_RE
        series_uid="1.1", patient_id="P1",
    )

    df = scan_dicoms_long(tmp_path, n_jobs=1, path_template=tmpl)
    assert list(df["session"]) == ["ses-followup"]


def test_path_template_without_session_group_falls_back_to_header(tmp_path: Path) -> None:
    """Template covers subject only -> session detection still uses the
    existing SeriesDescription ``ses-`` regex."""
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)")
    (tmp_path / "101").mkdir()
    _write_dicom(
        tmp_path / "101" / "a.dcm",
        series_desc="t1_mprage ses-baseline",
        series_uid="1.1", patient_id="P1",
    )

    df = scan_dicoms_long(tmp_path, n_jobs=1, path_template=tmpl)
    assert list(df["session"]) == ["ses-baseline"]
    # Subject override still applied even though session fell back.
    assert df["BIDS_name"].iloc[0] == "sub-001"


def test_no_template_preserves_header_driven_behaviour(tmp_path: Path) -> None:
    """Sanity check: omitting path_template keeps the original PatientID-
    clustering behaviour (two different PatientIDs and names -> two
    subjects)."""
    _write_dicom(
        tmp_path / "a.dcm", series_desc="t1_mprage",
        series_uid="1.1", patient_id="ID_ONE", patient_name="Doe^Jane",
    )
    _write_dicom(
        tmp_path / "b.dcm", series_desc="t1_mprage",
        series_uid="1.2", patient_id="ID_TWO", patient_name="Smith^John",
    )

    df = scan_dicoms_long(tmp_path, n_jobs=1)
    assert df["BIDS_name"].nunique() == 2


# ---------------------------------------------------------------------------
# MRI end-to-end: datatype/suffix/entity override via cli.scan.run_scan
# ---------------------------------------------------------------------------


def test_run_scan_applies_datatype_suffix_entity_override(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "101" / "anat" / "baseline" / "3").mkdir(parents=True)
    _write_dicom(
        src / "101" / "anat" / "baseline" / "3" / "a.dcm",
        series_desc="t1_mprage", series_uid="1.1", patient_id="P1",
    )
    tmpl = pt.PathTemplate(
        pattern=r"(?P<subject>[^/]+)/(?P<datatype>[^/]+)/(?P<session>[^/]+)/(?P<run>[^/]+)"
    )
    df = run_scan(
        src, tmp_path / "out.tsv", n_jobs=1, skip_bids_guess=True, path_template=tmpl,
    )
    row = df.iloc[0]
    assert row["proposed_datatype"] == "anat"
    assert "run-3" in row["proposed_basename"]
    entities = json.loads(row["entities"])
    assert entities["run"] == "3"


def test_run_scan_ignores_unrecognised_datatype_folder(tmp_path: Path, caplog) -> None:
    """A bogus datatype folder name is never injected — the classifier's
    original (schema-valid) datatype survives, and the skip is logged."""
    import logging

    src = tmp_path / "src"
    (src / "101" / "weird" / "baseline").mkdir(parents=True)
    _write_dicom(
        src / "101" / "weird" / "baseline" / "a.dcm",
        series_desc="t1_mprage", series_uid="1.1", patient_id="P1",
    )
    tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)/(?P<datatype>[^/]+)/(?P<session>[^/]+)")
    with caplog.at_level(logging.WARNING, logger="bidsmgr.cli.scan"):
        df = run_scan(
            src, tmp_path / "out.tsv", n_jobs=1, skip_bids_guess=True, path_template=tmpl,
        )
    row = df.iloc[0]
    assert row["proposed_datatype"] == "anat"  # classifier's original value, path ignored
    assert any("weird" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# EEG/MEG integration: path template wins over the sub-/ses- convention,
# plus entity injection
# ---------------------------------------------------------------------------


class TestPathTemplateEegMeg:
    def test_template_overrides_bids_convention(self, tmp_path: Path) -> None:
        # Folder uses the built-in sub-/ses- convention, but the template
        # targets a different, non-prefixed pair of segments -> path wins.
        path = tmp_path / "site-A" / "007" / "visit1" / "sub-999" / "ses-000" / "rec.edf"
        tmpl = pt.PathTemplate(pattern=r"site-A/(?P<subject>[^/]+)/(?P<session>[^/]+)")
        sub, ses, _task, _run = guess_subject_session_task(path, tmp_path, path_template=tmpl)
        assert sub == "007"
        assert ses == "visit1"

    def test_no_match_falls_back_to_builtin_convention(self, tmp_path: Path) -> None:
        path = tmp_path / "sub-007" / "ses-pre" / "rec.edf"
        tmpl = pt.PathTemplate(pattern=r"no-such-prefix/(?P<subject>[^/]+)")
        sub, ses, _task, _run = guess_subject_session_task(path, tmp_path, path_template=tmpl)
        assert sub == "007"
        assert ses == "pre"

    def test_no_template_is_unaffected(self, tmp_path: Path) -> None:
        path = tmp_path / "sub-007" / "ses-pre" / "rec.edf"
        sub, ses, _task, _run = guess_subject_session_task(path, tmp_path)
        assert sub == "007"
        assert ses == "pre"

    def test_scan_eeg_meg_injects_entities(self, tmp_path: Path, monkeypatch) -> None:
        """Path-derived acq/run entities win over the filename-derived
        guesses (scan_eeg_meg builds the ``entities`` TSV column)."""

        @dataclass
        class _Stub:
            source: Path
            sfreq: float = 500.0
            n_channels: int = 32
            n_times: int = 50_000
            duration_sec: float = 100.0
            recording_time: str = "2026-01-01T12:00:00"
            datatype: str = "eeg"
            has_positions: bool = False
            fmt: str = "EDF"
            manufacturer: str = ""
            model: str = ""
            subj_sex: str = ""
            subj_age: str = ""
            event_codes: tuple = ()
            montage_suggestion: str = ""

        monkeypatch.setattr(eeg_meg_mod, "_HAS_MNE", True, raising=False)
        monkeypatch.setattr(eeg_meg_mod, "_probe", lambda path: _Stub(source=path))

        (tmp_path / "101" / "handedness" / "run2").mkdir(parents=True)
        (tmp_path / "101" / "handedness" / "run2" / "rec.edf").write_bytes(b"x")

        tmpl = pt.PathTemplate(pattern=r"(?P<subject>[^/]+)/(?P<acq>[^/]+)/run(?P<run>[^/]+)")
        df = scan_eeg_meg(tmp_path, path_template=tmpl)
        row = df.iloc[0]
        entities = json.loads(row["entities"])
        assert entities["acquisition"] == "handedness"
        assert entities["run"] == "2"
