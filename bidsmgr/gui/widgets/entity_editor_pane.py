"""Editor side panel — edit a converted file's BIDS entities on disk.

Always-visible (not part of the center viewer stack), so the user can look
at a file in the NIfTI viewer and fix its entities at the same time — the
whole point being "I see this was misclassified as T1w when it's clearly a
T2w image." Parses the selected file via ``schema.parse_basename``, lets
the user edit datatype / suffix / session / other BIDS entities in a
schema-driven form (dropdowns from ``schema.list_datatypes()`` /
``schema.list_suffixes()``, same refill pattern as
``gui/settings_dialog.py:_add_hint_row``), previews the resulting basename
live, and on **Apply** renames the file plus its same-stem companions via
``bidsmgr.editor.rename``.

Undo/redo here is a small, self-contained command stack (list of applied
``RenamePlan``s) — a rename is a discrete, self-reversible action, not an
edit to a mutable in-memory document, so it doesn't reuse the continuous-
document ``SnapshotHistory`` that ``SidecarFormPane``/``TsvViewerPane`` use
internally. It exposes the same duck-typed protocol as those two panes
(``can_undo``/``can_redo``/``undo``/``redo``/``history_changed``), which is
all ``EditorPanel`` needs to fold it into the **main toolbar's** Undo/Redo
buttons — see ``EditorPanel._active_history_pane``, which prefers this
pane whenever it has pending rename history, falling back to the
center-stack viewer's active pane otherwise. There is no separate
Undo/Redo control here; the toolbar is the only surface for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import schema
from ...editor import rename
from .primitives import PaneHeader


class EntityEditorPane(QWidget):
    """Schema-driven form to rename a converted file's BIDS entities."""

    # Emitted after a successful Apply/Undo/Redo with the file's new
    # location (== old location for a no-op undo-of-nothing, never emitted
    # in that case). ``EditorPanel`` refreshes the tree + re-selects +
    # re-validates on this.
    renamed = pyqtSignal(Path)
    # Mirrors SidecarFormPane/TsvViewerPane's convention so a future
    # global-toolbar integration is a drop-in if ever wanted.
    history_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pane-dark")

        self._path: Optional[Path] = None
        self._bids_root: Optional[Path] = None
        self._parsed: Optional[schema.ParsedBasename] = None
        self._entity_edits: dict[str, QLineEdit] = {}
        self._undo_stack: list[rename.RenamePlan] = []
        self._redo_stack: list[rename.RenamePlan] = []
        # Optional callback ``(Path) -> bool``: return True if the given
        # (old) path has unsaved edits elsewhere in the Editor, so Apply
        # can prompt before proceeding. Wired by ``EditorPanel``; best-
        # effort, not a full transactional guarantee.
        self._dirty_check: Optional[Callable[[Path], bool]] = None

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(PaneHeader("Entities"))

        self._empty_hint = QLabel("Select a file to edit its BIDS entities.")
        self._empty_hint.setObjectName("pane-hint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        v.addWidget(self._empty_hint)

        self._body = QWidget()
        body_v = QVBoxLayout(self._body)
        body_v.setContentsMargins(14, 8, 14, 8)
        body_v.setSpacing(8)

        self._multifile_warning = QLabel("")
        self._multifile_warning.setObjectName("entity-editor-warning")
        self._multifile_warning.setWordWrap(True)
        self._multifile_warning.setVisible(False)
        body_v.addWidget(self._multifile_warning)

        form = QFormLayout()
        self._subject_label = QLabel("")
        form.addRow("Subject:", self._subject_label)

        self._datatype_combo = QComboBox()
        self._datatype_combo.addItems(sorted(schema.list_datatypes()))
        self._datatype_combo.currentTextChanged.connect(self._on_datatype_changed)
        form.addRow("Datatype:", self._datatype_combo)

        self._suffix_combo = QComboBox()
        self._suffix_combo.currentTextChanged.connect(self._update_preview)
        form.addRow("Suffix:", self._suffix_combo)

        self._session_edit = QLineEdit()
        self._session_edit.setPlaceholderText("(none)")
        self._session_edit.textChanged.connect(self._update_preview)
        form.addRow("Session:", self._session_edit)
        body_v.addLayout(form)

        self._entity_form = QFormLayout()
        body_v.addLayout(self._entity_form)

        self._preview_label = QLabel("")
        self._preview_label.setWordWrap(True)
        self._preview_label.setObjectName("entity-editor-preview")
        body_v.addWidget(self._preview_label)

        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton("Apply rename")
        self._apply_btn.setObjectName("tb-btn")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        btn_row.addWidget(self._apply_btn)
        btn_row.addStretch(1)
        # Undo/redo for renames lives in EditorPanel's main toolbar (see
        # module docstring) — no local buttons here.
        body_v.addLayout(btn_row)

        body_v.addStretch(1)
        v.addWidget(self._body, 1)
        self._body.setVisible(False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_dirty_check(self, check: Optional[Callable[[Path], bool]]) -> None:
        self._dirty_check = check

    def current_file(self) -> Optional[Path]:
        return self._path

    def set_file(self, path: Optional[Path], root: Optional[Path]) -> None:
        self._path = path
        self._bids_root = root

        if path is None or root is None or not path.is_file():
            self._parsed = None
            self._empty_hint.setVisible(True)
            self._body.setVisible(False)
            return

        self._empty_hint.setVisible(False)
        self._body.setVisible(True)

        self._parsed = schema.parse_basename(path)
        p = self._parsed

        self._multifile_warning.setVisible(rename.is_multifile_suffix(p.suffix))
        if rename.is_multifile_suffix(p.suffix):
            self._multifile_warning.setText(
                f"'{p.suffix}' is part of a related multi-file group "
                f"(e.g. magnitude1/magnitude2/phasediff). Renaming here "
                f"only affects this file — check siblings manually."
            )

        self._subject_label.setText(p.subject or "(unknown)")

        self._datatype_combo.blockSignals(True)
        idx = self._datatype_combo.findText(p.datatype)
        self._datatype_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._datatype_combo.blockSignals(False)

        self._refill_suffixes(self._datatype_combo.currentText(), select=p.suffix)
        self._session_edit.blockSignals(True)
        self._session_edit.setText(p.session)
        self._session_edit.blockSignals(False)

        self._rebuild_entity_fields(self._datatype_combo.currentText(), p.suffix)
        self._update_preview()

    def undo(self) -> None:
        if not self._undo_stack or self._bids_root is None:
            return
        plan = self._undo_stack.pop()
        rename.apply_rename(rename.reverse_plan(plan), self._bids_root)
        self._redo_stack.append(plan)
        new_path = plan[0][0]  # the originally-selected file's old (== restored) path
        self.set_file(new_path, self._bids_root)
        self.history_changed.emit()
        self.renamed.emit(new_path)

    def redo(self) -> None:
        if not self._redo_stack or self._bids_root is None:
            return
        plan = self._redo_stack.pop()
        rename.apply_rename(plan, self._bids_root)
        self._undo_stack.append(plan)
        new_path = plan[0][1]
        self.set_file(new_path, self._bids_root)
        self.history_changed.emit()
        self.renamed.emit(new_path)

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refill_suffixes(self, datatype: str, *, select: str = "") -> None:
        self._suffix_combo.blockSignals(True)
        self._suffix_combo.clear()
        try:
            suffixes = sorted(schema.list_suffixes(datatype))
        except Exception:
            suffixes = []
        self._suffix_combo.addItems(suffixes)
        idx = self._suffix_combo.findText(select) if select else -1
        self._suffix_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._suffix_combo.blockSignals(False)

    def _on_datatype_changed(self, datatype: str) -> None:
        self._refill_suffixes(datatype)
        self._rebuild_entity_fields(datatype, self._suffix_combo.currentText())
        self._update_preview()

    def _rebuild_entity_fields(self, datatype: str, suffix: str) -> None:
        while self._entity_form.rowCount():
            self._entity_form.removeRow(0)
        self._entity_edits.clear()

        try:
            allowed = schema.allowed_entities(datatype, suffix)
        except Exception:
            allowed = []

        prior = self._parsed.entities if self._parsed else {}
        for entity in allowed:
            if entity in ("subject", "session"):
                continue  # shown via their own dedicated fields above
            edit = QLineEdit()
            edit.setText(prior.get(entity, ""))
            edit.textChanged.connect(self._update_preview)
            short = schema.entity_info(entity).name
            self._entity_form.addRow(f"{short.capitalize()}:", edit)
            self._entity_edits[entity] = edit

    def _collect_entities(self) -> dict[str, str]:
        entities: dict[str, str] = {}
        if self._parsed is not None and self._parsed.subject:
            entities["subject"] = self._parsed.subject
        session = self._session_edit.text().strip()
        if session:
            entities["session"] = session
        for entity, edit in self._entity_edits.items():
            value = edit.text().strip()
            if value:
                entities[entity] = value
        return entities

    def _update_preview(self, *_args) -> None:
        if self._parsed is None:
            return
        datatype = self._datatype_combo.currentText()
        suffix = self._suffix_combo.currentText()
        entities = self._collect_entities()

        try:
            basename = schema.build_basename(entities, datatype, suffix, self._parsed.extension)
        except (ValueError, KeyError) as exc:
            self._preview_label.setStyleSheet("color: #e5534b;")
            self._preview_label.setText(f"Cannot build filename: {exc}")
            self._apply_btn.setEnabled(False)
            return

        verdicts = schema.validate_entity_set(entities, datatype, suffix)
        errors = [v for v in verdicts if v.severity is schema.Severity.ERROR]
        if errors:
            self._preview_label.setStyleSheet("color: #d29922;")
            issues = "; ".join(v.message for v in errors)
            self._preview_label.setText(f"{basename}\n⚠ {issues}")
        else:
            self._preview_label.setStyleSheet("color: #3fb950;")
            self._preview_label.setText(basename)

        changed = (
            datatype != self._parsed.datatype
            or suffix != self._parsed.suffix
            or entities != self._parsed.entities
        )
        self._apply_btn.setEnabled(changed and bool(self._parsed.subject))

    def _on_apply_clicked(self) -> None:
        if self._path is None or self._bids_root is None or self._parsed is None:
            return

        if self._dirty_check is not None and self._dirty_check(self._path):
            proceed = QMessageBox.question(
                self, "Unsaved changes",
                "This file has unsaved edits elsewhere in the Editor. "
                "Renaming now will leave them unsaved against the old "
                "path. Continue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if proceed != QMessageBox.StandardButton.Yes:
                return

        datatype = self._datatype_combo.currentText()
        suffix = self._suffix_combo.currentText()
        entities = self._collect_entities()

        companions = rename.find_companions(self._path)
        plan = rename.plan_rename(
            self._path, companions, bids_root=self._bids_root,
            entities=entities, datatype=datatype, suffix=suffix,
        )

        lines = "\n".join(
            f"{old.relative_to(self._bids_root)}  →  {new.relative_to(self._bids_root)}"
            for old, new in plan
        )
        confirm = QMessageBox.question(
            self, "Confirm rename",
            f"This will move {len(plan)} file(s):\n\n{lines}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        rename.apply_rename(plan, self._bids_root)
        self._undo_stack.append(plan)
        self._redo_stack.clear()
        new_path = plan[0][1]
        self.set_file(new_path, self._bids_root)
        self.history_changed.emit()
        self.renamed.emit(new_path)


__all__ = ["EntityEditorPane"]
