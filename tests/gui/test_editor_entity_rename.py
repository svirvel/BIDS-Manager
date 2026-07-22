"""GUI tests for the Editor's Entities panel (post-conversion rename).

Modeled on ``tests/gui/test_editor_tree.py``'s refresh-assertion patterns:
build a tiny BIDS-shaped tree, drive the real widgets, assert the tree +
disk end up in the expected state.

Selecting a ``.nii.gz`` file routes through ``NiftiViewerPane``, which
loads it on a background ``QThread`` (see ``test_nifti_viewer.py``). The
fixture's file isn't valid NIfTI data, so the load fails fast — but the
thread still needs to actually finish before the test tears down the
panel, or it corrupts state for whatever GUI test runs next in the same
process. Every selection is wrapped in
``qtbot.waitSignal(panel._nifti_viewer.load_failed, ...)`` for this reason
(bare ``processEvents()`` is not enough — that only pumps the *event
queue*, not the worker thread's own run-to-completion).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QMessageBox

from bidsmgr.gui.editor_panel import EditorPanel

pytestmark = pytest.mark.gui


@pytest.fixture
def bids_root(tmp_path: Path) -> Path:
    root = tmp_path / "Study"
    anat = root / "sub-01" / "anat"
    anat.mkdir(parents=True)
    (anat / "sub-01_T1w.nii.gz").write_bytes(b"IMG")
    (anat / "sub-01_T1w.json").write_text("{}")
    return root


@pytest.fixture
def auto_confirm(monkeypatch) -> None:
    """Every QMessageBox.question() in these tests answers Yes."""
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )


def _select_and_wait(panel: EditorPanel, path: Path, qtbot) -> None:
    """Select ``path`` in the tree and wait for its (failing) NIfTI load
    to actually finish, so no QThread is left running at test teardown."""
    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel.select_file_in_tree(path)


def test_selecting_file_populates_entities_panel(qapp, qtbot, bids_root: Path) -> None:
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)
    _select_and_wait(panel, bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz", qtbot)

    assert panel._entity_editor.current_file() is not None
    assert panel._entity_editor._datatype_combo.currentText() == "anat"
    assert panel._entity_editor._suffix_combo.currentText() == "T1w"


def test_apply_rename_moves_family_and_refreshes_tree(
    qapp, qtbot, bids_root: Path, auto_confirm,
) -> None:
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)
    old_img = bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz"
    old_json = bids_root / "sub-01" / "anat" / "sub-01_T1w.json"
    _select_and_wait(panel, old_img, qtbot)

    idx = panel._entity_editor._suffix_combo.findText("T2w")
    panel._entity_editor._suffix_combo.setCurrentIndex(idx)
    assert panel._entity_editor._apply_btn.isEnabled()

    new_img = bids_root / "sub-01" / "anat" / "sub-01_T2w.nii.gz"
    new_json = bids_root / "sub-01" / "anat" / "sub-01_T2w.json"
    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._entity_editor._on_apply_clicked()

    assert new_img.exists() and new_json.exists()
    assert not old_img.exists() and not old_json.exists()

    # The Editor followed the rename: tree selection + entity panel + the
    # validate-target helper all point at the new path.
    assert panel._entity_editor.current_file() == new_img
    assert panel._tree_pane_current_path() == new_img


def test_main_toolbar_undo_reverses_rename(qapp, qtbot, bids_root: Path, auto_confirm) -> None:
    """The Editor's main toolbar Undo (not a local button on the entities
    panel — there isn't one) reverses a rename."""
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)
    old_img = bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz"
    _select_and_wait(panel, old_img, qtbot)
    assert not hasattr(panel._entity_editor, "_undo_btn")

    idx = panel._entity_editor._suffix_combo.findText("T2w")
    panel._entity_editor._suffix_combo.setCurrentIndex(idx)
    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._entity_editor._on_apply_clicked()
    assert not old_img.exists()
    assert panel._active_history_pane() is panel._entity_editor
    assert panel._undo_btn.isEnabled()

    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._on_editor_undo()

    assert old_img.exists()
    assert not (bids_root / "sub-01" / "anat" / "sub-01_T2w.nii.gz").exists()
    assert panel._entity_editor.current_file() == old_img
    assert not panel._undo_btn.isEnabled()
    assert panel._redo_btn.isEnabled()


def test_main_toolbar_redo_reapplies_rename(qapp, qtbot, bids_root: Path, auto_confirm) -> None:
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)
    old_img = bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz"
    new_img = bids_root / "sub-01" / "anat" / "sub-01_T2w.nii.gz"
    _select_and_wait(panel, old_img, qtbot)

    idx = panel._entity_editor._suffix_combo.findText("T2w")
    panel._entity_editor._suffix_combo.setCurrentIndex(idx)
    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._entity_editor._on_apply_clicked()
    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._on_editor_undo()
    assert old_img.exists() and not new_img.exists()

    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._on_editor_redo()
    assert new_img.exists() and not old_img.exists()


def test_rename_history_takes_priority_over_sidecar_history(
    qapp, qtbot, bids_root: Path, auto_confirm,
) -> None:
    """A rename's undo history persists across file selections (it's a
    session-wide action log, not per-file pane state). If the user then
    edits a *different* file's JSON sidecar, both panes have live undo
    history at once — the toolbar must still prefer the rename, since
    that's the more recent, already-on-disk action."""
    other_json = bids_root / "sub-01" / "anat" / "sub-01_other.json"
    other_json.write_text('{"a": 1}')
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)

    old_img = bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz"
    _select_and_wait(panel, old_img, qtbot)
    idx = panel._entity_editor._suffix_combo.findText("T2w")
    panel._entity_editor._suffix_combo.setCurrentIndex(idx)
    with qtbot.waitSignal(panel._nifti_viewer.load_failed, timeout=2000):
        panel._entity_editor._on_apply_clicked()
    assert panel._entity_editor.can_undo()

    # Navigate to an unrelated JSON file and make an in-memory edit — the
    # sidecar pane now ALSO has live undo history.
    panel.select_file_in_tree(other_json)
    qapp.processEvents()
    panel._sidecar_form._json_cache["a"] = 2
    panel._sidecar_form._after_edit()
    assert panel._sidecar_form.can_undo()

    # The rename still wins.
    assert panel._active_history_pane() is panel._entity_editor


def test_apply_disabled_until_something_changes(qapp, qtbot, bids_root: Path) -> None:
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)
    _select_and_wait(panel, bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz", qtbot)
    assert not panel._entity_editor._apply_btn.isEnabled()


def test_selecting_folder_clears_entities_panel(qapp, qtbot, bids_root: Path) -> None:
    panel = EditorPanel()
    qtbot.addWidget(panel)
    panel._set_root(bids_root, persist=False)
    _select_and_wait(panel, bids_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz", qtbot)
    assert panel._entity_editor.current_file() is not None

    panel._on_file_selected(bids_root / "sub-01" / "anat")
    assert panel._entity_editor.current_file() is None
