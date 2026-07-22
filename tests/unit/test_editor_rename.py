"""Tests for ``bidsmgr.editor.rename`` — the post-conversion file-rename
engine that powers the Editor's ``EntityEditorPane``.

Pure filesystem logic, no Qt.
"""

from __future__ import annotations

from pathlib import Path

from bidsmgr.editor import rename as rn


def _make_file(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# find_companions
# ---------------------------------------------------------------------------


def test_find_companions_picks_up_json_bval_bvec(tmp_path: Path) -> None:
    dwi = tmp_path / "sub-01" / "dwi"
    img = _make_file(dwi / "sub-01_dwi.nii.gz")
    js = _make_file(dwi / "sub-01_dwi.json")
    bval = _make_file(dwi / "sub-01_dwi.bval")
    bvec = _make_file(dwi / "sub-01_dwi.bvec")
    _make_file(dwi / "sub-01_run-2_dwi.nii.gz")  # unrelated stem

    companions = rn.find_companions(img)
    assert companions == sorted([js, bval, bvec])


def test_find_companions_empty_for_unrecognised_extension(tmp_path: Path) -> None:
    f = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.xyz")
    assert rn.find_companions(f) == []


def test_find_companions_empty_when_alone(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.nii.gz")
    assert rn.find_companions(img) == []


# ---------------------------------------------------------------------------
# plan_rename
# ---------------------------------------------------------------------------


def test_plan_rename_suffix_change(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.nii.gz")
    js = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.json")

    plan = rn.plan_rename(
        img, [js], bids_root=tmp_path,
        entities={"subject": "01"}, datatype="anat", suffix="T2w",
    )
    plan_map = dict(plan)
    assert plan_map[img] == tmp_path / "sub-01" / "anat" / "sub-01_T2w.nii.gz"
    assert plan_map[js] == tmp_path / "sub-01" / "anat" / "sub-01_T2w.json"


def test_plan_rename_session_change_moves_folder(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "ses-pre" / "anat" / "sub-01_ses-pre_T1w.nii.gz")
    plan = rn.plan_rename(
        img, [], bids_root=tmp_path,
        entities={"subject": "01", "session": "post"},
        datatype="anat", suffix="T1w",
    )
    _, new = plan[0]
    assert new == tmp_path / "sub-01" / "ses-post" / "anat" / "sub-01_ses-post_T1w.nii.gz"


# ---------------------------------------------------------------------------
# apply_rename / reverse_plan
# ---------------------------------------------------------------------------


def test_apply_rename_moves_family_together(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.nii.gz", b"IMG")
    js = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.json", b"{}")

    plan = rn.plan_rename(
        img, [js], bids_root=tmp_path,
        entities={"subject": "01"}, datatype="anat", suffix="T2w",
    )
    rn.apply_rename(plan, tmp_path)

    new_img = tmp_path / "sub-01" / "anat" / "sub-01_T2w.nii.gz"
    new_js = tmp_path / "sub-01" / "anat" / "sub-01_T2w.json"
    assert new_img.read_bytes() == b"IMG"
    assert new_js.read_bytes() == b"{}"
    assert not img.exists()
    assert not js.exists()


def test_apply_rename_removes_now_empty_old_dir_but_keeps_subject(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "ses-pre" / "anat" / "sub-01_ses-pre_T1w.nii.gz")
    plan = rn.plan_rename(
        img, [], bids_root=tmp_path,
        entities={"subject": "01", "session": "post"},
        datatype="anat", suffix="T1w",
    )
    rn.apply_rename(plan, tmp_path)

    assert not (tmp_path / "sub-01" / "ses-pre").exists()
    assert (tmp_path / "sub-01").exists()
    assert (tmp_path / "sub-01" / "ses-post" / "anat" / "sub-01_ses-post_T1w.nii.gz").exists()


def test_apply_rename_keeps_old_dir_if_sibling_file_remains(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_run-1_T1w.nii.gz")
    _make_file(tmp_path / "sub-01" / "anat" / "sub-01_run-2_T1w.nii.gz")

    plan = rn.plan_rename(
        img, [], bids_root=tmp_path,
        entities={"subject": "01", "run": "1"}, datatype="func", suffix="bold",
    )
    rn.apply_rename(plan, tmp_path)
    assert (tmp_path / "sub-01" / "anat").exists()  # sibling still there


def test_apply_rename_collision_backs_up_existing_destination(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.nii.gz", b"NEW")
    existing = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T2w.nii.gz", b"OLD")

    plan = [(img, existing)]
    rn.apply_rename(plan, tmp_path)

    assert existing.read_bytes() == b"NEW"
    backups = list((tmp_path / ".bidsmgr" / "backup").rglob("sub-01_T2w.nii.gz"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"OLD"


def test_apply_rename_undo_round_trip(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "ses-pre" / "anat" / "sub-01_ses-pre_T1w.nii.gz", b"IMG")
    js = _make_file(tmp_path / "sub-01" / "ses-pre" / "anat" / "sub-01_ses-pre_T1w.json", b"{}")

    plan = rn.plan_rename(
        img, [js], bids_root=tmp_path,
        entities={"subject": "01", "session": "post"},
        datatype="anat", suffix="T2w",
    )
    rn.apply_rename(plan, tmp_path)
    rn.apply_rename(rn.reverse_plan(plan), tmp_path)

    assert img.read_bytes() == b"IMG"
    assert js.read_bytes() == b"{}"
    assert not (tmp_path / "sub-01" / "ses-post").exists()


def test_apply_rename_noop_for_unchanged_pair(tmp_path: Path) -> None:
    img = _make_file(tmp_path / "sub-01" / "anat" / "sub-01_T1w.nii.gz", b"IMG")
    plan = [(img, img)]
    rn.apply_rename(plan, tmp_path)  # must not raise or corrupt the file
    assert img.read_bytes() == b"IMG"


def test_apply_rename_empty_plan_is_noop(tmp_path: Path) -> None:
    assert rn.apply_rename([], tmp_path) == []


# ---------------------------------------------------------------------------
# is_multifile_suffix
# ---------------------------------------------------------------------------


def test_is_multifile_suffix() -> None:
    assert rn.is_multifile_suffix("magnitude1")
    assert rn.is_multifile_suffix("phasediff")
    assert not rn.is_multifile_suffix("T1w")
    assert not rn.is_multifile_suffix("bold")
