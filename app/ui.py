from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import (
    Qt, Signal, QObject, QRunnable, Slot, QUrl, QSettings, QSize, QRectF, QPointF,
    QThreadPool, QTimer, QPoint, QRect
)
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QFont, QAction, QKeySequence, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QProgressDialog, QPushButton, QScrollArea, QSlider,
    QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget, QGridLayout,
    QSizePolicy, QAbstractItemView, QRubberBand
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget

from .exporter import ExportEngine, ExportItem, unique_file
from .history import History
from .media import VIDEO_EXTS, generate_thumbnails, generate_waveform, probe_keyframes, probe_media, lossless_safe_bounds
from .model import SegmentModel


# -----------------------------------------------------------------------------
# Design tokens – one compact system instead of ad-hoc Qt defaults.
# -----------------------------------------------------------------------------
C = {
    'app': '#0A0D12',
    'top': '#0D1117',
    'panel': '#10151C',
    'panel_2': '#131922',
    'panel_3': '#171E28',
    'raised': '#1B2330',
    'hover': '#202A37',
    'line': '#252E3A',
    'line_soft': '#1B222C',
    'text': '#F4F7FB',
    'text_2': '#C5CCD6',
    'muted': '#7F8998',
    'muted_2': '#596373',
    'accent': '#2D9CFF',
    'accent_hover': '#45A8FF',
    'accent_soft': '#17334D',
    'danger': '#FF636B',
    'green': '#56D49A',
    'black': '#030507',
}


def fmt_ms(ms):
    ms = max(0, int(ms))
    s, milli = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f'{h:02d}:{m:02d}:{s:02d}.{milli:03d}'
    return f'{m:02d}:{s:02d}.{milli:03d}'


def fmt_ruler(ms):
    ms = max(0, int(ms))
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def human_bytes(n):
    n = float(max(0, n))
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f'{n:.1f} {units[i]}' if i else f'{int(n)} B'


class WorkerSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class FnWorker(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            self.signals.done.emit(self.fn())
        except Exception as exc:
            self.signals.error.emit(str(exc))




class ClipListWidget(QListWidget):
    """Editor-style clip browser with deterministic click and marquee selection.

    Selection is handled here instead of relying on QListWidget's default drag
    selection.  That makes Ctrl/Shift clicks and true rubber-band selection
    behave the same even when a drag starts directly on top of a clip card.
    """
    marqueeFinished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(False)
        self.setAutoScroll(False)  # marquee path performs predictable edge scrolling itself
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._press_pos = None
        self._press_mods = Qt.NoModifier
        self._base_rows = set()
        self._anchor_row = None
        self._marquee_active = False
        self._rubber = QRubberBand(QRubberBand.Rectangle, self.viewport())

    def _selected_rows(self):
        return {self.row(i) for i in self.selectedItems()}

    def _apply_rows(self, rows):
        wanted = set(rows)
        self.blockSignals(True)
        try:
            for row in range(self.count()):
                self.item(row).setSelected(row in wanted)
        finally:
            self.blockSignals(False)
        # Emit exactly once so Timeline / inspector stay synchronized.
        self.itemSelectionChanged.emit()

    def _edge_scroll(self, pos):
        """Scroll while marquee approaches the top/bottom of the list."""
        margin = 28
        bar = self.verticalScrollBar()
        if pos.y() < margin:
            bar.setValue(bar.value() - max(8, margin - pos.y()))
        elif pos.y() > self.viewport().height() - margin:
            bar.setValue(bar.value() + max(8, pos.y() - (self.viewport().height() - margin)))

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            super().mousePressEvent(e)
            return
        self._press_pos = e.position().toPoint()
        self._press_mods = e.modifiers()
        self._base_rows = self._selected_rows()
        self._marquee_active = False
        e.accept()

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.LeftButton) or self._press_pos is None:
            super().mouseMoveEvent(e)
            return

        cur = e.position().toPoint()
        if not self._marquee_active and (cur - self._press_pos).manhattanLength() < 6:
            e.accept()
            return

        self._marquee_active = True
        self._edge_scroll(cur)
        # visualItemRect already reflects any edge scrolling.  Rebase the
        # rubber band to the current viewport while keeping the gesture origin.
        rect = QRect(self._press_pos, cur).normalized().intersected(self.viewport().rect())
        self._rubber.setGeometry(rect)
        self._rubber.show()

        hit = set()
        for row in range(self.count()):
            item = self.item(row)
            if self.visualItemRect(item).intersects(rect):
                hit.add(row)
        wanted = (self._base_rows | hit) if (self._press_mods & Qt.ControlModifier) else hit
        self._apply_rows(wanted)
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._press_pos is None:
            super().mouseReleaseEvent(e)
            return

        pos = e.position().toPoint()
        if self._marquee_active:
            self._rubber.hide()
            self._marquee_active = False
            self._press_pos = None
            self.marqueeFinished.emit()
            e.accept()
            return

        item = self.itemAt(pos)
        row = self.row(item) if item is not None else -1
        mods = self._press_mods
        selected = self._selected_rows()

        if row < 0:
            if not (mods & Qt.ControlModifier):
                selected = set()
        elif mods & Qt.ShiftModifier:
            anchor = self._anchor_row if self._anchor_row is not None else row
            lo, hi = sorted((anchor, row))
            span = set(range(lo, hi + 1))
            selected = (selected | span) if (mods & Qt.ControlModifier) else span
        elif mods & Qt.ControlModifier:
            if row in selected:
                selected.remove(row)
            else:
                selected.add(row)
            self._anchor_row = row
        else:
            selected = {row}
            self._anchor_row = row

        if row >= 0:
            self.setCurrentItem(self.item(row))
        self._apply_rows(selected)
        self._press_pos = None
        e.accept()

class DropPage(QWidget):
    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(
            Path(u.toLocalFile()).suffix.lower() in VIDEO_EXTS for u in e.mimeData().urls()
        ):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if Path(p).suffix.lower() in VIDEO_EXTS:
                self.fileDropped.emit(p)
                e.acceptProposedAction()
                return
        e.ignore()


class VideoDropWidget(QVideoWidget):
    fileDropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(
            Path(u.toLocalFile()).suffix.lower() in VIDEO_EXTS for u in e.mimeData().urls()
        ):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if Path(p).suffix.lower() in VIDEO_EXTS:
                self.fileDropped.emit(p)
                e.acceptProposedAction()
                return
        e.ignore()


class TimelineCanvas(QWidget):
    seek = Signal(int)
    split = Signal(int)
    select = Signal(int, str)
    marqueeSelect = Signal(object, str)
    trimPreview = Signal(int, str, int)
    trimCommit = Signal(int, str, int)
    zoomRequest = Signal(float, int)
    panRequest = Signal(int)

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.duration_ms = 1
        self.position = 0
        self.px_per_sec = 42.0
        self.min_content_px = 240
        self.thumbs = []
        self.waveform = QPixmap()
        self.keyframes = []
        self.markers = []
        self.in_point = None
        self.out_point = None

        self.margin = 18
        self.ruler_h = 34
        self.track_top = 43
        self.video_h = 102
        self.wave_h = 44
        self.track_gap = 6
        self.track_h = self.video_h + self.track_gap + self.wave_h
        self.footer_h = 26

        self.drag_mode = None
        self.drag_uid = None
        self.drag_edge = None
        self.press_pos = None
        self.press_uid = None
        self.press_from_ruler = False
        self.press_mode = 'replace'
        self.marquee = None
        self.pan_last = None
        self.tool_mode = 'select'

        # The canvas height is synchronized to the visible scroll viewport by
        # MainWindow. Keep a hard floor so the ruler/tracks can never collapse
        # to zero when the editor splitter is dragged.
        self.setMinimumHeight(124)
        self.setMouseTracking(True)
        self.refresh_width()

    def set_duration(self, ms):
        self.duration_ms = max(1, int(ms))
        self.refresh_width()
        self.update()

    def set_position(self, ms):
        self.position = max(0, min(int(ms), self.duration_ms))
        self.update()

    def set_thumbs(self, paths):
        result = []
        for p in paths:
            pm = QPixmap(p)
            if not pm.isNull():
                result.append(pm)
        self.thumbs = result
        self.update()

    def set_keyframes(self, keyframes):
        self.keyframes = keyframes or []
        self.update()

    def set_waveform(self, path):
        pm = QPixmap(path) if path else QPixmap()
        if not pm.isNull():
            tinted = QPixmap(pm.size())
            tinted.fill(Qt.transparent)
            qp = QPainter(tinted)
            qp.drawPixmap(0, 0, pm)
            qp.setCompositionMode(QPainter.CompositionMode_SourceIn)
            qp.fillRect(tinted.rect(), QColor('#2D9CFF'))
            qp.end()
            pm = tinted
        self.waveform = pm
        self.update()

    def set_markers(self, markers):
        self.markers = list(markers or [])
        self.update()

    def set_in_out(self, in_point, out_point):
        self.in_point = in_point
        self.out_point = out_point
        self.update()

    def snap_ms(self, ms, include_keyframes=False):
        ms = int(max(0, min(self.duration_ms, ms)))
        targets = [0, self.duration_ms]
        for seg in self.model.segments:
            targets.extend([seg.start_ms, seg.end_ms])
        targets.extend(self.markers)
        if include_keyframes:
            targets.extend(self.keyframes)
        threshold_ms = max(8, int(8 / max(self.px_per_sec, 0.001) * 1000))
        nearest = min(targets, key=lambda x: abs(x-ms), default=ms)
        return nearest if abs(nearest-ms) <= threshold_ms else ms

    def set_min_content_width(self, px):
        self.min_content_px = max(200, int(px))
        self.refresh_width()

    def refresh_width(self):
        # Never collapse below 1/4 viewport (configured by MainWindow), with 200px absolute floor.
        content = max(int(self.min_content_px), int(self.duration_ms / 1000 * self.px_per_sec))
        self.setMinimumWidth(content + 2 * self.margin)
        self.resize(self.minimumWidth(), self.height())

    def x_for_ms(self, ms):
        return self.margin + (ms / 1000.0) * self.px_per_sec

    def ms_for_x(self, x):
        return int(max(0, min(self.duration_ms, (x - self.margin) / self.px_per_sec * 1000)))

    def segment_rect(self, s):
        return QRectF(
            self.x_for_ms(s.start_ms), self.track_top,
            max(2, self.x_for_ms(s.end_ms) - self.x_for_ms(s.start_ms)), self.track_h
        )

    def hit(self, pos):
        for s in reversed(self.model.segments):
            r = self.segment_rect(s)
            if r.contains(pos):
                edge = None
                if not s.deleted:
                    if abs(pos.x() - r.left()) <= 9:
                        edge = 'left'
                    elif abs(pos.x() - r.right()) <= 9:
                        edge = 'right'
                return s, edge
        return None, None

    def _marquee_rect(self, current_pos):
        rect = QRectF(self.press_pos, current_pos).normalized()
        if getattr(self, 'press_from_ruler', False):
            left, right = rect.left(), rect.right()
            # Ruler drag represents a horizontal time-range selection.  Extend
            # it through both video and waveform tracks even if the pointer
            # never leaves the ruler itself.
            return QRectF(left, self.ruler_h, max(1.0, right - left),
                          self.track_top + self.track_h - self.ruler_h)
        return rect

    def set_tool_mode(self, mode):
        self.tool_mode = 'hand' if mode == 'hand' else 'select'
        self.drag_mode = None
        self.pan_last = None
        self.marquee = None
        self._set_idle_cursor()
        self.update()

    def _set_idle_cursor(self, pos=None):
        if self.tool_mode == 'hand':
            self.setCursor(Qt.OpenHandCursor)
            return
        if pos is not None:
            s, edge = self.hit(pos)
            if edge:
                self.setCursor(Qt.SizeHorCursor)
            elif s and not s.deleted:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def resizeEvent(self, e):
        # Reflow the two visual tracks to the actual visible timeline height.
        # This prevents the splitter from leaving a large blank viewport or a
        # zero-height canvas while still allowing a compact timeline.
        h = max(124, self.height())
        self.ruler_h = 28
        self.track_top = 34
        self.track_gap = 4
        available = max(62, h - self.track_top - 18)
        self.video_h = max(40, int(available * 0.66))
        self.wave_h = max(20, available - self.video_h - self.track_gap)
        self.track_h = self.video_h + self.track_gap + self.wave_h
        super().resizeEvent(e)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoomRequest.emit(
                1.20 if e.angleDelta().y() > 0 else 1 / 1.20,
                self.ms_for_x(e.position().x())
            )
            e.accept()
            return
        # Plain wheel = horizontal navigation, familiar in editor timelines.
        if e.angleDelta().y():
            self.panRequest.emit(-int(e.angleDelta().y() * 0.85))
            e.accept()
            return
        super().wheelEvent(e)

    def mousePressEvent(self, e):
        # Middle mouse is always a temporary pan override.
        if e.button() == Qt.MiddleButton:
            self.drag_mode = 'pan'
            self.pan_last = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        if e.button() != Qt.LeftButton:
            return

        # Hand tool: left-drag anywhere in the timeline pans the viewport. It
        # never selects, trims, seeks, or moves a cut point.
        if self.tool_mode == 'hand':
            self.drag_mode = 'pan'
            self.pan_last = e.position()
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return

        s, edge = self.hit(e.position())
        if s and edge and not s.deleted:
            self.drag_mode = 'trim'
            self.drag_uid = s.uid
            self.drag_edge = edge
            self.setCursor(Qt.SizeHorCursor)
            return

        # Left-click anywhere in the ruler/track starts as a pending gesture.
        # A simple click seeks/selects; crossing the drag threshold becomes a
        # marquee.  When the gesture starts in the ruler, the marquee spans the
        # full track height so horizontal ruler drags can select clips.
        self.press_pos = e.position()
        self.press_uid = s.uid if s and not s.deleted else None
        self.press_from_ruler = e.position().y() < self.track_top
        mods = e.modifiers()
        self.press_mode = (
            'toggle' if mods & Qt.ControlModifier
            else ('range' if mods & Qt.ShiftModifier else 'replace')
        )

        # Grabbing the visible playhead keeps conventional scrub behavior.
        if self.press_from_ruler and abs(e.position().x() - self.x_for_ms(self.position)) <= 7:
            self.drag_mode = 'scrub'
            self.seek.emit(self.ms_for_x(e.position().x()))
        else:
            self.drag_mode = 'pending'

    def mouseMoveEvent(self, e):
        if self.drag_mode == 'pan' and self.pan_last is not None:
            dx = int(e.position().x() - self.pan_last.x())
            self.pan_last = e.position()
            self.panRequest.emit(-dx)
            return
        if self.drag_mode == 'trim':
            raw = self.ms_for_x(e.position().x())
            self.trimPreview.emit(self.drag_uid, self.drag_edge, raw if e.modifiers() & Qt.AltModifier else self.snap_ms(raw))
            return
        if self.drag_mode == 'scrub':
            raw = self.ms_for_x(e.position().x())
            self.seek.emit(raw if e.modifiers() & Qt.AltModifier else self.snap_ms(raw))
            return
        if self.drag_mode == 'pending' and self.press_pos is not None:
            if (e.position() - self.press_pos).manhattanLength() > 5:
                self.drag_mode = 'marquee'
                self.marquee = self._marquee_rect(e.position())
                self.update()
                return
        if self.drag_mode == 'marquee':
            self.marquee = self._marquee_rect(e.position())
            self.update()
            return

        self._set_idle_cursor(e.position())

    def mouseReleaseEvent(self, e):
        if self.drag_mode == 'pan' and e.button() in (Qt.MiddleButton, Qt.LeftButton):
            self.drag_mode = None
            self.pan_last = None
            self._set_idle_cursor(e.position())
            e.accept()
            return

        if self.drag_mode == 'trim':
            raw = self.ms_for_x(e.position().x())
            self.trimCommit.emit(self.drag_uid, self.drag_edge, raw if e.modifiers() & Qt.AltModifier else self.snap_ms(raw))
        elif self.drag_mode == 'pending':
            if self.press_uid is not None and not self.press_from_ruler:
                self.select.emit(self.press_uid, self.press_mode)
            else:
                # Ruler click remains a familiar seek action; only a drag
                # crossing the threshold becomes selection.
                if not (e.modifiers() & Qt.ControlModifier):
                    self.marqueeSelect.emit([], 'replace')
                self.seek.emit(self.ms_for_x(e.position().x()))
        elif self.drag_mode == 'marquee' and self.marquee is not None:
            hits = [
                s.uid for s in self.model.segments
                if not s.deleted and self.segment_rect(s).intersects(self.marquee)
            ]
            self.marqueeSelect.emit(hits, 'add' if self.press_mode == 'toggle' else 'replace')

        self.drag_mode = None
        self.drag_uid = None
        self.drag_edge = None
        self.press_pos = None
        self.press_uid = None
        self.press_from_ruler = False
        self.marquee = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.tool_mode == 'select' and e.button() == Qt.LeftButton:
            self.split.emit(self.ms_for_x(e.position().x()))

    def contextMenuEvent(self, e):
        s, _ = self.hit(e.pos())
        ms = self.ms_for_x(e.pos().x())
        menu = QMenu(self)

        if s and s.deleted:
            a_restore = menu.addAction('恢复此区域')
            menu.addSeparator()
            a_in = menu.addAction('设置入点')
            a_out = menu.addAction('设置出点')
            a_mark = menu.addAction('添加 / 删除标记')
            menu.addSeparator()
            a_fit = menu.addAction('适应时间轴')
            chosen = menu.exec(e.globalPos())
            if chosen == a_restore: self.window().restore_gap_at(ms)
            elif chosen == a_in: self.window().set_in_point_at(ms)
            elif chosen == a_out: self.window().set_out_point_at(ms)
            elif chosen == a_mark: self.window().toggle_marker_at(ms)
            elif chosen == a_fit: self.window().fit_timeline()
            return

        # Right-clicking an active clip also makes it the current selection unless it is already selected.
        if s and s.uid not in self.model.selected:
            self.select.emit(s.uid, 'replace')

        a_split = menu.addAction('切开')
        a_split.setShortcut(QKeySequence('S'))
        a_uncut = menu.addAction('删除此处切点 / 取消切分')
        a_merge = menu.addAction('合并选中片段')
        a_merge.setShortcut(QKeySequence('G'))
        a_merge.setEnabled(len(self.model.selected_active()) >= 2)
        menu.addSeparator()
        a_left = menu.addAction('保留左侧'); a_left.setShortcut(QKeySequence('Q'))
        a_right = menu.addAction('保留右侧'); a_right.setShortcut(QKeySequence('W'))
        a_delete = menu.addAction('删除选中片段'); a_delete.setShortcut(QKeySequence('Delete'))
        a_delete.setEnabled(bool(self.model.selected_active()))
        menu.addSeparator()
        a_in = menu.addAction('设置入点'); a_in.setShortcut(QKeySequence('I'))
        a_out = menu.addAction('设置出点'); a_out.setShortcut(QKeySequence('O'))
        a_mark = menu.addAction('添加 / 删除标记'); a_mark.setShortcut(QKeySequence('M'))
        menu.addSeparator()
        a_export_current = menu.addAction('导出当前片段')
        a_export_selected = menu.addAction(f'导出选中片段（{len(self.model.selected_active())}）')
        a_export_selected.setShortcut(QKeySequence('Ctrl+Shift+E'))
        a_export_selected.setEnabled(bool(self.model.selected_active()))
        a_export_all = menu.addAction('导出全部片段'); a_export_all.setShortcut(QKeySequence('Ctrl+E'))
        menu.addSeparator()
        a_all = menu.addAction('全选'); a_all.setShortcut(QKeySequence('Ctrl+A'))
        a_clear = menu.addAction('取消选择'); a_clear.setShortcut(QKeySequence('Esc'))
        menu.addSeparator()
        a_fit = menu.addAction('适应时间轴'); a_fit.setShortcut(QKeySequence('F'))

        chosen = menu.exec(e.globalPos())
        w = self.window()
        if chosen == a_split: self.split.emit(ms)
        elif chosen == a_uncut: w.remove_cut_at(ms)
        elif chosen == a_merge: w.merge_selected()
        elif chosen == a_left: w.keep_left_at(ms)
        elif chosen == a_right: w.keep_right_at(ms)
        elif chosen == a_delete: w.delete_selected()
        elif chosen == a_in: w.set_in_point_at(ms)
        elif chosen == a_out: w.set_out_point_at(ms)
        elif chosen == a_mark: w.toggle_marker_at(ms)
        elif chosen == a_export_current: w.export_segment_at(ms)
        elif chosen == a_export_selected: w.export(False)
        elif chosen == a_export_all: w.export(True)
        elif chosen == a_all: w.select_all()
        elif chosen == a_clear: w.clear_selection()
        elif chosen == a_fit: w.fit_timeline()

    def _choose_ruler_step(self):
        # Aim for 82–150 px between labeled major ticks.
        candidates = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200]
        for s in candidates:
            if s * self.px_per_sec >= 82:
                return s
        return candidates[-1]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(C['app']))

        if self.duration_ms <= 1:
            p.setPen(QColor(C['muted_2']))
            p.setFont(QFont('Segoe UI', 10))
            p.drawText(self.rect(), Qt.AlignCenter, '打开或拖入视频后显示时间轴')
            return

        # Ruler background and baseline.
        p.fillRect(QRectF(0, 0, self.width(), self.ruler_h), QColor('#0D1218'))
        p.setPen(QPen(QColor(C['line']), 1))
        p.drawLine(0, self.ruler_h, self.width(), self.ruler_h)

        step = self._choose_ruler_step()
        minor = max(step / 5.0, 0.02)
        end = self.duration_ms / 1000.0

        # Minor ticks.
        t = 0.0
        while t <= end + 1e-6:
            x = self.x_for_ms(t * 1000)
            p.setPen(QPen(QColor('#35404D'), 1))
            p.drawLine(int(x), self.ruler_h - 7, int(x), self.ruler_h)
            t += minor

        # Major ticks + labels.
        p.setFont(QFont('Segoe UI', 8))
        t = 0.0
        while t <= end + 1e-6:
            x = self.x_for_ms(t * 1000)
            p.setPen(QPen(QColor('#667283'), 1))
            p.drawLine(int(x), self.ruler_h - 13, int(x), self.ruler_h)
            p.setPen(QColor('#AAB3BF'))
            p.drawText(QRectF(x - 42, 5, 84, 18), Qt.AlignCenter, fmt_ruler(t * 1000))
            t += step

        track_w = max(2, self.x_for_ms(self.duration_ms) - self.margin)
        video_track = QRectF(self.margin, self.track_top, track_w, self.video_h)
        wave_track = QRectF(self.margin, self.track_top + self.video_h + self.track_gap, track_w, self.wave_h)
        track = QRectF(self.margin, self.track_top, track_w, self.track_h)

        p.setBrush(QColor('#111821'))
        p.setPen(QPen(QColor('#283340'), 1))
        p.drawRoundedRect(video_track, 7, 7)
        p.setBrush(QColor('#0D141C'))
        p.setPen(QPen(QColor('#202A35'), 1))
        p.drawRoundedRect(wave_track, 6, 6)

        # Thumbnail filmstrip stays inside the video track only.
        if self.thumbs:
            p.save()
            p.setClipRect(video_track.adjusted(1, 1, -1, -1))
            n = len(self.thumbs)
            tw = video_track.width() / max(1, n)
            for i, pm in enumerate(self.thumbs):
                rr = QRectF(video_track.left() + i * tw, video_track.top(), tw + 1, video_track.height())
                p.drawPixmap(rr.toRect(), pm, pm.rect())
            p.fillRect(video_track, QColor(3, 7, 11, 52))
            p.restore()

        # Real source waveform. It shares the same source-time coordinate system.
        if not self.waveform.isNull():
            p.save()
            p.setOpacity(0.70)
            p.setClipRect(wave_track.adjusted(2, 2, -2, -2))
            p.drawPixmap(wave_track.toRect(), self.waveform, self.waveform.rect())
            p.restore()
        else:
            p.setPen(QPen(QColor('#263240'), 1))
            cy = int(wave_track.center().y())
            p.drawLine(int(wave_track.left()+4), cy, int(wave_track.right()-4), cy)

        active_no = 0
        swatches = ['#173B56', '#173B56', '#173B56', '#173B56']
        for s in self.model.segments:
            r = self.segment_rect(s)
            if s.deleted:
                p.setBrush(QColor('#090C10'))
                p.setPen(QPen(QColor('#343C47'), 1, Qt.DashLine))
                p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
                if r.width() > 48:
                    p.setPen(QColor('#606A78'))
                    p.setFont(QFont('Segoe UI', 8))
                    p.drawText(r, Qt.AlignCenter, '已删除')
                continue

            active_no += 1
            selected = s.uid in self.model.selected
            marquee_hit = self.marquee is not None and self.segment_rect(s).intersects(self.marquee)
            visual_selected = selected or marquee_hit
            base = QColor(swatches[(active_no - 1) % len(swatches)])
            base.setAlpha(54 if visual_selected else 18)
            p.setBrush(base)
            p.setPen(QPen(QColor(C['accent'] if visual_selected else '#3A4654'), 1.5 if visual_selected else 1))
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 7, 7)

            # Trim handles appear as proper editor affordances when selected.
            if selected and r.width() > 12:
                p.setBrush(QColor(C['accent']))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 28, 4, r.height() - 56), 2, 2)
                p.drawRoundedRect(QRectF(r.right() - 5, r.top() + 28, 4, r.height() - 56), 2, 2)

            if r.width() > 62:
                p.setPen(QColor(C['text']))
                p.setFont(QFont('Segoe UI', 9, QFont.DemiBold))
                p.drawText(r.adjusted(10, 8, -10, -8), Qt.AlignLeft | Qt.AlignTop, f'{active_no:02d}')
                p.setFont(QFont('Segoe UI', 8))
                p.setPen(QColor('#D7DDE5'))
                p.drawText(
                    r.adjusted(10, 8, -10, -8),
                    Qt.AlignLeft | Qt.AlignBottom,
                    fmt_ms(s.duration_ms).split('.')[0],
                )

        # Safe keyframe hints are intentionally subtle, not editing constraints.
        for k in self.keyframes:
            x = self.x_for_ms(k)
            p.setPen(QPen(QColor(86, 212, 154, 76), 1))
            p.drawLine(int(x), self.track_top + 3, int(x), self.track_top + 10)

        # In/Out range and markers are editor guides, never destructive by themselves.
        if self.in_point is not None and self.out_point is not None and self.out_point > self.in_point:
            x1, x2 = self.x_for_ms(self.in_point), self.x_for_ms(self.out_point)
            p.fillRect(QRectF(x1, self.ruler_h, x2-x1, self.track_top+self.track_h-self.ruler_h), QColor(45,156,255,18))
            p.setPen(QPen(QColor(45,156,255,120), 1, Qt.DashLine))
            p.drawLine(int(x1), self.ruler_h, int(x1), self.track_top+self.track_h)
            p.drawLine(int(x2), self.ruler_h, int(x2), self.track_top+self.track_h)

        for m in self.markers:
            mx = self.x_for_ms(m)
            p.setPen(QPen(QColor('#F2C94C'), 1))
            p.drawLine(int(mx), self.ruler_h, int(mx), self.track_top+self.track_h)
            p.setBrush(QColor('#F2C94C'))
            p.setPen(Qt.NoPen)
            p.drawPolygon([QPointF(mx-4,self.ruler_h+1), QPointF(mx+4,self.ruler_h+1), QPointF(mx,self.ruler_h+7)])

        # Playhead.
        x = self.x_for_ms(self.position)
        p.setPen(QPen(QColor(C['accent']), 2))
        p.drawLine(int(x), self.ruler_h - 1, int(x), self.track_top + self.track_h + 12)
        p.setBrush(QColor(C['accent']))
        p.setPen(Qt.NoPen)
        p.drawPolygon([QPointF(x - 5, self.ruler_h - 1), QPointF(x + 5, self.ruler_h - 1), QPointF(x, self.ruler_h + 6)])

        # Floating playhead time.
        p.setPen(QColor(C['accent']))
        p.setFont(QFont('Consolas', 8, QFont.DemiBold))
        p.drawText(QRectF(x - 50, self.track_top + self.track_h + 9, 100, 18), Qt.AlignCenter, fmt_ms(self.position))

        if self.marquee is not None:
            p.setBrush(QColor(45, 156, 255, 34))
            p.setPen(QPen(QColor(C['accent']), 1))
            p.drawRoundedRect(self.marquee, 4, 4)


class ShortcutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('快捷键')
        self.resize(720, 500)
        self.setObjectName('shortcutDialog')
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 24)
        root.setSpacing(16)
        title = QLabel('快捷键')
        title.setObjectName('dialogTitle')
        sub = QLabel('常用操作保持与成熟桌面剪辑器一致。')
        sub.setObjectName('dialogSub')
        root.addWidget(title)
        root.addWidget(sub)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(8)
        groups = [
            ('编辑', [('分割', 'S / Ctrl+B'), ('保留左侧', 'Q'), ('保留右侧', 'W'), ('删除', 'Delete'), ('撤销', 'Ctrl+Z'), ('重做', 'Ctrl+Shift+Z')]),
            ('播放', [('播放 / 暂停', 'Space'), ('倒放 / 停止 / 正放', 'J / K / L'), ('细移', '← / →'), ('大步移动', 'Shift+← / →'), ('上 / 下一切点', '↑ / ↓'), ('开头 / 结尾', 'Home / End')]),
            ('选择', [('全选', 'Ctrl+A'), ('取消选择', 'Esc'), ('追加选择', 'Ctrl+点击'), ('范围选择', 'Shift+点击'), ('框选', 'V + 左键拖动')]),
            ('时间轴', [('鼠标位置缩放', 'Ctrl+滚轮'), ('选择 / 手型工具', 'V / H'), ('水平平移', 'H+左键拖动 / 中键拖动'), ('平移视野', 'Alt+← / →'), ('入点 / 出点', 'I / O'), ('标记', 'M'), ('适应时间轴', 'F'), ('放大 / 缩小', '+ / -')]),
        ]
        for col, (name, rows) in enumerate(groups):
            card = QFrame()
            card.setObjectName('shortcutCard')
            lay = QVBoxLayout(card)
            lay.setContentsMargins(16, 14, 16, 14)
            lay.setSpacing(8)
            h = QLabel(name)
            h.setObjectName('shortcutGroup')
            lay.addWidget(h)
            for label, key in rows:
                row = QHBoxLayout()
                l = QLabel(label)
                l.setObjectName('shortcutLabel')
                k = QLabel(key)
                k.setObjectName('keycap')
                row.addWidget(l)
                row.addStretch()
                row.addWidget(k)
                lay.addLayout(row)
            lay.addStretch()
            grid.addWidget(card, col // 2, col % 2)
        root.addLayout(grid, 1)
        close = QPushButton('完成')
        close.setObjectName('secondary')
        close.setFixedWidth(88)
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        root.addLayout(row)


class MainWindow(QMainWindow):
    def __init__(self, base_dir):
        super().__init__()
        self.base_dir = Path(base_dir)
        self.ffmpeg = (
            str(self.base_dir / 'bin' / 'ffmpeg.exe') if os.name == 'nt'
            else (subprocess.getoutput('command -v ffmpeg') or 'ffmpeg')
        )
        self.ffprobe = (
            str(self.base_dir / 'bin' / 'ffprobe.exe') if os.name == 'nt'
            else (subprocess.getoutput('command -v ffprobe') or 'ffprobe')
        )

        self.file_path = None
        self.file_size = 0
        self.duration_ms = 0
        self.keyframes = []
        self.model = SegmentModel()
        self.history = History()
        self.settings = QSettings('LosslessSlicer', 'LosslessSlicer')
        self.pool = QThreadPool.globalInstance()
        self._trim_snapshot = None
        self.media_info = {}
        self.export_mode = 'lossless'
        self.thumb_paths = []
        self.marker_positions = []
        self.in_point = None
        self.out_point = None
        self._shuttle_reverse_rate = 0
        self._shuttle_forward_rate = 1
        self.reverse_timer = QTimer(self)
        self.reverse_timer.setInterval(40)
        self.reverse_timer.timeout.connect(self.reverse_tick)

        self.setWindowTitle('无损视频切片器')
        self.resize(1540, 940)
        self.setMinimumSize(1180, 760)
        self.setAcceptDrops(True)
        icon = self.base_dir / 'assets' / 'icon.png'
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        self.build_ui()
        self.build_menus()
        self.bind_shortcuts()
        self.apply_style()
        if os.name == 'nt':
            QTimer.singleShot(0, self.enable_windows_dark_titlebar)

    def enable_windows_dark_titlebar(self):
        """Ask Windows 10/11 DWM for a dark native title bar; fail silently elsewhere."""
        if os.name != 'nt':
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            value = ctypes.c_int(1)
            dwm = ctypes.windll.dwmapi
            # Windows 11 uses 20; some Windows 10 builds use 19.
            for attr in (20, 19):
                try:
                    if dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                        break
                except Exception:
                    pass
        except Exception:
            pass

    # ----- small UI factories -------------------------------------------------
    def icon(self, name):
        return QIcon(str(self.base_dir / 'assets' / 'icons' / f'{name}.svg'))

    def tool(self, name, tip, fn, *, size=34, icon_size=18, object_name='tool'):
        b = QToolButton()
        b.setObjectName(object_name)
        b.setIcon(self.icon(name))
        b.setIconSize(QSize(icon_size, icon_size))
        b.setFixedSize(size, size)
        b.setToolTip(tip)
        # Qt's clicked(bool) signal can leak the checked flag into Python
        # callbacks whose first argument is optional (for example keep_left_at(ms=None)).
        # Always discard the signal payload here; toolbar commands are command-style.
        b.clicked.connect(lambda _checked=False, _fn=fn: _fn())
        return b

    def divider(self, vertical=True):
        f = QFrame()
        f.setObjectName('divider')
        if vertical:
            f.setFixedSize(1, 22)
        else:
            f.setFixedHeight(1)
        return f

    def section_title(self, text, sub=None):
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        t = QLabel(text)
        t.setObjectName('sectionTitle')
        lay.addWidget(t)
        if sub:
            s = QLabel(sub)
            s.setObjectName('sectionSub')
            lay.addWidget(s)
        return box

    # ----- layout -------------------------------------------------------------
    def build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)

        # Command header – deliberately sparse.
        top = QFrame()
        top.setObjectName('topBar')
        top.setFixedHeight(52)
        tl = QHBoxLayout(top)
        tl.setContentsMargins(16, 0, 16, 0)
        tl.setSpacing(10)

        brand_icon = QLabel()
        if (self.base_dir / 'assets' / 'icon.png').exists():
            brand_icon.setPixmap(QPixmap(str(self.base_dir / 'assets' / 'icon.png')).scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand_icon.setFixedSize(26, 26)
        tl.addWidget(brand_icon)
        brand = QLabel('无损视频切片器')
        brand.setObjectName('brand')
        tl.addWidget(brand)
        tl.addSpacing(12)

        self.open_btn = QPushButton('  打开视频')
        self.open_btn.setIcon(self.icon('open'))
        self.open_btn.setIconSize(QSize(17, 17))
        self.open_btn.setObjectName('secondary')
        self.open_btn.clicked.connect(self.open_dialog)
        tl.addWidget(self.open_btn)
        tl.addStretch()

        self.file_label = QLabel('未打开视频')
        self.file_label.setObjectName('fileName')
        self.file_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tl.addWidget(self.file_label)
        tl.addSpacing(10)
        outer.addWidget(top)

        # Main work area.
        workspace = QSplitter(Qt.Horizontal)
        workspace.setObjectName('workspace')
        workspace.setChildrenCollapsible(False)

        # Left: segment browser.
        left = QFrame()
        left.setObjectName('leftPanel')
        ll = QVBoxLayout(left)
        ll.setContentsMargins(16, 16, 12, 14)
        ll.setSpacing(12)
        title_row = QHBoxLayout()
        title_row.addWidget(self.section_title('片段列表', '按源视频时间顺序'))
        title_row.addStretch()
        self.clip_count = QLabel('0')
        self.clip_count.setObjectName('countBadge')
        title_row.addWidget(self.clip_count)
        ll.addLayout(title_row)

        self.clip_list = ClipListWidget()
        self.clip_list.setObjectName('clipList')
        self.clip_list.setSpacing(7)
        self.clip_list.setIconSize(QSize(78, 48))
        self.clip_list.itemClicked.connect(self.clip_item_clicked)
        self.clip_list.itemSelectionChanged.connect(self.clip_selection_changed)
        self.clip_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.clip_list.customContextMenuRequested.connect(self.clip_list_context_menu)
        ll.addWidget(self.clip_list, 1)
        self.clip_empty = QLabel('切割后，片段会出现在这里')
        self.clip_empty.setObjectName('emptyMuted')
        self.clip_empty.setAlignment(Qt.AlignCenter)
        self.clip_empty.setWordWrap(True)
        ll.addWidget(self.clip_empty)
        left.setMinimumWidth(248)
        left.setMaximumWidth(320)
        workspace.addWidget(left)

        # Center: preview stage.
        center = QFrame()
        center.setObjectName('centerPanel')
        cl = QVBoxLayout(center)
        cl.setContentsMargins(14, 14, 14, 12)
        cl.setSpacing(10)

        preview_shell = QFrame()
        preview_shell.setObjectName('previewShell')
        pv = QVBoxLayout(preview_shell)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.setSpacing(0)
        self.preview_stack = QStackedWidget()
        self.preview_stack.setObjectName('previewStack')

        empty = DropPage()
        empty.fileDropped.connect(self.load_video)
        ev = QVBoxLayout(empty)
        ev.setContentsMargins(24, 24, 24, 24)
        ev.addStretch()
        drop_icon = QLabel()
        drop_icon.setPixmap(self.icon('open').pixmap(44, 44))
        drop_icon.setAlignment(Qt.AlignCenter)
        ev.addWidget(drop_icon)
        drop_title = QLabel('拖入视频开始')
        drop_title.setObjectName('dropTitle')
        drop_title.setAlignment(Qt.AlignCenter)
        ev.addWidget(drop_title)
        drop_sub = QLabel('也可以点击“打开视频” · 支持 MP4 / MOV / MKV / WEBM 等常见格式')
        drop_sub.setObjectName('dropSub')
        drop_sub.setAlignment(Qt.AlignCenter)
        drop_sub.setWordWrap(True)
        ev.addWidget(drop_sub)
        ev.addSpacing(8)
        choose = QPushButton('选择视频')
        choose.setObjectName('secondary')
        choose.setFixedWidth(108)
        choose.clicked.connect(self.open_dialog)
        choose_row = QHBoxLayout()
        choose_row.addStretch()
        choose_row.addWidget(choose)
        choose_row.addStretch()
        ev.addLayout(choose_row)
        ev.addStretch()
        self.preview_stack.addWidget(empty)

        self.video = VideoDropWidget()
        self.video.setObjectName('video')
        self.video.fileDropped.connect(self.load_video)
        self.preview_stack.addWidget(self.video)
        pv.addWidget(self.preview_stack)
        cl.addWidget(preview_shell, 1)

        # Playback strip.
        controls = QFrame()
        controls.setObjectName('playerControls')
        pc = QHBoxLayout(controls)
        pc.setContentsMargins(10, 5, 10, 5)
        pc.setSpacing(6)
        pc.addStretch()
        pc.addWidget(self.tool('prev', '上一切点  ↑', lambda: self.jump_cut(-1), size=32, icon_size=16))
        self.play_btn = self.tool('play', '播放 / 暂停  Space', self.toggle_play, size=36, icon_size=18, object_name='transportPrimary')
        pc.addWidget(self.play_btn)
        pc.addWidget(self.tool('next', '下一切点  ↓', lambda: self.jump_cut(1), size=32, icon_size=16))
        self.time_label = QLabel('00:00.000  /  00:00.000')
        self.time_label.setObjectName('timeCode')
        self.time_label.mouseDoubleClickEvent = lambda e: self.jump_time()
        pc.addWidget(self.time_label)
        pc.addStretch()
        vol_icon = QLabel()
        vol_icon.setPixmap(self.icon('volume').pixmap(16, 16))
        pc.addWidget(vol_icon)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setObjectName('volume')
        self.volume.setRange(0, 100)
        self.volume.setValue(75)
        self.volume.setFixedWidth(104)
        pc.addWidget(self.volume)
        pc.addSpacing(4)
        pc.addWidget(self.tool('fullscreen', '全屏预览', self.toggle_fullscreen, size=30, icon_size=16))
        cl.addWidget(controls)
        workspace.addWidget(center)

        # Right: export inspector.
        right = QFrame()
        right.setObjectName('rightPanel')
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 16, 16, 14)
        rl.setSpacing(12)
        rl.addWidget(self.section_title('导出', '独立片段 · 按源时间顺序'))

        self.export_info = QLabel('打开视频后显示导出信息')
        self.export_info.setObjectName('infoCard')
        self.export_info.setWordWrap(True)
        rl.addWidget(self.export_info)

        mode_card = QFrame()
        mode_card.setObjectName('settingsCard')
        mvl = QVBoxLayout(mode_card)
        mvl.setContentsMargins(12, 12, 12, 12)
        mvl.setSpacing(8)
        mode_title = QLabel('切割模式')
        mode_title.setObjectName('fieldLabel')
        mvl.addWidget(mode_title)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self.mode_lossless = QPushButton('无损')
        self.mode_lossless.setCheckable(True)
        self.mode_lossless.setChecked(True)
        self.mode_lossless.setObjectName('modeButton')
        self.mode_precise = QPushButton('精准')
        self.mode_precise.setCheckable(True)
        self.mode_precise.setObjectName('modeButton')
        self.mode_lossless.clicked.connect(lambda: self.set_export_mode('lossless'))
        self.mode_precise.clicked.connect(lambda: self.set_export_mode('precise'))
        mode_row.addWidget(self.mode_lossless)
        mode_row.addWidget(self.mode_precise)
        mvl.addLayout(mode_row)
        self.mode_hint = QLabel('纯 Stream Copy · 极速 · 切点在导出时扩展到安全关键帧')
        self.mode_hint.setObjectName('micro')
        self.mode_hint.setWordWrap(True)
        mvl.addWidget(self.mode_hint)
        rl.addWidget(mode_card)

        path_card = QFrame()
        path_card.setObjectName('settingsCard')
        path_card.setMinimumHeight(146)
        self.path_card = path_card
        pvl = QVBoxLayout(path_card)
        pvl.setContentsMargins(12, 12, 12, 12)
        pvl.setSpacing(9)
        label = QLabel('保存位置')
        label.setObjectName('fieldLabel')
        pvl.addWidget(label)
        pathrow = QHBoxLayout()
        pathrow.setSpacing(6)
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText('与源视频相同目录')
        pathrow.addWidget(self.out_dir, 1)
        browse = self.tool('folder', '选择保存位置', self.browse_output, size=34, icon_size=17)
        pathrow.addWidget(browse)
        pvl.addLayout(pathrow)

        self.prefix_on = QPushButton('  添加文件名前缀')
        self.prefix_on.setCheckable(True)
        self.prefix_on.setObjectName('optionButton')
        self.prefix_on.toggled.connect(self.prefix_toggle)
        pvl.addWidget(self.prefix_on)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText('例如：东京')
        self.prefix.hide()
        pvl.addWidget(self.prefix)
        rl.addWidget(path_card)

        preview_card = QFrame()
        preview_card.setObjectName('settingsCard')
        pv2 = QVBoxLayout(preview_card)
        pv2.setContentsMargins(12, 12, 12, 12)
        pv2.setSpacing(8)
        ph = QHBoxLayout()
        pl = QLabel('文件预览')
        pl.setObjectName('fieldLabel')
        ph.addWidget(pl)
        ph.addStretch()
        self.preview_count = QLabel('0 个文件')
        self.preview_count.setObjectName('micro')
        ph.addWidget(self.preview_count)
        pv2.addLayout(ph)
        self.name_preview = QLabel('001.mp4\n002.mp4\n003.mp4')
        self.name_preview.setObjectName('exportPreview')
        pv2.addWidget(self.name_preview)
        rl.addWidget(preview_card)
        rl.addStretch()

        self.side_export_selected = QPushButton('导出选中')
        self.side_export_selected.setObjectName('secondary')
        self.side_export_selected.clicked.connect(lambda: self.export(False))
        self.side_export_all = QPushButton('导出全部')
        self.side_export_all.setObjectName('primary')
        self.side_export_all.clicked.connect(lambda: self.export(True))
        rl.addWidget(self.side_export_selected)
        rl.addWidget(self.side_export_all)
        right.setMinimumWidth(286)
        right.setMaximumWidth(350)
        workspace.addWidget(right)

        workspace.setStretchFactor(1, 1)
        workspace.setSizes([270, 920, 310])
        # Timeline tool strip.  The workspace and timeline are joined by a
        # vertical splitter below so the user can freely resize timeline height.

        tb = QFrame()
        tb.setObjectName('timelineBar')
        bl = QHBoxLayout(tb)
        bl.setContentsMargins(12, 5, 12, 5)
        bl.setSpacing(3)

        self.select_tool_btn = self.tool('select-tool', '选择工具  V', lambda: self.set_timeline_tool('select'), object_name='modeTool')
        self.select_tool_btn.setCheckable(True)
        self.select_tool_btn.setAutoExclusive(True)
        self.hand_tool_btn = self.tool('hand-tool', '手型平移工具  H', lambda: self.set_timeline_tool('hand'), object_name='modeTool')
        self.hand_tool_btn.setCheckable(True)
        self.hand_tool_btn.setAutoExclusive(True)
        self.select_tool_btn.setChecked(True)
        bl.addWidget(self.select_tool_btn)
        bl.addWidget(self.hand_tool_btn)
        bl.addWidget(self.divider())
        bl.addWidget(self.tool('undo', '撤销  Ctrl+Z', self.undo))
        bl.addWidget(self.tool('redo', '重做  Ctrl+Shift+Z', self.redo))
        bl.addWidget(self.divider())
        bl.addWidget(self.tool('split', '分割  S / Ctrl+B', lambda: self.split_at(self.player.position())))
        bl.addWidget(self.tool('merge', '合并选中片段 / 取消切分  G', self.merge_selected))
        bl.addWidget(self.tool('keep-left', '保留左侧  Q', self.keep_left_at))
        bl.addWidget(self.tool('keep-right', '保留右侧  W', self.keep_right_at))
        bl.addWidget(self.tool('delete', '删除  Delete', self.delete_selected))
        bl.addWidget(self.divider())
        bl.addWidget(self.tool('prev', '上一切点  ↑', lambda: self.jump_cut(-1)))
        bl.addWidget(self.tool('next', '下一切点  ↓', lambda: self.jump_cut(1)))

        bl.addStretch()
        self.summary = QLabel('0 个片段')
        self.summary.setObjectName('summary')
        bl.addWidget(self.summary)
        bl.addSpacing(14)
        bl.addWidget(self.tool('minus', '缩小  -', lambda: self.step_zoom(1 / 1.25), size=30, icon_size=15))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setObjectName('zoom')
        self.zoom.setRange(1, 20000)
        self.zoom.setValue(420)
        self.zoom.setFixedWidth(142)
        self.zoom.valueChanged.connect(self.zoom_changed)
        bl.addWidget(self.zoom)
        bl.addWidget(self.tool('plus', '放大  +', lambda: self.step_zoom(1.25), size=30, icon_size=15))
        bl.addWidget(self.tool('fit', '适应时间轴  F', self.fit_timeline, size=32, icon_size=16))

        # Timeline viewport.
        timeline_shell = QFrame()
        timeline_shell.setObjectName('timelineShell')
        tsl = QVBoxLayout(timeline_shell)
        tsl.setContentsMargins(0, 0, 0, 0)
        tsl.setSpacing(0)
        self.canvas = TimelineCanvas(self.model)
        self.canvas.seek.connect(self.seek_to)
        self.canvas.split.connect(self.split_at)
        self.canvas.select.connect(self.select_segment)
        self.canvas.marqueeSelect.connect(self.select_many)
        self.canvas.trimPreview.connect(self.trim_preview)
        self.canvas.trimCommit.connect(self.trim_commit)
        self.canvas.zoomRequest.connect(self.zoom_at)
        self.canvas.panRequest.connect(self.pan_timeline)

        self.scroll = QScrollArea()
        self.scroll.setObjectName('timelineScroll')
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(False)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumHeight(124)
        tsl.addWidget(self.scroll)

        foot = QFrame()
        foot.setObjectName('timelineFooter')
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(14, 3, 14, 3)
        fl.setSpacing(8)
        self.notice = QLabel('拖入视频或点击“打开视频”')
        self.notice.setObjectName('notice')
        fl.addWidget(self.notice)
        fl.addStretch()
        helper = QLabel('V 选择 / H 手型平移   ·   中键临时平移   ·   Ctrl+滚轮缩放   ·   拖分隔线调整高度')
        helper.setObjectName('helper')
        fl.addWidget(helper)
        tsl.addWidget(foot)

        # Resizable editor split: the timeline can be made as thin or as tall as
        # the user wants, while preserving a small usable floor for its ruler,
        # video strip and waveform.  This intentionally has no reset/preset UI.
        timeline_panel = QFrame()
        timeline_panel.setObjectName('timelinePanel')
        tpl = QVBoxLayout(timeline_panel)
        tpl.setContentsMargins(0, 0, 0, 0)
        tpl.setSpacing(0)
        tpl.addWidget(tb)
        tpl.addWidget(timeline_shell, 1)
        timeline_panel.setMinimumHeight(194)

        self.editor_splitter = QSplitter(Qt.Vertical)
        self.editor_splitter.setObjectName('editorSplitter')
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.setHandleWidth(5)
        self.editor_splitter.addWidget(workspace)
        self.editor_splitter.addWidget(timeline_panel)
        self.editor_splitter.setStretchFactor(0, 1)
        self.editor_splitter.setStretchFactor(1, 0)
        # A slimmer default than QA3; users can immediately drag the separator.
        self.editor_splitter.setSizes([680, 210])
        self.editor_splitter.splitterMoved.connect(lambda _pos, _index: QTimer.singleShot(0, self.sync_timeline_canvas_height))
        outer.addWidget(self.editor_splitter, 1)
        QTimer.singleShot(0, self.sync_timeline_canvas_height)

        # Media engine.
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.75)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.player.positionChanged.connect(self.on_position)
        self.player.durationChanged.connect(self.on_duration)
        self.player.playbackStateChanged.connect(self.on_state)

        for w in (self.out_dir, self.prefix):
            w.textChanged.connect(self.update_export_preview)
        self.prefix_on.toggled.connect(self.update_export_preview)
        self.play_btn.setEnabled(False)
        self.refresh_all()

    # ----- menus / shortcuts --------------------------------------------------
    def build_menus(self):
        mb = self.menuBar()
        mb.setNativeMenuBar(False)
        f = mb.addMenu('文件')
        self.add_menu_action(f, '打开视频', 'Ctrl+O', self.open_dialog)
        f.addSeparator()
        self.add_menu_action(f, '导出选中', 'Ctrl+Shift+E', lambda: self.export(False))
        self.add_menu_action(f, '导出全部', 'Ctrl+E', lambda: self.export(True))
        f.addSeparator()
        self.add_menu_action(f, '退出', 'Alt+F4', self.close)

        e = mb.addMenu('编辑')
        self.add_menu_action(e, '撤销', 'Ctrl+Z', self.undo)
        self.add_menu_action(e, '重做', 'Ctrl+Shift+Z', self.redo)
        e.addSeparator()
        self.add_menu_action(e, '分割', 'S', lambda: self.split_at(self.player.position()))
        self.add_menu_action(e, '合并选中片段 / 取消切分', 'G', self.merge_selected)
        self.add_menu_action(e, '保留左侧', 'Q', self.keep_left_at)
        self.add_menu_action(e, '保留右侧', 'W', self.keep_right_at)
        self.add_menu_action(e, '删除', 'Delete', self.delete_selected)
        self.add_menu_action(e, '删除入点到出点', 'Shift+Delete', self.delete_in_out)
        self.add_menu_action(e, '仅保留入点到出点', 'Ctrl+Shift+K', self.keep_in_out)
        e.addSeparator()
        self.add_menu_action(e, '全选', 'Ctrl+A', self.select_all)
        self.add_menu_action(e, '取消选择', 'Esc', self.clear_selection)

        p = mb.addMenu('播放')
        self.add_menu_action(p, '播放 / 暂停', 'Space', self.toggle_play)
        p.addSeparator()
        self.add_menu_action(p, '上一切点', 'Up', lambda: self.jump_cut(-1))
        self.add_menu_action(p, '下一切点', 'Down', lambda: self.jump_cut(1))
        self.add_menu_action(p, '视频开头', 'Home', lambda: self.seek_to(0))
        self.add_menu_action(p, '视频结尾', 'End', lambda: self.seek_to(self.duration_ms))
        p.addSeparator()
        self.add_menu_action(p, '倒放 / 加速倒放', 'J', self.shuttle_j)
        self.add_menu_action(p, '暂停', 'K', self.shuttle_k)
        self.add_menu_action(p, '播放 / 加速播放', 'L', self.shuttle_l)

        t = mb.addMenu('时间轴')
        self.add_menu_action(t, '选择工具', 'V', lambda: self.set_timeline_tool('select'))
        self.add_menu_action(t, '手型平移工具', 'H', lambda: self.set_timeline_tool('hand'))
        t.addSeparator()
        self.add_menu_action(t, '放大', '+', lambda: self.step_zoom(1.25))
        self.add_menu_action(t, '缩小', '-', lambda: self.step_zoom(1 / 1.25))
        self.add_menu_action(t, '适应时间轴', 'F', self.fit_timeline)
        t.addSeparator()
        self.add_menu_action(t, '向左平移', 'Alt+Left', lambda: self.pan_timeline(-120))
        self.add_menu_action(t, '向右平移', 'Alt+Right', lambda: self.pan_timeline(120))
        t.addSeparator()
        self.add_menu_action(t, '设置入点', 'I', self.set_in_point)
        self.add_menu_action(t, '设置出点', 'O', self.set_out_point)
        self.add_menu_action(t, '添加 / 删除标记', 'M', self.toggle_marker)
        self.add_menu_action(t, '清除入点 / 出点', 'Ctrl+Shift+X', self.clear_in_out)

        h = mb.addMenu('帮助')
        a = h.addAction('快捷键')
        a.triggered.connect(self.show_shortcuts)

    def add_menu_action(self, menu, text, key, fn):
        a = QAction(text, self)
        a.setShortcut(QKeySequence(key))
        # QAction.triggered(bool) has the same optional-argument trap as QToolButton.clicked.
        # Commands in this UI do not consume QAction's checked state.
        a.triggered.connect(lambda _checked=False, _fn=fn: _fn())
        menu.addAction(a)
        return a

    def bind_shortcuts(self):
        def act(keys, fn):
            for k in keys:
                a = QAction(self)
                a.setShortcut(QKeySequence(k))
                a.triggered.connect(lambda _checked=False, _fn=fn: _fn())
                self.addAction(a)

        act(['Ctrl+B'], lambda: self.split_at(self.player.position()))
        act(['Ctrl+Y'], self.redo)
        act(['Right'], lambda: self.seek_to(self.player.position() + 40))
        act(['Left'], lambda: self.seek_to(self.player.position() - 40))
        act(['Shift+Right'], lambda: self.seek_to(self.player.position() + 5000))
        act(['Shift+Left'], lambda: self.seek_to(self.player.position() - 5000))

    # ----- visual system ------------------------------------------------------
    def apply_style(self):
        self.setStyleSheet(f'''
        QMainWindow, QWidget {{
            background: {C['app']};
            color: {C['text']};
            font-family: "Segoe UI";
            font-size: 12px;
        }}

        QMenuBar {{
            background: #0B0F14;
            border-bottom: 1px solid {C['line_soft']};
            padding: 2px 10px;
            min-height: 27px;
        }}
        QMenuBar::item {{ padding: 5px 10px; border-radius: 5px; color: {C['text_2']}; }}
        QMenuBar::item:selected {{ background: {C['hover']}; color: {C['text']}; }}
        QMenu {{ background: #151B23; border: 1px solid #303946; padding: 6px; border-radius: 8px; }}
        QMenu::item {{ padding: 8px 30px 8px 12px; border-radius: 5px; }}
        QMenu::item:selected {{ background: #243243; }}
        QMenu::separator {{ height: 1px; background: {C['line']}; margin: 5px 7px; }}

        QSplitter::handle {{ background: {C['line_soft']}; width: 1px; }}
        QSplitter#editorSplitter::handle:vertical {{
            height: 5px; background: #1B232D; border-top: 1px solid #2A3440;
            border-bottom: 1px solid #0B0F14;
        }}
        QSplitter#editorSplitter::handle:vertical:hover {{ background: #344353; }}
        QFrame#topBar {{ background: {C['top']}; border-bottom: 1px solid {C['line']}; }}
        QLabel#brand {{ font-size: 15px; font-weight: 650; color: {C['text']}; }}
        QLabel#fileName {{ color: {C['muted']}; }}

        QFrame#leftPanel, QFrame#rightPanel {{ background: {C['panel']}; }}
        QFrame#leftPanel {{ border-right: 1px solid {C['line']}; }}
        QFrame#rightPanel {{ border-left: 1px solid {C['line']}; }}
        QFrame#centerPanel {{ background: #0C1015; }}
        QLabel#sectionTitle {{ font-size: 14px; font-weight: 650; color: {C['text']}; }}
        QLabel#sectionSub {{ font-size: 10px; color: {C['muted']}; }}
        QLabel#countBadge {{
            color: {C['text_2']}; background: #1A212B; border: 1px solid #2B3440;
            border-radius: 10px; padding: 2px 7px; font-size: 10px;
        }}
        QLabel#emptyMuted {{ color: {C['muted_2']}; padding: 14px 18px; }}

        QFrame#previewShell {{
            background: {C['black']};
            border: 1px solid #1F2731;
            border-radius: 10px;
        }}
        QStackedWidget#previewStack, QVideoWidget#video {{ background: {C['black']}; border-radius: 10px; }}
        QLabel#dropTitle {{ font-size: 19px; font-weight: 650; color: #E9EEF5; margin-top: 8px; }}
        QLabel#dropSub {{ color: {C['muted']}; font-size: 11px; }}
        QFrame#playerControls {{ background: transparent; }}

        QPushButton {{
            min-height: 32px;
            padding: 0 13px;
            background: #1A212B;
            border: 1px solid #303946;
            border-radius: 7px;
            color: {C['text_2']};
        }}
        QPushButton:hover {{ background: #222B37; border-color: #465261; color: {C['text']}; }}
        QPushButton:pressed {{ background: #151B23; }}
        QPushButton#primary {{ background: #2188D8; border-color: #2F9BEA; color: white; font-weight: 650; }}
        QPushButton#primary:hover {{ background: #2998EB; }}
        QPushButton#secondary {{ background: #171D25; }}
        QPushButton#optionButton {{ text-align: left; background: #151C24; }}
        QPushButton#optionButton:checked {{ background: #173047; border-color: #2D75A9; color: #E7F5FF; }}
        QPushButton#modeButton {{ min-height: 30px; padding: 0 10px; background: #10161D; color: #8F9AA8; border: 1px solid #293441; }}
        QPushButton#modeButton:checked {{ background: #17334D; border-color: #2D9CFF; color: #F4F7FB; font-weight: 650; }}
        QPushButton#modeButton:hover {{ border-color: #46586A; color: #F4F7FB; }}

        QToolButton#tool {{ background: transparent; border: 0; border-radius: 7px; padding: 0; }}
        QToolButton#tool:hover {{ background: {C['hover']}; }}
        QToolButton#tool:pressed {{ background: #151B23; }}
        QToolButton#modeTool {{ background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 0; }}
        QToolButton#modeTool:hover {{ background: {C['hover']}; border-color: #303B48; }}
        QToolButton#modeTool:checked {{ background: #17334D; border-color: {C['accent']}; }}
        QToolButton#transportPrimary {{
            background: #1A222C; border: 1px solid #303B48; border-radius: 8px; padding: 0;
        }}
        QToolButton#transportPrimary:hover {{ background: #25313E; border-color: #46586A; }}
        QToolButton#transportPrimary:pressed {{ background: #121820; }}
        QToolButton#transportPrimary:disabled {{ background: #131922; border-color: #202A34; opacity: 0.55; }}

        QLabel#timeCode {{
            font-family: "Cascadia Mono", "Consolas";
            font-size: 12px; color: #D8DEE7; padding: 0 11px;
        }}

        QListWidget#clipList {{ background: transparent; border: 0; outline: 0; }}
        QListWidget#clipList::item {{
            background: #151B23;
            border: 1px solid #242D38;
            border-radius: 8px;
            padding: 10px 11px;
            min-height: 54px;
            color: #C6CED8;
        }}
        QListWidget#clipList::item:hover {{ background: #1A222D; border-color: #34404E; }}
        QListWidget#clipList::item:selected {{ background: #162F44; border: 1px solid {C['accent']}; color: white; }}

        QFrame#settingsCard {{ background: #121820; border: 1px solid #242D38; border-radius: 9px; }}
        QLabel#fieldLabel {{ color: #C7CFD9; font-weight: 600; }}
        QLabel#infoCard {{
            background: #111A22; border: 1px solid #213342; border-radius: 9px;
            color: #9EB0BF; padding: 10px 12px;
        }}
        QLabel#exportPreview {{
            background: #0D1218; border: 1px solid #222B36; border-radius: 7px;
            color: #B7C0CB; padding: 9px 10px; font-family: "Cascadia Mono", "Consolas";
        }}
        QLabel#micro {{ font-size: 10px; color: {C['muted']}; }}

        QLineEdit {{
            min-height: 32px; background: #0D1218; border: 1px solid #303946;
            border-radius: 7px; padding: 0 9px; color: #E5EAF0;
        }}
        QLineEdit:focus {{ border-color: {C['accent']}; }}
        QLineEdit::placeholder {{ color: #657080; }}

        QCheckBox#toggleCheck {{ spacing: 8px; color: #BDC6D1; }}
        QCheckBox#toggleCheck::indicator {{ width: 30px; height: 16px; border-radius: 8px; background: #2A333F; border: 1px solid #394554; }}
        QCheckBox#toggleCheck::indicator:checked {{ background: #1D75B5; border-color: #2E9BE7; }}

        QFrame#timelineBar {{ background: #10151C; border-top: 1px solid {C['line']}; border-bottom: 1px solid {C['line']}; }}
        QFrame#divider {{ background: #2A3340; margin: 0 7px; }}
        QLabel#summary {{ color: #8F9AA8; font-size: 11px; }}
        QFrame#timelineShell {{ background: #0B0F14; }}
        QScrollArea#timelineScroll {{ border: 0; background: #0B0F14; }}
        QFrame#timelineFooter {{ background: #0C1015; border-top: 1px solid #1D252F; }}
        QLabel#notice {{ color: #7E8998; font-size: 10px; }}
        QLabel#helper {{ color: #5D6877; font-size: 10px; }}

        QSlider#zoom::groove:horizontal, QSlider#volume::groove:horizontal {{
            height: 3px; background: #303A47; border-radius: 1px;
        }}
        QSlider#zoom::sub-page:horizontal, QSlider#volume::sub-page:horizontal {{ background: #607083; border-radius: 1px; }}
        QSlider#zoom::handle:horizontal, QSlider#volume::handle:horizontal {{
            width: 12px; height: 12px; margin: -5px 0; background: #D8E0E8;
            border: 2px solid #11161D; border-radius: 7px;
        }}
        QSlider#zoom::handle:horizontal:hover, QSlider#volume::handle:horizontal:hover {{ background: {C['accent']}; }}

        QScrollBar:horizontal {{ height: 9px; background: #0A0E13; margin: 1px 2px 1px 2px; }}
        QScrollBar::handle:horizontal {{ background: #394452; min-width: 54px; border-radius: 4px; }}
        QScrollBar::handle:horizontal:hover {{ background: #4B5867; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
        QScrollBar:vertical {{ width: 8px; background: transparent; }}
        QScrollBar::handle:vertical {{ background: #303946; min-height: 40px; border-radius: 4px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

        QToolTip {{ background: #202731; color: #EEF2F7; border: 1px solid #394452; padding: 5px 7px; }}

        QDialog#shortcutDialog {{ background: #0D1117; }}
        QLabel#dialogTitle {{ font-size: 20px; font-weight: 700; }}
        QLabel#dialogSub {{ color: {C['muted']}; }}
        QFrame#shortcutCard {{ background: #121820; border: 1px solid #27313D; border-radius: 9px; }}
        QLabel#shortcutGroup {{ font-size: 13px; font-weight: 650; color: #E9EDF3; padding-bottom: 4px; }}
        QLabel#shortcutLabel {{ color: #AEB8C5; }}
        QLabel#keycap {{ background: #202935; border: 1px solid #394555; border-radius: 5px; padding: 3px 7px; color: #DDE4EC; font-family: "Cascadia Mono", "Consolas"; font-size: 10px; }}
        ''')

    def closeEvent(self, e):
        if self.file_path and self.history.undo_stack:
            box = QMessageBox(self)
            box.setWindowTitle('退出无损视频切片器')
            box.setText('当前视频有尚未保存为文件的切割操作。')
            box.setInformativeText('退出不会修改原视频，但本次切割状态会丢失。')
            leave = box.addButton('退出', QMessageBox.DestructiveRole)
            box.addButton('继续编辑', QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() != leave:
                e.ignore()
                return
        e.accept()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, 'scroll') and hasattr(self, 'canvas'):
            self.canvas.set_min_content_width(max(200, self.scroll.viewport().width() // 4))
            QTimer.singleShot(0, self.sync_timeline_canvas_height)

    # ----- drag / open --------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(
            Path(u.toLocalFile()).suffix.lower() in VIDEO_EXTS for u in e.mimeData().urls()
        ):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if Path(p).suffix.lower() in VIDEO_EXTS:
                self.load_video(p)
                break

    def open_dialog(self):
        p, _ = QFileDialog.getOpenFileName(
            self,
            '打开视频',
            self.settings.value('last_open', ''),
            '视频 (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.ts *.m2ts *.mts);;所有文件 (*.*)'
        )
        if p:
            self.load_video(p)

    def load_video(self, path):
        try:
            info = probe_media(self.ffprobe, path)
        except Exception as exc:
            QMessageBox.critical(self, '无法读取视频', str(exc))
            return

        self.file_path = path
        self.media_info = info
        self.file_size = info['size']
        self.duration_ms = info['duration_ms']
        self.settings.setValue('last_open', str(Path(path).parent))
        self.model.reset(self.duration_ms)
        self.history.clear()
        self.keyframes = []
        self.canvas.set_keyframes([])
        self.canvas.set_thumbs([])
        self.canvas.set_waveform('')
        self.thumb_paths = []
        self.marker_positions = []
        self.in_point = None
        self.out_point = None
        self.canvas.set_markers([])
        self.canvas.set_in_out(None, None)
        self.canvas.set_duration(self.duration_ms)
        self.canvas.set_position(0)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.preview_stack.setCurrentIndex(1)
        self.play_btn.setEnabled(True)
        self.file_label.setText(Path(path).name)
        self.out_dir.setText(str(Path(path).parent))
        self.notice.setText('正在分析无损安全切点与缩略图…')
        self.refresh_all()
        self.fit_timeline()

        wk = FnWorker(lambda: probe_keyframes(self.ffprobe, path))
        wk.signals.done.connect(self.keyframes_ready)
        wk.signals.error.connect(lambda e: self.notice.setText('安全切点分析失败：' + e))
        self.pool.start(wk)

        wt = FnWorker(lambda: generate_thumbnails(self.ffmpeg, path, self.duration_ms, 96))
        wt.signals.done.connect(self.thumbs_ready)
        wt.signals.error.connect(lambda e: self.notice.setText('缩略图生成失败：' + e))
        self.pool.start(wt)

        ww = FnWorker(lambda: generate_waveform(self.ffmpeg, path, 2600, 92))
        ww.signals.done.connect(self.waveform_ready)
        ww.signals.error.connect(lambda e: self.notice.setText('波形生成失败：' + e))
        self.pool.start(ww)

    def keyframes_ready(self, k):
        self.keyframes = k
        self.canvas.set_keyframes(k)
        self.notice.setText(f'已分析 {len(k)} 个无损安全边界')

    def thumbs_ready(self, paths):
        self.thumb_paths = list(paths or [])
        self.canvas.set_thumbs(paths)
        self.notice.setText('缩略图已就绪')
        self.refresh_all()

    def waveform_ready(self, path):
        self.canvas.set_waveform(path)
        self.notice.setText('缩略图与音频波形已就绪')

    # ----- playback -----------------------------------------------------------
    def on_duration(self, ms):
        if ms > 0 and not self.duration_ms:
            self.duration_ms = ms
            self.model.reset(ms)
            self.canvas.set_duration(ms)
            self.refresh_all()

    def on_position(self, ms):
        self.canvas.set_position(ms)
        self.time_label.setText(f'{fmt_ms(ms)}  /  {fmt_ms(self.duration_ms)}')
        self.ensure_playhead_visible(ms)

    def on_state(self, state):
        name = 'pause' if state == QMediaPlayer.PlayingState else 'play'
        self.play_btn.setIcon(self.icon(name))

    def toggle_play(self):
        if not self.file_path:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def toggle_fullscreen(self):
        if not self.file_path:
            return
        self.video.setFullScreen(not self.video.isFullScreen())

    def seek_to(self, ms):
        if self.duration_ms:
            self.player.setPosition(max(0, min(int(ms), self.duration_ms)))

    def ensure_playhead_visible(self, ms):
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        x = self.canvas.x_for_ms(ms)
        bar = self.scroll.horizontalScrollBar()
        left = bar.value()
        vw = self.scroll.viewport().width()
        if x > left + vw - 70:
            bar.setValue(int(x - vw * 0.35))
        elif x < left + 40:
            bar.setValue(max(0, int(x - vw * 0.2)))

    def reverse_tick(self):
        if not self.file_path or self._shuttle_reverse_rate <= 0:
            return
        step = int(40 * self._shuttle_reverse_rate)
        self.seek_to(self.player.position() - step)

    def shuttle_j(self):
        if not self.file_path:
            return
        self.player.pause()
        self._shuttle_forward_rate = 1
        self._shuttle_reverse_rate = min(4, self._shuttle_reverse_rate * 2 if self._shuttle_reverse_rate else 1)
        self.reverse_timer.start()
        self.notice.setText(f'倒放 {self._shuttle_reverse_rate}× · K 暂停')

    def shuttle_k(self):
        self.reverse_timer.stop()
        self._shuttle_reverse_rate = 0
        self._shuttle_forward_rate = 1
        self.player.setPlaybackRate(1.0)
        self.player.pause()
        self.notice.setText('已暂停')

    def shuttle_l(self):
        if not self.file_path:
            return
        self.reverse_timer.stop()
        self._shuttle_reverse_rate = 0
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self._shuttle_forward_rate = min(4, self._shuttle_forward_rate * 2)
        else:
            self._shuttle_forward_rate = 1
        self.player.setPlaybackRate(float(self._shuttle_forward_rate))
        self.player.play()
        self.notice.setText(f'播放 {self._shuttle_forward_rate}× · K 暂停')

    def set_in_point(self):
        if not self.file_path: return
        self.in_point = self.player.position()
        if self.out_point is not None and self.out_point <= self.in_point:
            self.out_point = None
        self.canvas.set_in_out(self.in_point, self.out_point)
        self.notice.setText(f'入点 I · {fmt_ms(self.in_point)}')

    def set_out_point(self):
        if not self.file_path: return
        self.out_point = self.player.position()
        if self.in_point is not None and self.out_point <= self.in_point:
            self.in_point = None
        self.canvas.set_in_out(self.in_point, self.out_point)
        self.notice.setText(f'出点 O · {fmt_ms(self.out_point)}')

    def clear_in_out(self):
        self.in_point = self.out_point = None
        self.canvas.set_in_out(None, None)
        self.notice.setText('已清除入点 / 出点')

    def toggle_marker(self):
        if not self.file_path: return
        cur = self.player.position()
        threshold = 120
        found = next((m for m in self.marker_positions if abs(m-cur) <= threshold), None)
        if found is not None:
            self.marker_positions.remove(found)
            self.notice.setText('已删除标记')
        else:
            self.marker_positions.append(cur)
            self.marker_positions.sort()
            self.notice.setText(f'已添加标记 · {fmt_ms(cur)}')
        self.canvas.set_markers(self.marker_positions)

    def _valid_in_out(self):
        return self.in_point is not None and self.out_point is not None and self.out_point > self.in_point

    def delete_in_out(self):
        if not self._valid_in_out():
            self.notice.setText('请先使用 I / O 设置入点和出点')
            return
        snap = self.model.snapshot()
        if self.model.delete_range(self.in_point, self.out_point):
            self.history.push(snap)
            self.notice.setText(f'已删除范围 · {fmt_ms(self.in_point)} — {fmt_ms(self.out_point)}')
            self.refresh_all()

    def keep_in_out(self):
        if not self._valid_in_out():
            self.notice.setText('请先使用 I / O 设置入点和出点')
            return
        snap = self.model.snapshot()
        if self.model.keep_range(self.in_point, self.out_point):
            self.history.push(snap)
            self.notice.setText(f'仅保留范围 · {fmt_ms(self.in_point)} — {fmt_ms(self.out_point)}')
            self.refresh_all()

    # ----- editing ------------------------------------------------------------
    def split_at(self, ms):
        if not self.file_path:
            return
        snap = self.model.snapshot()
        target = int(ms)
        if self.model.split_at(target):
            self.history.push(snap)
            self.seek_to(target)
            self.notice.setText(f'已分割 · {fmt_ms(target)}')
            self.refresh_all()
        else:
            self.notice.setText('当前位置已经是边界，或距离片段边缘过近')

    def keep_left_at(self, ms=None):
        if not self.file_path:
            return
        ms = int(self.player.position() if ms is None else ms)
        snap = self.model.snapshot()
        if self.model.keep_left_at(ms):
            self.history.push(snap)
            self.notice.setText(f'已保留左侧 · {fmt_ms(ms)}')
            self.refresh_all()

    def keep_right_at(self, ms=None):
        if not self.file_path:
            return
        ms = int(self.player.position() if ms is None else ms)
        snap = self.model.snapshot()
        if self.model.keep_right_at(ms):
            self.history.push(snap)
            self.notice.setText(f'已保留右侧 · {fmt_ms(ms)}')
            self.refresh_all()

    def select_segment(self, uid, mode):
        self.model.select_uid(uid, mode)
        self.refresh_all()

    def select_many(self, uids, mode):
        self.model.select_many(uids, mode)
        self.refresh_all()

    def select_all(self):
        self.model.select_all()
        self.refresh_all()

    def clear_selection(self):
        self.model.clear_selection()
        self.refresh_all()

    def delete_selected(self):
        if not self.model.selected:
            return
        snap = self.model.snapshot()
        if self.model.delete_selected():
            self.history.push(snap)
            self.notice.setText('已删除片段 · Ctrl+Z 可恢复')
            self.refresh_all()

    def trim_preview(self, uid, edge, ms):
        if self._trim_snapshot is None:
            self._trim_snapshot = self.model.snapshot()
        if self.model.trim(uid, edge, int(ms)):
            self.seek_to(ms)
            self.refresh_all()

    def trim_commit(self, uid, edge, ms):
        if self._trim_snapshot is not None:
            self.history.push(self._trim_snapshot)
            self._trim_snapshot = None
            self.notice.setText('边界已调整')
            self.refresh_all()

    def undo(self):
        state = self.history.undo(self.model.snapshot())
        if state:
            self.model.restore(state)
            self.notice.setText('已撤销')
            self.refresh_all()

    def redo(self):
        state = self.history.redo(self.model.snapshot())
        if state:
            self.model.restore(state)
            self.notice.setText('已重做')
            self.refresh_all()

    # ----- timeline -----------------------------------------------------------
    def set_timeline_tool(self, mode):
        mode = 'hand' if mode == 'hand' else 'select'
        self.canvas.set_tool_mode(mode)
        self.select_tool_btn.setChecked(mode == 'select')
        self.hand_tool_btn.setChecked(mode == 'hand')
        self.notice.setText('选择工具' if mode == 'select' else '手型平移工具：按住左键拖动时间轴')

    def sync_timeline_canvas_height(self):
        # QScrollArea with a non-resizable wide child can otherwise retain a
        # stale/zero child height after a QSplitter drag on Windows. Pin the
        # canvas height to the *visible* viewport on every splitter movement.
        if not hasattr(self, 'scroll') or not hasattr(self, 'canvas'):
            return
        h = max(124, self.scroll.viewport().height())
        if self.canvas.height() != h:
            self.canvas.setFixedHeight(h)
        self.canvas.refresh_width()
        self.canvas.updateGeometry()
        self.canvas.update()

    def update_zoom_limits(self):
        """Allow deep timeline zoom without exceeding Qt's practical widget width."""
        duration_s = max(0.001, self.duration_ms / 1000.0)
        # 2000 px/s means a ~1200 px viewport can inspect about 0.6 s.
        # For very long sources, cap the backing canvas near 12M pixels so
        # QWidget geometry stays safely below its platform limit.
        max_canvas_px = 12_000_000
        max_pps = min(2000.0, max_canvas_px / duration_s)
        max_value = max(4200, int(max_pps * 10.0))
        self.zoom.setMaximum(max_value)

    def step_zoom(self, factor):
        # Relative steps remain useful at both overview and frame-detail scales.
        value = max(1, self.zoom.value())
        new_value = int(round(value * factor))
        if new_value == value:
            new_value += 1 if factor > 1 else -1
        self.zoom.setValue(max(self.zoom.minimum(), min(self.zoom.maximum(), new_value)))

    def zoom_changed(self, v):
        self.canvas.px_per_sec = max(0.05, float(v) / 10.0)
        self.canvas.refresh_width()
        self.canvas.update()

    def zoom_at(self, factor, anchor):
        old_x = self.canvas.x_for_ms(anchor)
        new_value = int(self.canvas.px_per_sec * 10 * factor)
        self.zoom.setValue(max(self.zoom.minimum(), min(self.zoom.maximum(), new_value)))
        new_x = self.canvas.x_for_ms(anchor)
        bar = self.scroll.horizontalScrollBar()
        bar.setValue(int(bar.value() + new_x - old_x))

    def fit_timeline(self):
        if not self.duration_ms:
            return
        self.update_zoom_limits()
        viewport = max(260, self.scroll.viewport().width())
        self.canvas.set_min_content_width(max(200, viewport // 4))
        usable = max(200, viewport - 2 * self.canvas.margin - 8)
        pps = max(0.05, usable / max(0.001, self.duration_ms / 1000.0))
        # Keep the slider capable of zooming out to fit, but never collapse map below ~200px.
        fit_value = max(1, int(pps * 10))
        self.zoom.setMinimum(min(self.zoom.minimum(), fit_value))
        self.zoom.setValue(fit_value)
        self.scroll.horizontalScrollBar().setValue(0)

    def pan_timeline(self, dx):
        bar = self.scroll.horizontalScrollBar()
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + int(dx))))

    def jump_cut(self, direction):
        edges = sorted(set(
            [0, self.duration_ms] + [x for s in self.model.segments for x in (s.start_ms, s.end_ms)]
        ))
        cur = self.player.position()
        if direction < 0:
            candidates = [x for x in edges if x < cur - 5]
            self.seek_to(candidates[-1] if candidates else 0)
        else:
            candidates = [x for x in edges if x > cur + 5]
            self.seek_to(candidates[0] if candidates else self.duration_ms)

    def jump_time(self):
        if not self.file_path:
            return
        val, ok = QInputDialog.getText(
            self, '跳转时间', '输入 HH:MM:SS.mmm 或 MM:SS.mmm', text=fmt_ms(self.player.position())
        )
        if ok:
            try:
                sec = 0.0
                for part in val.strip().split(':'):
                    sec = sec * 60 + float(part)
                self.seek_to(int(sec * 1000))
            except Exception:
                QMessageBox.warning(self, '时间格式错误', '例如：01:25:00 或 12:34.500')

    def merge_selected(self):
        snap = self.model.snapshot()
        if self.model.merge_selected():
            self.history.push(snap)
            self.notice.setText('已合并选中片段 · 中间切点已移除')
            self.refresh_all()
        else:
            self.notice.setText('只能合并两个或更多连续、未删除的片段')

    def remove_cut_at(self, ms):
        snap = self.model.snapshot()
        if self.model.remove_cut_at(ms):
            self.history.push(snap)
            self.notice.setText(f'已取消切分 · {fmt_ms(ms)}')
            self.refresh_all()
        else:
            self.notice.setText('此处没有可删除的相邻切点')

    def restore_gap_at(self, ms):
        snap = self.model.snapshot()
        if self.model.restore_deleted_at(ms):
            self.history.push(snap)
            self.notice.setText('已恢复删除区域')
            self.refresh_all()

    def set_in_point_at(self, ms):
        self.seek_to(ms); self.set_in_point()

    def set_out_point_at(self, ms):
        self.seek_to(ms); self.set_out_point()

    def toggle_marker_at(self, ms):
        self.seek_to(ms); self.toggle_marker()

    def export_segment_at(self, ms):
        seg = self.model.segment_at(int(ms))
        if not seg or seg.deleted:
            self.notice.setText('当前位置没有可导出的片段')
            return
        self.model.select_uid(seg.uid, 'replace')
        self.refresh_all()
        self.export(False)

    def clip_list_context_menu(self, pos):
        item = self.clip_list.itemAt(pos)
        if not item:
            return
        uid = item.data(Qt.UserRole)
        if uid not in self.model.selected:
            self.model.select_uid(uid, 'replace')
            self.refresh_all()
        seg = next((x for x in self.model.segments if x.uid == uid), None)
        menu = QMenu(self)
        a_export_one = menu.addAction('导出此片段')
        a_export_sel = menu.addAction(f'导出选中片段（{len(self.model.selected_active())}）')
        a_export_sel.setEnabled(bool(self.model.selected_active()))
        menu.addSeparator()
        a_merge = menu.addAction('合并选中片段 / 取消切分')
        a_merge.setEnabled(len(self.model.selected_active()) >= 2)
        a_left = menu.addAction('保留左侧')
        a_right = menu.addAction('保留右侧')
        a_delete = menu.addAction('删除选中片段')
        menu.addSeparator()
        a_locate = menu.addAction('在时间轴中定位')
        chosen = menu.exec(self.clip_list.viewport().mapToGlobal(pos))
        if chosen == a_export_one and seg:
            self.model.select_uid(uid, 'replace'); self.refresh_all(); self.export(False)
        elif chosen == a_export_sel: self.export(False)
        elif chosen == a_merge: self.merge_selected()
        elif chosen == a_left and seg: self.keep_left_at(seg.end_ms - 1)
        elif chosen == a_right and seg: self.keep_right_at(seg.start_ms + 1)
        elif chosen == a_delete: self.delete_selected()
        elif chosen == a_locate and seg: self.seek_to(seg.start_ms)

    # ----- list / export ------------------------------------------------------
    def clip_item_clicked(self, item):
        # Selection itself is handled by QListWidget ExtendedSelection, so Ctrl
        # and Shift retain their native desktop semantics. A click also locates
        # the clip in the preview without collapsing a multi-selection.
        uid = item.data(Qt.UserRole)
        seg = next((x for x in self.model.segments if x.uid == uid), None)
        if seg:
            self.seek_to(seg.start_ms)

    def clip_selection_changed(self):
        """Make the left browser, timeline and export inspector share one selection."""
        uids = {i.data(Qt.UserRole) for i in self.clip_list.selectedItems()}
        active_uids = {s.uid for s in self.model.active()}
        self.model.selected = uids & active_uids

        # Do not call refresh_all(): it rebuilds the QListWidget and would break
        # an in-progress marquee gesture. Update only selection-dependent UI.
        self.canvas.update()
        active = self.model.active()
        selected = self.model.selected_active()
        total_active_ms = sum(s.duration_ms for s in active)
        estimated = self.file_size * (total_active_ms / self.duration_ms) if self.duration_ms else 0
        self.summary.setText(
            f'{len(active)} 个片段  ·  已选 {len(selected)}  ·  ≈ {human_bytes(estimated)}'
            if active else '0 个片段'
        )
        if self.file_path:
            self.export_info.setText(
                f'保留 {len(active)} 个片段\n'
                f'已选 {len(selected)} 个  ·  预计 {human_bytes(estimated)}'
            )
        self.side_export_selected.setEnabled(bool(selected))
        self.update_export_preview()

    def refresh_all(self):
        self.canvas.update()
        active = self.model.active()
        selected = self.model.selected_active()
        total_active_ms = sum(s.duration_ms for s in active)
        estimated = self.file_size * (total_active_ms / self.duration_ms) if self.duration_ms else 0

        self.clip_count.setText(str(len(active)))
        self.summary.setText(
            f'{len(active)} 个片段  ·  已选 {len(selected)}  ·  ≈ {human_bytes(estimated)}'
            if active else '0 个片段'
        )

        self.clip_list.blockSignals(True)
        self.clip_list.clear()
        index = 0
        for s in self.model.segments:
            if s.deleted:
                continue
            index += 1
            text = (
                f'片段 {index:03d}\n'
                f'{fmt_ms(s.start_ms)}  →  {fmt_ms(s.end_ms)}\n'
                f'时长  {fmt_ms(s.duration_ms)}'
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s.uid)
            item.setSizeHint(QSize(0, 82))
            if self.thumb_paths and self.duration_ms:
                mid = (s.start_ms + s.end_ms) / 2
                ti = min(len(self.thumb_paths)-1, max(0, int(mid / self.duration_ms * len(self.thumb_paths))))
                pm = QPixmap(self.thumb_paths[ti])
                if not pm.isNull():
                    item.setIcon(QIcon(pm))
            self.clip_list.addItem(item)
            item.setSelected(s.uid in self.model.selected)
        self.clip_list.blockSignals(False)
        self.clip_empty.setVisible(len(active) == 0)

        if self.file_path:
            self.export_info.setText(
                f'保留 {len(active)} 个片段\n'
                f'已选 {len(selected)} 个  ·  预计 {human_bytes(estimated)}'
            )
        else:
            self.export_info.setText('打开视频后显示导出信息')
        self.side_export_selected.setEnabled(bool(selected))
        self.side_export_all.setEnabled(bool(active))
        self.update_export_preview()

    def set_export_mode(self, mode):
        mode = 'precise' if mode == 'precise' else 'lossless'
        self.export_mode = mode
        self.mode_lossless.blockSignals(True)
        self.mode_precise.blockSignals(True)
        self.mode_lossless.setChecked(mode == 'lossless')
        self.mode_precise.setChecked(mode == 'precise')
        self.mode_lossless.blockSignals(False)
        self.mode_precise.blockSignals(False)
        if mode == 'lossless':
            self.mode_hint.setText('纯 Stream Copy · 极速 · 导出边界扩展到安全关键帧')
            self.notice.setText('无损模式：编辑点不变，导出时使用安全关键帧边界')
        else:
            self.mode_hint.setText('帧级精准 · 重新编码所选片段 · 切点与时间轴完全一致')
            self.notice.setText('精准模式：按时间轴切点逐帧导出')
        self.update_export_preview()

    def resolved_export_bounds(self, seg):
        if self.export_mode == 'precise':
            return seg.start_ms, seg.end_ms
        return lossless_safe_bounds(self.keyframes, seg.start_ms, seg.end_ms, self.duration_ms)

    def browse_output(self):
        d = QFileDialog.getExistingDirectory(
            self,
            '选择保存位置',
            self.out_dir.text() or (str(Path(self.file_path).parent) if self.file_path else '')
        )
        if d:
            self.out_dir.setText(d)

    def _sync_path_card_height(self):
        # Only the optional prefix field expands now.  The final output folder is
        # selected directly with the folder picker; no hidden UUID/subfolder step.
        base = 146
        extra = 43 if self.prefix_on.isChecked() else 0
        self.path_card.setMinimumHeight(base + extra)
        self.path_card.updateGeometry()

    def prefix_toggle(self, on):
        self.prefix.setVisible(on)
        self._sync_path_card_height()
        if on:
            self.prefix.setFocus(Qt.MouseFocusReason)
            self.prefix.selectAll()
        self.update_export_preview()

    def update_export_preview(self, *_):
        ext = Path(self.file_path).suffix if self.file_path else '.mp4'
        pre = (
            self.prefix.text().strip() + '_'
            if self.prefix_on.isChecked() and self.prefix.text().strip()
            else ''
        )
        count = len(self.model.selected_active()) or len(self.model.active())
        n = max(1, min(4, count))
        names = [f'{pre}{i:03d}{ext}' for i in range(1, n + 1)]
        mode_note = ''
        if self.file_path and self.export_mode == 'lossless':
            sample = self.model.selected_active() or self.model.active()
            shifted = 0
            max_shift = 0
            for seg in sample:
                a, b = self.resolved_export_bounds(seg)
                d = max(abs(a-seg.start_ms), abs(b-seg.end_ms))
                if d:
                    shifted += 1
                    max_shift = max(max_shift, d)
            if shifted:
                mode_note = f'\n\n◇ {shifted} 个片段将扩展到安全关键帧 · 最大 {max_shift/1000:.3f}s'
        elif self.file_path and self.export_mode == 'precise':
            mode_note = '\n\n◆ 精准模式：按时间轴编辑点导出'
        preview_names = names
        self.name_preview.setText('\n'.join(preview_names) + ('\n…' if count > 4 else '') + mode_note)
        self.preview_count.setText(f'{count} 个文件' if count else '0 个文件')

    def output_items(self, segs):
        if not self.file_path:
            return None, None
        parent = Path(self.out_dir.text().strip() or str(Path(self.file_path).parent))
        parent.mkdir(parents=True, exist_ok=True)
        out = parent
        ext = Path(self.file_path).suffix or '.mp4'
        pre = (
            self.prefix.text().strip() + '_'
            if self.prefix_on.isChecked() and self.prefix.text().strip()
            else ''
        )
        items = []
        for i, s in enumerate(sorted(segs, key=lambda x: x.start_ms), 1):
            a, b = self.resolved_export_bounds(s)
            items.append(ExportItem(
                s.uid, a, b, str(unique_file(out / f'{pre}{i:03d}{ext}')),
                requested_start_ms=s.start_ms, requested_end_ms=s.end_ms
            ))
        return out, items

    def export(self, all_segments):
        if not self.file_path:
            return
        segs = self.model.active() if all_segments else self.model.selected_active()
        if not segs:
            QMessageBox.information(
                self, '没有片段',
                '请先选择要导出的片段。' if not all_segments else '没有可导出的片段。'
            )
            return
        out, items = self.output_items(segs)
        engine = ExportEngine(self.ffmpeg, self.export_mode, self.media_info)
        mode_label = '无损' if self.export_mode == 'lossless' else '精准'
        prog = QProgressDialog(f'正在{mode_label}导出…', '取消', 0, len(items), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        fails = []
        done = 0
        for i, item in enumerate(items, 1):
            if prog.wasCanceled():
                break
            prog.setLabelText(f'正在导出 {i}/{len(items)} · {Path(item.output_path).name}')
            prog.setValue(i - 1)
            QApplication.processEvents()
            rc, err = engine.export_one(self.file_path, item, cancel_cb=lambda: (QApplication.processEvents() or prog.wasCanceled()))
            if rc == 130:
                break
            if rc:
                fails.append((item, err))
            else:
                done += 1
        prog.setValue(len(items))
        prog.close()
        if fails:
            QMessageBox.warning(
                self, '导出完成（部分失败）',
                f'成功 {done} 个，失败 {len(fails)} 个。\n\n' + fails[0][1][-500:]
            )
        else:
            box = QMessageBox(self)
            box.setWindowTitle('导出完成')
            box.setText(f'已导出 {done} 个片段。')
            open_button = box.addButton('打开文件夹', QMessageBox.AcceptRole)
            box.addButton('完成', QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() == open_button:
                if os.name == 'nt':
                    os.startfile(str(out))
                else:
                    subprocess.Popen(['xdg-open', str(out)])

    def show_shortcuts(self):
        ShortcutDialog(self).exec()
