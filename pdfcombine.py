"""PDF Combine - drag PDFs in, reorder files and pages, save one combined PDF."""

import math
import os
import sys
import tempfile
import time

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PySide6.QtCore import QEvent, QLine, QMimeData, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDrag,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
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
PAGE_ASPECT = 1.3  # height / width of a page-grid cell
ZOOM_MIN, ZOOM_MAX = 70, 1000  # thumbnail width in pixels
ZOOM_STEPS = 1000  # slider positions; the scale is logarithmic and starts in the middle
ZOOM_STEP = 1.2
ROLE_PATH = Qt.UserRole
ROLE_KEY = Qt.UserRole + 1
ROLE_FIRST_PAGE = Qt.UserRole + 2
ROLE_PAGE = Qt.UserRole
ROLE_RENDERED = Qt.UserRole + 1
PAGE_MIME = "application/x-pdfcombine-pages"


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


def _fit_rect(box, w_pt, h_pt):
    """Rect of a page scaled to fit, centred in box."""
    scale = min(box.width() / w_pt, box.height() / h_pt)
    w, h = max(int(w_pt * scale), 1), max(int(h_pt * scale), 1)
    return QRect((box.width() - w) // 2, (box.height() - h) // 2, w, h)


def _blank_pixmap(box, dpr):
    pix = QPixmap(box * dpr)
    pix.setDevicePixelRatio(dpr)
    pix.fill(Qt.transparent)
    return pix


def make_thumb(doc, index, box, dpr):
    """Thumbnail of one page, fitted and centred in box, with a thin border."""
    rect = _fit_rect(box, *doc.get_page_size(index))
    pix = _blank_pixmap(box, dpr)
    painter = QPainter(pix)
    painter.drawImage(rect, render_page(doc, index, rect.width(), dpr))
    painter.setPen(QColor(150, 150, 150))
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
    painter.end()
    return pix


def make_placeholder(doc, index, box, dpr):
    """Cheap stand-in shown until the real thumbnail has been rendered."""
    rect = _fit_rect(box, *doc.get_page_size(index))
    pix = _blank_pixmap(box, dpr)
    painter = QPainter(pix)
    painter.fillRect(rect, QColor(245, 245, 245))
    painter.setPen(QColor(200, 200, 200))
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
    painter.end()
    return pix


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


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


class PageGrid(QListWidget):
    """Grid of page thumbnails. Drag to reorder; the drop is handled here, not by Qt,
    so the page order stays owned by PageEditor."""

    reorder_requested = Signal(list, int)  # selected rows, insertion row
    zoom_requested = Signal(float)  # scale factor

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # setMovement(Static) turns drag and drop off, including on the viewport,
        # which is what actually receives drops - switch both back on
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        self._drop = None  # (insertion row, indicator line) while a drag is over us

    def wheelEvent(self, event):
        # Qt maps Cmd to ControlModifier on macOS, so this is Cmd+scroll there
        if event.modifiers() & Qt.ControlModifier and event.angleDelta().y():
            self.zoom_requested.emit(ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP)
            event.accept()
        else:
            super().wheelEvent(event)

    def event(self, event):
        if event.type() == QEvent.NativeGesture and event.gestureType() == Qt.ZoomNativeGesture:
            self.zoom_requested.emit(1 + event.value())  # trackpad pinch
            return True
        return super().event(event)

    def startDrag(self, _actions):
        items = self.selectedItems()
        if not items:
            return
        mime = QMimeData()
        mime.setData(PAGE_MIME, b"")
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(items[0].icon().pixmap(self.iconSize() * 0.6))
        drag.exec(Qt.MoveAction)
        self._set_drop(None)

    def _accepts(self, event):
        return event.source() is self and event.mimeData().hasFormat(PAGE_MIME)

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._accepts(event):
            event.ignore()
            return
        event.acceptProposedAction()
        pos = event.position().toPoint()
        self._autoscroll(pos)
        self._set_drop(self.drop_target(pos))

    def dragLeaveEvent(self, event):
        self._set_drop(None)

    def dropEvent(self, event):
        if not self._accepts(event) or self._drop is None:
            event.ignore()
            return
        event.setDropAction(Qt.MoveAction)
        event.accept()
        rows = sorted(self.row(i) for i in self.selectedItems())
        target = self._drop[0]
        self._set_drop(None)
        # defer: the grid gets rebuilt, which must not happen inside the drag loop
        QTimer.singleShot(0, lambda: self.reorder_requested.emit(rows, target))

    def drop_target(self, pos):
        """Insertion row for a drop at pos, plus the line to draw for it."""
        n = self.count()
        if n == 0:
            return 0, None
        rects = [self.visualItemRect(self.item(i)) for i in range(n)]

        def before(i):
            r = rects[i]
            return i, QLine(r.left() - 3, r.top(), r.left() - 3, r.bottom())

        def after(i):
            r = rects[i]
            return i + 1, QLine(r.right() + 3, r.top(), r.right() + 3, r.bottom())

        same_row = [i for i, r in enumerate(rects) if r.top() <= pos.y() <= r.bottom()]
        if not same_row:
            return before(0) if pos.y() < rects[0].top() else after(n - 1)
        for i in same_row:
            if pos.x() < rects[i].center().x():
                return before(i)
        return after(same_row[-1])

    def _set_drop(self, drop):
        self._drop = drop
        self.viewport().update()

    def _autoscroll(self, pos):
        bar, margin = self.verticalScrollBar(), 40
        if pos.y() < margin:
            bar.setValue(bar.value() - 20)
        elif pos.y() > self.viewport().height() - margin:
            bar.setValue(bar.value() + 20)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop and self._drop[1] is not None:
            painter = QPainter(self.viewport())
            painter.setPen(QPen(self.palette().highlight().color(), 3))
            painter.drawLine(self._drop[1])


class PageEditor(QWidget):
    """Right pane: page grid for the selected file, with delete / reset / undo / zoom."""

    pages_edited = Signal(int, list)  # file key, new page order (original page indexes)
    undo_requested = Signal()

    def __init__(self):
        super().__init__()
        self._key = None
        self._doc = None
        self._pages = []
        self._name = ""
        self._cache = {}  # page index -> thumbnail at the current zoom, current file only
        self._select_next = None

        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        hint = QLabel("Drag to reorder · Delete to remove · Double-click to enlarge")
        hint.setStyleSheet("color: gray;")

        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(0, ZOOM_STEPS)
        self.zoom.setValue(ZOOM_STEPS // 2)
        self.zoom.setFixedWidth(200)
        self.zoom.setToolTip("Thumbnail size: also Ctrl/Cmd + scroll, pinch, or Ctrl/Cmd +/−")
        self.zoom.valueChanged.connect(self._on_zoom)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.undo_requested)
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Restore this file's original pages and order")
        self.reset_btn.clicked.connect(self.reset)
        self.delete_btn = QPushButton("Delete Pages")
        self.delete_btn.clicked.connect(self.delete_selected)

        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.undo_btn)
        top.addWidget(self.reset_btn)
        top.addWidget(self.delete_btn)
        second = QHBoxLayout()
        second.addWidget(hint, 1)
        second.addWidget(QLabel("Zoom"))
        second.addWidget(self.zoom)

        self.grid = PageGrid()
        self.grid.reorder_requested.connect(self._on_reorder)
        self.grid.zoom_requested.connect(self.zoom_by)
        self.grid.itemSelectionChanged.connect(self._update_buttons)
        self.grid.itemDoubleClicked.connect(self._open_viewer)
        self.grid.verticalScrollBar().valueChanged.connect(self._schedule_render)
        for key in (QKeySequence.Delete, QKeySequence(Qt.Key_Backspace)):
            shortcut = QShortcut(key, self.grid, self.delete_selected)
            shortcut.setContext(Qt.WidgetShortcut)

        for key, factor in (
            (QKeySequence.ZoomIn, ZOOM_STEP),
            (QKeySequence("Ctrl+="), ZOOM_STEP),
            (QKeySequence.ZoomOut, 1 / ZOOM_STEP),
        ):
            QShortcut(key, self, lambda f=factor: self.zoom_by(f))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 8, 8)
        layout.addLayout(top)
        layout.addLayout(second)
        layout.addWidget(self.grid, 1)

        self._render_timer = QTimer(self, singleShot=True, interval=0)
        self._render_timer.timeout.connect(self._render_tick)
        self._apply_zoom()
        self.set_file(None, None, [], "")

    # ---- public -------------------------------------------------------------

    def set_file(self, key, doc, pages, name):
        if key != self._key:
            self._cache.clear()
            self.grid.clearSelection()  # don't carry a selection over to another file
        self._key, self._doc, self._pages, self._name = key, doc, list(pages), name
        self._populate(scroll_to_top=True)

    def refresh(self, pages):
        """Show a new page order for the current file, keeping scroll position."""
        self._pages = list(pages)
        self._populate()

    def set_undo_enabled(self, enabled):
        self.undo_btn.setEnabled(enabled)

    def zoom_by(self, factor):
        if self._doc is not None:
            width = min(max(self._zoom_width() * factor, ZOOM_MIN), ZOOM_MAX)
            self.zoom.setValue(round(ZOOM_STEPS * math.log(width / ZOOM_MIN) / math.log(ZOOM_MAX / ZOOM_MIN)))

    def delete_selected(self):
        rows = {self.grid.row(i) for i in self.grid.selectedItems()}
        if not rows:
            return
        if len(rows) == len(self._pages):
            QMessageBox.information(
                self,
                APP_NAME,
                "A file needs at least one page.\n"
                "To leave the whole file out, remove it from the list on the left.",
            )
            return
        first = min(rows)
        new = [p for i, p in enumerate(self._pages) if i not in rows]
        self._select_next = [new[min(first, len(new) - 1)]]
        self.pages_edited.emit(self._key, new)

    def reset(self):
        if self._doc is not None:
            self.pages_edited.emit(self._key, list(range(len(self._doc))))

    # ---- internals ----------------------------------------------------------

    def _box(self):
        z = self._zoom_width()
        return QSize(z, int(z * PAGE_ASPECT))

    def _zoom_width(self):
        """Thumbnail width for the slider position (logarithmic, ZOOM_MIN..ZOOM_MAX)."""
        return round(ZOOM_MIN * (ZOOM_MAX / ZOOM_MIN) ** (self.zoom.value() / ZOOM_STEPS))

    def _apply_zoom(self):
        box = self._box()
        self.grid.setIconSize(box)
        self.grid.setGridSize(QSize(box.width() + 24, box.height() + 34))
        self._update_hscroll()

    def _update_hscroll(self):
        """Only allow sideways scrolling when a single page is wider than the pane."""
        too_wide = self.grid.gridSize().width() > self.grid.viewport().width()
        self.grid.setHorizontalScrollBarPolicy(
            Qt.ScrollBarAsNeeded if too_wide else Qt.ScrollBarAlwaysOff
        )

    def _on_zoom(self, _value):
        self._cache.clear()
        self._apply_zoom()
        self._populate()

    def _populate(self, scroll_to_top=False):
        bar = self.grid.verticalScrollBar()
        scroll = 0 if scroll_to_top else bar.value()
        selected = self._select_next or [i.data(ROLE_PAGE) for i in self.grid.selectedItems()]
        self._select_next = None

        self.grid.clear()
        if self._doc is not None:
            box, dpr = self._box(), self.devicePixelRatioF()
            for p in self._pages:
                cached = self._cache.get(p)
                pix = cached or make_placeholder(self._doc, p, box, dpr)
                item = QListWidgetItem(QIcon(pix), str(p + 1))
                item.setData(ROLE_PAGE, p)
                item.setData(ROLE_RENDERED, cached is not None)
                item.setToolTip(f"Original page {p + 1}")
                item.setTextAlignment(Qt.AlignHCenter)
                self.grid.addItem(item)
            for i in range(self.grid.count()):
                if self.grid.item(i).data(ROLE_PAGE) in selected:
                    self.grid.item(i).setSelected(True)
        QTimer.singleShot(0, lambda: bar.setValue(scroll))

        if self._doc is None:
            self.title.setText("No file selected")
        else:
            total = len(self._doc)
            count = len(self._pages)
            pages = plural(total, "page") if count == total else f"{count} of {plural(total, 'page')}"
            self.title.setText(f"{self._name} — {pages}")
        self._update_buttons()
        self._schedule_render()

    def _update_buttons(self):
        has_doc = self._doc is not None
        self.delete_btn.setEnabled(has_doc and bool(self.grid.selectedItems()))
        self.reset_btn.setEnabled(has_doc and self._pages != list(range(len(self._doc or []))))
        self.zoom.setEnabled(has_doc)

    def _on_reorder(self, rows, target):
        if self._doc is None or not rows:
            return
        moving = [self._pages[r] for r in rows]
        rest = [p for i, p in enumerate(self._pages) if i not in set(rows)]
        at = target - sum(1 for r in rows if r < target)
        new = rest[:at] + moving + rest[at:]
        if new != self._pages:
            self._select_next = moving
            self.pages_edited.emit(self._key, new)

    def _schedule_render(self, *_):
        self._render_timer.start()

    def _render_tick(self):
        """Render a few visible thumbnails per tick so the UI stays responsive."""
        if self._doc is None:
            return
        visible = self.grid.viewport().rect().adjusted(0, -300, 0, 300)
        box, dpr = self._box(), self.devicePixelRatioF()
        deadline = time.monotonic() + 0.04
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            if item.data(ROLE_RENDERED) or not visible.intersects(self.grid.visualItemRect(item)):
                continue
            p = item.data(ROLE_PAGE)
            if p not in self._cache:
                self._cache[p] = make_thumb(self._doc, p, box, dpr)
            item.setIcon(QIcon(self._cache[p]))
            item.setData(ROLE_RENDERED, True)
            if time.monotonic() > deadline:
                self._render_timer.start()
                return

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_hscroll()
        self._schedule_render()

    def _open_viewer(self, item):
        viewer = PageViewer(
            self, self._doc, self._pages, self.grid.row(item), f"{self._name} — {APP_NAME}"
        )
        viewer.exec()


class PreviewPane(QScrollArea):
    """Scrollable full-width view of a list of pages, rendered lazily."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setAlignment(Qt.AlignHCenter)
        self._doc = None
        self._pages = []
        self._labels = []
        self._rendered = set()
        self._render_width = 0
        self._anchor = None  # page position to keep at the top across relayouts

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

    def show_document(self, doc, pages, start=0):
        for label in self._labels:
            label.deleteLater()
        self._labels, self._rendered = [], set()
        self._doc, self._pages, self._anchor = doc, list(pages), start
        for _ in self._pages:
            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("background: white;")
            self._layout.addWidget(label)
            self._labels.append(label)
        self._render_width = 0
        self._relayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._anchor is None:
            self._anchor = self._top_page()
        self._timer.start()

    def _top_page(self):
        top = self.verticalScrollBar().value()
        for i, label in enumerate(self._labels):
            if label.y() + label.height() > top:
                return i
        return 0

    def _relayout(self):
        """Size placeholders to each page's aspect ratio, keep our place, render what shows."""
        if self._doc is None:
            return
        width = max(self.viewport().width() - 2 * PAGE_GAP, 100)
        if width != self._render_width:
            self._render_width = width
            self._rendered.clear()
            for label, p in zip(self._labels, self._pages):
                w_pt, h_pt = self._doc.get_page_size(p)
                label.setFixedSize(width, int(width * h_pt / w_pt))
                label.clear()
        if self._anchor is not None and self._labels:
            self.widget().layout().activate()
            self.verticalScrollBar().setValue(self._labels[self._anchor].y() - PAGE_GAP)
            if self.isVisible():
                self._anchor = None
        QTimer.singleShot(0, self._render_visible)

    def _render_visible(self):
        if self._doc is None:
            return
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        dpr = self.devicePixelRatioF()
        for i, (label, p) in enumerate(zip(self._labels, self._pages)):
            if i in self._rendered:
                continue
            y = label.y()
            if y + label.height() >= top - 400 and y <= bottom + 400:
                label.setPixmap(QPixmap.fromImage(render_page(self._doc, p, label.width(), dpr)))
                self._rendered.add(i)


class PageViewer(QDialog):
    """Full-size view of a file's pages (in edited order), opened at one page."""

    def __init__(self, parent, doc, pages, start, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        screen = self.screen().availableGeometry()
        self.resize(int(screen.width() * 0.6), int(screen.height() * 0.85))
        self.preview = PreviewPane()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.preview)
        self.preview.show_document(doc, pages, start)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 800)
        self._docs = {}  # item key -> open pdfium document
        self._pages = {}  # item key -> page order to save (original page indexes)
        self._undo = []  # (item key, previous page order)
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

        self.editor = PageEditor()
        self.editor.pages_edited.connect(self._apply_pages)
        self.editor.undo_requested.connect(self._undo_last)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.editor)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 900])
        self.setCentralWidget(splitter)

        for key in (QKeySequence.Delete, QKeySequence(Qt.Key_Backspace)):
            shortcut = QShortcut(key, self.file_list, self._remove_selected)
            shortcut.setContext(Qt.WidgetShortcut)
        QShortcut(QKeySequence.Open, self, self._browse)
        QShortcut(QKeySequence.Save, self, self._save)
        QShortcut(QKeySequence.Undo, self, self._undo_last)
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
                self._pages[key] = list(range(len(doc)))

                item = QListWidgetItem()
                item.setData(ROLE_PATH, path)
                item.setData(ROLE_KEY, key)
                item.setToolTip(path)
                self._refresh_file_item(item)
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

    def _refresh_file_item(self, item):
        """Update a file's label and, if its first page changed, its thumbnail."""
        key = item.data(ROLE_KEY)
        doc, pages = self._docs[key], self._pages[key]
        total = len(doc)
        count = plural(total, "page") if len(pages) == total else f"{len(pages)} of {total} pages"
        item.setText(f"{os.path.basename(item.data(ROLE_PATH))}\n{count}")
        if item.data(ROLE_FIRST_PAGE) != pages[0]:
            item.setIcon(QIcon(make_thumb(doc, pages[0], THUMB_SIZE, self.devicePixelRatioF())))
            item.setData(ROLE_FIRST_PAGE, pages[0])

    # ---- page edits ---------------------------------------------------------

    def _find_item(self, key):
        for i in range(self.file_list.count()):
            if self.file_list.item(i).data(ROLE_KEY) == key:
                return self.file_list.item(i)
        return None

    def _set_pages(self, key, pages):
        self._pages[key] = list(pages)
        item = self._find_item(key)
        self._refresh_file_item(item)
        if self.file_list.currentItem() is item:
            self.editor.refresh(pages)
        else:
            self.file_list.setCurrentItem(item)
        self.editor.set_undo_enabled(bool(self._undo))
        self._update_status()

    def _apply_pages(self, key, pages):
        if key not in self._docs or pages == self._pages[key]:
            return
        self._undo.append((key, self._pages[key]))
        self._set_pages(key, pages)

    def _undo_last(self):
        if self._undo:
            key, pages = self._undo.pop()
            self._set_pages(key, pages)

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
            self._release(self.file_list.takeItem(row))
        self._update_status()

    def _clear(self):
        while self.file_list.count():
            self._release(self.file_list.takeItem(0))
        self._update_status()

    def _release(self, item):
        key = item.data(ROLE_KEY)
        doc = self._docs.pop(key, None)
        self._pages.pop(key, None)
        self._undo = [u for u in self._undo if u[0] != key]
        self.editor.set_undo_enabled(bool(self._undo))
        if doc is not None:
            if self.editor._doc is doc:
                self.editor.set_file(None, None, [], "")
            doc.close()

    def _on_current_changed(self, current, _previous):
        if current is None:
            self.editor.set_file(None, None, [], "")
            return
        key = current.data(ROLE_KEY)
        if self.editor._key != key:
            name = os.path.basename(current.data(ROLE_PATH))
            self.editor.set_file(key, self._docs[key], self._pages[key], name)

    def _update_status(self, *_):
        count = self.file_list.count()
        pages = sum(len(self._pages[self.file_list.item(i).data(ROLE_KEY)]) for i in range(count))
        has_sel = bool(self.file_list.selectedItems())
        self.up_btn.setEnabled(has_sel and count > 1)
        self.down_btn.setEnabled(has_sel and count > 1)
        self.remove_btn.setEnabled(has_sel)
        self.clear_btn.setEnabled(count > 0)
        self.save_btn.setEnabled(count > 0)
        self.editor.set_undo_enabled(bool(self._undo))
        self.statusBar().showMessage(
            f"{plural(count, 'file')}, {plural(pages, 'page')} total"
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
                key = self.file_list.item(i).data(ROLE_KEY)
                combined.import_pages(self._docs[key], pages=self._pages[key])
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
