"""PDF Combine - drag PDFs in, reorder them, save one combined PDF."""

import os
import sys
import tempfile

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "PDF Combine"
ICON_PATH = os.path.join(
    getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "assets", "icon.png"
)
THUMB_SIZE = QSize(110, 140)
PAGE_GAP = 12
ROLE_PATH = Qt.UserRole
ROLE_KEY = Qt.UserRole + 1


def render_page(doc, index, target_width, dpr=1.0):
    """Render a page to a QImage that is target_width logical pixels wide."""
    page = doc[index]
    try:
        width_pt, _ = page.get_size()
        scale = max(target_width * dpr / width_pt, 0.05)
        bitmap = page.render(scale=scale, rev_byteorder=True, may_draw_forms=False)
        image = QImage(
            bitmap.buffer, bitmap.width, bitmap.height, bitmap.stride, QImage.Format_RGB888
        ).copy()  # copy: the bitmap buffer is freed when bitmap goes away
        image.setDevicePixelRatio(dpr)
        return image
    finally:
        page.close()


class FileList(QListWidget):
    """Left pane: thumbnails, accepts files dropped from Finder/Explorer, drag to reorder."""

    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setIconSize(THUMB_SIZE)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAcceptDrops(True)
        self.setSpacing(4)
        self.setWordWrap(True)
        self.setMinimumWidth(240)

    def _is_external(self, event):
        return event.mimeData().hasUrls() and event.source() is not self

    def dragEnterEvent(self, event):
        if self._is_external(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._is_external(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self._is_external(event):
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            event.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(self.palette().placeholderText().color())
            painter.drawText(
                self.viewport().rect().adjusted(16, 16, -16, -16),
                Qt.AlignCenter | Qt.TextWordWrap,
                "Drag PDF files here\nor click “Add PDFs…”",
            )


class PreviewPane(QScrollArea):
    """Right pane: scrollable preview of every page of one document, rendered lazily."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignHCenter)
        self._doc = None
        self._labels = []
        self._rendered = set()
        self._render_width = 0

        container = QWidget()
        container.setAutoFillBackground(True)
        pal = container.palette()
        pal.setColor(container.backgroundRole(), QColor(90, 90, 90))
        container.setPalette(pal)
        self._layout = QVBoxLayout(container)
        self._layout.setSpacing(PAGE_GAP)
        self._layout.setContentsMargins(PAGE_GAP, PAGE_GAP, PAGE_GAP, PAGE_GAP)
        self._layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setWidget(container)

        self._timer = QTimer(self, singleShot=True, interval=60)
        self._timer.timeout.connect(self._relayout)
        self.verticalScrollBar().valueChanged.connect(self._render_visible)

    def show_document(self, doc):
        for label in self._labels:
            label.deleteLater()
        self._labels, self._rendered = [], set()
        self._doc = doc
        self.verticalScrollBar().setValue(0)
        if doc is not None:
            for _ in range(len(doc)):
                label = QLabel()
                label.setAlignment(Qt.AlignCenter)
                label.setStyleSheet("background: white;")
                self._layout.addWidget(label)
                self._labels.append(label)
        self._render_width = 0
        self._relayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._timer.start()

    def _page_width(self):
        return max(self.viewport().width() - 2 * PAGE_GAP, 100)

    def _relayout(self):
        """Size placeholders to the page aspect ratio, then render what is on screen."""
        if self._doc is None:
            return
        width = self._page_width()
        if width != self._render_width:
            self._render_width = width
            self._rendered.clear()
            for i, label in enumerate(self._labels):
                w_pt, h_pt = self._doc.get_page_size(i)
                label.setFixedSize(width, int(width * h_pt / w_pt))
                label.clear()
        QTimer.singleShot(0, self._render_visible)

    def _render_visible(self):
        if self._doc is None:
            return
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        dpr = self.devicePixelRatioF()
        for i, label in enumerate(self._labels):
            if i in self._rendered:
                continue
            y = label.y()
            if y + label.height() >= top - 400 and y <= bottom + 400:
                label.setPixmap(QPixmap.fromImage(render_page(self._doc, i, label.width(), dpr)))
                self._rendered.add(i)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 760)
        self._docs = {}  # item key -> open pdfium document
        self._next_key = 0

        self.file_list = FileList()
        self.file_list.files_dropped.connect(self.add_files)
        self.file_list.currentItemChanged.connect(self._on_current_changed)
        self.file_list.itemSelectionChanged.connect(self._update_status)
        self.file_list.model().rowsMoved.connect(self._update_status)

        add_btn = QPushButton("Add PDFs…")
        add_btn.clicked.connect(self._browse)
        self.up_btn = QPushButton("▲ Up")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("▼ Down")
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear)

        row1 = QHBoxLayout()
        row1.addWidget(add_btn)
        row1.addWidget(self.up_btn)
        row1.addWidget(self.down_btn)
        row2 = QHBoxLayout()
        row2.addWidget(self.remove_btn)
        row2.addWidget(self.clear_btn)

        self.save_btn = QPushButton("Save Combined PDF…")
        self.save_btn.setDefault(True)
        self.save_btn.setMinimumHeight(36)
        self.save_btn.clicked.connect(self._save)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.addWidget(self.file_list, 1)
        left_layout.addLayout(row1)
        left_layout.addLayout(row2)
        left_layout.addWidget(self.save_btn)

        self.preview_title = QLabel("No file selected")
        self.preview_title.setStyleSheet("font-weight: bold; padding: 2px;")
        self.preview = PreviewPane()
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 8, 8, 8)
        right_layout.addWidget(self.preview_title)
        right_layout.addWidget(self.preview, 1)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 800])
        self.setCentralWidget(splitter)

        QShortcut(QKeySequence.Delete, self.file_list, self._remove_selected)
        QShortcut(QKeySequence(Qt.Key_Backspace), self.file_list, self._remove_selected)
        QShortcut(QKeySequence.Open, self, self._browse)
        QShortcut(QKeySequence.Save, self, self._save)
        QShortcut(QKeySequence("Ctrl+Up"), self, lambda: self._move(-1))
        QShortcut(QKeySequence("Ctrl+Down"), self, lambda: self._move(1))

        self._update_status()

    # ---- adding -------------------------------------------------------------

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add PDFs", "", "PDF files (*.pdf)")
        self.add_files(paths)

    def _open_pdf(self, path):
        """Open a PDF, prompting for a password if needed. Returns None if skipped."""
        password = None
        while True:
            try:
                return pdfium.PdfDocument(path, password=password)
            except pdfium.PdfiumError as exc:
                if exc.err_code != pdfium_c.FPDF_ERR_PASSWORD:
                    QMessageBox.warning(self, APP_NAME, f"Could not open:\n{path}\n\n{exc}")
                    return None
                prompt = "Incorrect password, try again:" if password else "Password:"
                password, ok = QInputDialog.getText(
                    self,
                    "Password required",
                    f"{os.path.basename(path)} is password protected.\n{prompt}",
                    QLineEdit.Password,
                )
                if not ok:
                    return None

    def add_files(self, paths):
        pdfs = []
        for path in paths:
            if os.path.isdir(path):
                pdfs += sorted(
                    os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(".pdf")
                )
            elif path.lower().endswith(".pdf"):
                pdfs.append(path)
        skipped = [p for p in paths if not os.path.isdir(p) and not p.lower().endswith(".pdf")]

        first_new = None
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for path in pdfs:
                doc = self._open_pdf(path)
                if doc is None:
                    continue
                if len(doc) == 0:
                    doc.close()
                    skipped.append(path)
                    continue
                key = self._next_key
                self._next_key += 1
                self._docs[key] = doc

                thumb = QPixmap(THUMB_SIZE * self.devicePixelRatioF())
                thumb.setDevicePixelRatio(self.devicePixelRatioF())
                thumb.fill(Qt.transparent)
                image = render_page(doc, 0, THUMB_SIZE.width(), self.devicePixelRatioF())
                if image.height() / image.devicePixelRatio() > THUMB_SIZE.height():
                    image = image.scaledToHeight(
                        int(THUMB_SIZE.height() * image.devicePixelRatio()), Qt.SmoothTransformation
                    )
                painter = QPainter(thumb)
                logical = image.size() / image.devicePixelRatio()
                x = (THUMB_SIZE.width() - logical.width()) // 2
                y = (THUMB_SIZE.height() - logical.height()) // 2
                painter.drawImage(x, y, image)
                painter.setPen(QColor(150, 150, 150))
                painter.drawRect(x, y, logical.width() - 1, logical.height() - 1)
                painter.end()

                pages = len(doc)
                item = QListWidgetItem(
                    QIcon(thumb), f"{os.path.basename(path)}\n{pages} page{'s' if pages != 1 else ''}"
                )
                item.setData(ROLE_PATH, path)
                item.setData(ROLE_KEY, key)
                item.setToolTip(path)
                self.file_list.addItem(item)
                first_new = first_new or item
        finally:
            QApplication.restoreOverrideCursor()

        if skipped:
            QMessageBox.information(
                self, APP_NAME, "Skipped (not a PDF or empty):\n" + "\n".join(skipped)
            )
        if first_new is not None:
            self.file_list.setCurrentItem(first_new)
        self._update_status()

    # ---- list management ----------------------------------------------------

    def _selected_rows(self):
        return sorted(self.file_list.row(i) for i in self.file_list.selectedItems())

    def _move(self, delta):
        rows = self._selected_rows()
        if not rows:
            return
        if (delta < 0 and rows[0] == 0) or (delta > 0 and rows[-1] == self.file_list.count() - 1):
            return
        current = self.file_list.currentItem()
        moved = []
        for row in rows if delta < 0 else reversed(rows):
            item = self.file_list.takeItem(row)
            self.file_list.insertItem(row + delta, item)
            moved.append(item)
        self.file_list.setCurrentItem(current)
        for item in moved:
            item.setSelected(True)
        self._update_status()

    def _remove_selected(self):
        for row in reversed(self._selected_rows()):
            item = self.file_list.takeItem(row)
            self._release(item)
        self._update_status()

    def _clear(self):
        while self.file_list.count():
            self._release(self.file_list.takeItem(0))
        self._update_status()

    def _release(self, item):
        doc = self._docs.pop(item.data(ROLE_KEY), None)
        if doc is not None:
            if self.preview._doc is doc:
                self.preview.show_document(None)
                self.preview_title.setText("No file selected")
            doc.close()

    def _on_current_changed(self, current, _previous):
        if current is None:
            self.preview.show_document(None)
            self.preview_title.setText("No file selected")
            return
        doc = self._docs[current.data(ROLE_KEY)]
        if self.preview._doc is not doc:
            self.preview_title.setText(current.data(ROLE_PATH))
            self.preview.show_document(doc)

    def _update_status(self, *_):
        count = self.file_list.count()
        pages = sum(len(self._docs[self.file_list.item(i).data(ROLE_KEY)]) for i in range(count))
        has_sel = bool(self.file_list.selectedItems())
        self.up_btn.setEnabled(has_sel and count > 1)
        self.down_btn.setEnabled(has_sel and count > 1)
        self.remove_btn.setEnabled(has_sel)
        self.clear_btn.setEnabled(count > 0)
        self.save_btn.setEnabled(count > 0)
        self.statusBar().showMessage(
            f"{count} file{'s' if count != 1 else ''}, {pages} page{'s' if pages != 1 else ''} total"
            if count
            else "Drag PDF files into the left pane to begin"
        )

    # ---- saving -------------------------------------------------------------

    def _save(self):
        count = self.file_list.count()
        if not count:
            return
        first = self.file_list.item(0).data(ROLE_PATH)
        out, _ = QFileDialog.getSaveFileName(
            self,
            "Save Combined PDF",
            os.path.join(os.path.dirname(first), "combined.pdf"),
            "PDF files (*.pdf)",
        )
        if not out:
            return
        if not out.lower().endswith(".pdf"):
            out += ".pdf"
        sources = {os.path.normcase(os.path.abspath(self.file_list.item(i).data(ROLE_PATH)))
                   for i in range(count)}
        if os.path.normcase(os.path.abspath(out)) in sources:
            QMessageBox.warning(
                self, APP_NAME, "The output file is one of the input files.\nChoose a different name."
            )
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        tmp_path = None
        try:
            combined = pdfium.PdfDocument.new()
            for i in range(count):
                combined.import_pages(self._docs[self.file_list.item(i).data(ROLE_KEY)])
            # write to a temp file next to the target, then swap it in
            fd, tmp_path = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(out) or ".")
            with os.fdopen(fd, "wb") as fh:
                combined.save(fh)
            pages = len(combined)
            combined.close()
            # mkstemp creates the file owner-only; give it normal permissions
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp_path, 0o666 & ~umask)
            os.replace(tmp_path, out)
            tmp_path = None
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, APP_NAME, f"Failed to save combined PDF:\n\n{exc}")
            return
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(f"Saved {pages} pages to {out}", 10000)
        QMessageBox.information(self, APP_NAME, f"Saved {pages} pages to:\n{out}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(ICON_PATH))
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        window.add_files(sys.argv[1:])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
