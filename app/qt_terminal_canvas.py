"""Cell-based terminal painting; no editable text document or layout pipeline."""

from __future__ import annotations

import math
from collections import OrderedDict
from contextlib import nullcontext

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QFrame


_BACKGROUND = QColor("#111827")
_FOREGROUND = QColor("#e5e7eb")
_SELECTION = QColor("#374151")
_ANSI_COLORS = {
    "black": "#000000", "red": "#cd3131", "green": "#0dbc79",
    "brown": "#e5e510", "blue": "#2472c8", "magenta": "#bc3fbc",
    "cyan": "#11a8cd", "white": "#e5e5e5",
    "brightblack": "#666666", "brightred": "#f14c4c", "brightgreen": "#23d18b",
    "brightbrown": "#f5f543", "brightblue": "#3b8eea", "brightmagenta": "#d670d6",
    "brightcyan": "#29b8db", "brightwhite": "#ffffff",
}


class TerminalCanvas(QAbstractScrollArea):
    keyPressed = Signal(object)
    textCommitted = Signal(str)
    geometryChanged = Signal()
    _PADDING = 8
    _CACHE_BYTES = 16 * 1024 * 1024

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._screen = None
        self._screen_lock = None
        self._history = []
        self._history_key = None
        self._history_base = 0
        self._line_cache = OrderedDict()
        self._cache_bytes = 0
        self._dirty_lines = set()
        self._font_cache = {}
        self._color_cache = {}
        self._geometry = None
        self._preview = None
        self._cursor = None
        self._anchor = None
        self._selection_end = None
        self._dragging = False
        self._drag_point = QPoint()
        self._preedit = ""
        self._cursor_on = True
        self._wheel_remainder = 0.0
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.viewport().setCursor(Qt.IBeamCursor)
        self.viewport().setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(500)
        self._blink_timer.timeout.connect(self._blink_cursor)
        self._selection_timer = QTimer(self)
        self._selection_timer.setInterval(35)
        self._selection_timer.timeout.connect(self._auto_scroll_selection)
        self._refresh_metrics()

    def set_screen_lock(self, lock) -> None:
        self._screen_lock = lock

    def _screen_guard(self):
        return self._screen_lock if self._screen_lock is not None else nullcontext()

    def _refresh_metrics(self) -> None:
        metrics = QFontMetricsF(self.font())
        self._cell_width = max(1.0, metrics.horizontalAdvance("0"))
        self._line_height = max(1, math.ceil(metrics.lineSpacing()))
        self._ascent = metrics.ascent()
        self._dpr = self.devicePixelRatioF()
        self._line_cache.clear()
        self._cache_bytes = 0
        self._dirty_lines.clear()
        self._font_cache.clear()

    def terminal_size(self) -> tuple[int, int]:
        return (
            max(20, int((self.viewport().width() - 2 * self._PADDING) / self._cell_width)),
            max(4, (self.viewport().height() - 2 * self._PADDING) // self._line_height),
        )

    def reset_view(self) -> None:
        self._screen = None
        self._history = []
        self._history_key = None
        self._history_base = 0
        self._geometry = None
        self._preview = None
        self._cursor = None
        self._preedit = ""
        self.clear_selection()
        self._refresh_metrics()
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)

    def _visible_rows(self) -> int:
        return max(1, (self.viewport().height() - 2 * self._PADDING) // self._line_height)

    def _update_scrollbars(self) -> None:
        with self._screen_guard():
            self._update_scrollbars_unlocked()

    def _update_scrollbars_unlocked(self) -> None:
        if self._screen is None:
            return
        visible = self._visible_rows()
        self.verticalScrollBar().setPageStep(visible)
        self.verticalScrollBar().setRange(0, max(0, len(self._history) + self._screen.lines - visible))
        width = max(0, math.ceil(self._screen.columns * self._cell_width + 2 * self._PADDING))
        self.horizontalScrollBar().setPageStep(self.viewport().width())
        self.horizontalScrollBar().setSingleStep(max(1, int(self._cell_width)))
        self.horizontalScrollBar().setRange(0, max(0, width - self.viewport().width()))

    def sync_screen(self, screen, preview=None) -> None:
        with self._screen_guard():
            self._sync_screen_unlocked(screen, preview)

    def _sync_screen_unlocked(self, screen, preview=None) -> None:
        scrollbar = self.verticalScrollBar()
        follow = scrollbar.value() >= scrollbar.maximum() and not self.has_selection() and not self._dragging
        old_top = self._history_base + scrollbar.value()
        old_cursor, old_preview = self._cursor, self._preview
        geometry = (screen.columns, screen.lines, self.devicePixelRatioF())
        geometry_changed = geometry != self._geometry
        if geometry_changed:
            if self._geometry is None or geometry[0] != self._geometry[0] or geometry[2] != self._geometry[2]:
                self._refresh_metrics()
            self._geometry = geometry
        self._screen = screen
        history = screen.history.top
        key = (len(history), id(history[0]) if history else 0, id(history[-1]) if history else 0)
        history_changed = key != self._history_key
        if history_changed:
            new_history = list(history)
            old_history_ids = {id(line) for line in self._history}
            # A row can change and scroll into history between two frames.
            # Invalidate its old live-screen image as well as current dirty rows.
            for line in new_history:
                key_id = id(line)
                if key_id not in old_history_ids and key_id in self._line_cache:
                    self._dirty_lines.add(key_id)
            if self._history and new_history:
                first = next((index for index, line in enumerate(self._history) if line is new_history[0]), None)
                if first is None:
                    self._history_base += len(self._history)
                    self.clear_selection()
                else:
                    self._history_base += first
            elif self._history:
                self._history_base = 0
                self.clear_selection()
            self._history = new_history
            self._history_key = key
            if self._anchor is not None and min(self._anchor[0], self._selection_end[0]) < self._history_base:
                self.clear_selection()
        dirty_rows = set(screen.dirty)
        for row in dirty_rows:
            line = screen.buffer.get(row)
            if line is not None and id(line) in self._line_cache:
                self._dirty_lines.add(id(line))
        self._preview = None
        if preview is not None:
            row, predicted = preview
            line = screen.buffer[row]
            prefix = "".join(line.get(column, screen.default_char).data for column in range(screen.cursor.x))
            if predicted.startswith(prefix):
                self._preview = (row, screen.cursor.x, predicted[len(prefix):])
        cursor_column = screen.cursor.x
        if self._preview is not None:
            cursor_column += len(self._preview[2])
        cursor_row = min(max(0, screen.cursor.y), screen.lines - 1)
        cursor_column = min(max(0, cursor_column), screen.columns - 1)
        cursor_cells = screen.buffer.get(cursor_row, {})
        while cursor_column > 0 and cursor_cells.get(cursor_column, screen.default_char).data == "":
            cursor_column -= 1
        self._cursor = (
            self._history_base + len(self._history) + cursor_row,
            cursor_column, not screen.cursor.hidden,
        )
        self._update_scrollbars_unlocked()
        scrollbar.setValue(scrollbar.maximum() if follow else max(0, old_top - self._history_base))
        if geometry_changed or history_changed or old_top != self._history_base + scrollbar.value():
            self.viewport().update()
        else:
            for row in dirty_rows:
                self._update_row(self._history_base + len(self._history) + row)
            if old_cursor != self._cursor or dirty_rows:
                self._cursor_on = True
                if old_cursor is not None:
                    self._update_row(old_cursor[0])
                self._update_row(self._cursor[0])
            if old_preview != self._preview:
                for value in (old_preview, self._preview):
                    if value is not None:
                        self._update_row(self._history_base + len(self._history) + value[0])
        screen.dirty.clear()
        if self.hasFocus():
            QApplication.inputMethod().update(Qt.ImCursorRectangle)

    def _update_row(self, absolute_row: int) -> None:
        top = self._history_base + self.verticalScrollBar().value()
        y = self._PADDING + (absolute_row - top) * self._line_height
        if -self._line_height < y < self.viewport().height():
            self.viewport().update(0, y, self.viewport().width(), self._line_height)

    def _line_at(self, absolute_row: int):
        index = absolute_row - self._history_base
        if 0 <= index < len(self._history):
            return self._history[index]
        row = index - len(self._history)
        if self._screen is not None and 0 <= row < self._screen.lines:
            return self._screen.buffer[row]
        return None

    def _color(self, value: str, default: QColor) -> QColor:
        if value == "default":
            return default
        if value not in self._color_cache:
            color = QColor(_ANSI_COLORS.get(value, "#" + value))
            self._color_cache[value] = color if color.isValid() else default
        return self._color_cache[value]

    def _font_for(self, cell):
        key = (cell.bold, cell.italics, cell.underscore, cell.strikethrough)
        if key not in self._font_cache:
            font = QFont(self.font())
            font.setBold(cell.bold)
            font.setItalic(cell.italics)
            font.setUnderline(cell.underscore)
            font.setStrikeOut(cell.strikethrough)
            self._font_cache[key] = font
        return self._font_cache[key]

    def _draw_cells(self, painter: QPainter, cells, start: int, end: int, selected: bool = False) -> None:
        column = start
        while column < end:
            cell = cells[column]
            if not cell.data:
                column += 1
                continue
            width = 2 if column + 1 < len(cells) and cells[column + 1].data == "" else 1
            run_end = min(end, column + width)
            text = cell.data
            # ASCII runs in a fixed-width font avoid one drawing call per cell.
            if len(text) == 1 and " " <= text <= "~" and width == 1:
                while run_end < end:
                    next_cell = cells[run_end]
                    if (
                        len(next_cell.data) != 1 or not " " <= next_cell.data <= "~"
                        or next_cell[1:] != cell[1:]
                    ):
                        break
                    text += next_cell.data
                    run_end += 1
            fg = self._color(cell.fg, _FOREGROUND)
            bg = self._color(cell.bg, _BACKGROUND)
            if cell.reverse:
                fg, bg = bg, fg
            if selected:
                fg, bg = QColor("#ffffff"), _SELECTION
            rect = QRectF(column * self._cell_width, 0, (run_end - column) * self._cell_width, self._line_height)
            painter.fillRect(rect, bg)
            if text.strip() or cell.underscore or cell.strikethrough:
                painter.setFont(self._font_for(cell))
                painter.setPen(fg)
                painter.save()
                painter.setClipRect(rect)
                painter.drawText(QPointF(column * self._cell_width, self._ascent), text)
                painter.restore()
            column = run_end

    def _row_image(self, line):
        key = id(line)
        cached = self._line_cache.get(key)
        if cached is not None and cached[0] is line and key not in self._dirty_lines:
            self._line_cache.move_to_end(key)
            return cached[1], cached[2]
        cells = tuple(line.get(column, self._screen.default_char) for column in range(self._screen.columns))
        self._dirty_lines.discard(key)
        if cached is not None and cached[0] is line and cached[1] == cells:
            self._line_cache.move_to_end(key)
            return cached[1], cached[2]
        if cached is not None:
            self._cache_bytes -= cached[3]
            del self._line_cache[key]
        width = max(1, math.ceil(self._screen.columns * self._cell_width * self._dpr))
        height = max(1, math.ceil(self._line_height * self._dpr))
        pixmap = QPixmap(width, height)
        pixmap.setDevicePixelRatio(self._dpr)
        pixmap.fill(_BACKGROUND)
        painter = QPainter(pixmap)
        self._draw_cells(painter, cells, 0, len(cells))
        painter.end()
        size = width * height * 4
        self._line_cache[key] = (line, cells, pixmap, size)
        self._cache_bytes += size
        while self._cache_bytes > self._CACHE_BYTES and len(self._line_cache) > 1:
            removed_key, removed = self._line_cache.popitem(last=False)
            self._cache_bytes -= removed[3]
            self._dirty_lines.discard(removed_key)
        return cells, pixmap

    def paintEvent(self, event) -> None:
        with self._screen_guard():
            self._paint_event_unlocked(event)

    def _paint_event_unlocked(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.setClipRegion(event.region())
        painter.fillRect(event.rect(), _BACKGROUND)
        if self._screen is None:
            return
        top = self._history_base + self.verticalScrollBar().value()
        x = self._PADDING - self.horizontalScrollBar().value()
        first = max(0, (event.rect().top() - self._PADDING) // self._line_height)
        last = min(self._visible_rows() + 1, (event.rect().bottom() - self._PADDING) // self._line_height)
        selected = sorted((self._anchor, self._selection_end)) if self.has_selection() else None
        for offset in range(first, last + 1):
            row = top + offset
            y = self._PADDING + offset * self._line_height
            if not event.region().intersects(QRect(0, y, self.viewport().width(), self._line_height)):
                continue
            line = self._line_at(row)
            if line is None:
                continue
            cells, pixmap = self._row_image(line)
            painter.drawPixmap(QPointF(x, y), pixmap)
            if selected is not None and selected[0][0] <= row <= selected[1][0]:
                start = selected[0][1] if row == selected[0][0] else 0
                end = selected[1][1] if row == selected[1][0] else len(cells)
                start, end = min(start, len(cells)), min(end, len(cells))
                if start < len(cells) and not cells[start].data and start > 0:
                    start -= 1
                if end < len(cells) and not cells[end].data:
                    end += 1
                painter.save()
                painter.translate(x, y)
                self._draw_cells(painter, cells, start, end, selected=True)
                painter.restore()
        if self._preview is not None:
            row, column, text = self._preview
            y = self._PADDING + (self._history_base + len(self._history) + row - top) * self._line_height
            painter.fillRect(QRectF(x + column * self._cell_width, y, len(text) * self._cell_width, self._line_height), _BACKGROUND)
            painter.setFont(self.font())
            painter.setPen(_FOREGROUND)
            painter.drawText(QPointF(x + column * self._cell_width, y + self._ascent), text)
        self._paint_cursor(painter, top, x)

    def _paint_cursor(self, painter: QPainter, top: int, x: int) -> None:
        if self._cursor is None or not self._cursor[2]:
            return
        row, column, _visible = self._cursor
        y = self._PADDING + (row - top) * self._line_height
        if y < 0 or y >= self.viewport().height():
            return
        line = self._line_at(row)
        if line is None:
            return
        cell = line.get(column, self._screen.default_char)
        width = 2 if column + 1 < self._screen.columns and line.get(column + 1, self._screen.default_char).data == "" else 1
        rect = QRectF(x + column * self._cell_width, y, width * self._cell_width, self._line_height)
        painter.setPen(_FOREGROUND)
        if not self.hasFocus():
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        elif self._cursor_on:
            painter.fillRect(rect, _FOREGROUND)
            painter.setPen(_BACKGROUND)
            painter.setFont(self._font_for(cell))
            painter.drawText(QPointF(rect.x(), y + self._ascent), cell.data)
        if self._preedit:
            font = QFont(self.font())
            font.setUnderline(True)
            painter.setFont(font)
            painter.setPen(_FOREGROUND)
            painter.fillRect(QRectF(rect.x(), y, self.viewport().width() - rect.x(), self._line_height), _BACKGROUND)
            painter.drawText(QPointF(rect.x(), y + self._ascent), self._preedit)

    def has_selection(self) -> bool:
        return self._anchor is not None and self._selection_end is not None and self._anchor != self._selection_end

    def clear_selection(self) -> None:
        self._anchor = self._selection_end = None
        self._dragging = False
        self._selection_timer.stop()
        self.viewport().update()

    def selected_text(self) -> str:
        with self._screen_guard():
            return self._selected_text_unlocked()

    def _selected_text_unlocked(self) -> str:
        if not self.has_selection() or self._screen is None:
            return ""
        start, end = sorted((self._anchor, self._selection_end))
        result = []
        for row in range(start[0], end[0] + 1):
            line = self._line_at(row)
            if line is None:
                continue
            left = start[1] if row == start[0] else 0
            right = end[1] if row == end[0] else self._screen.columns
            left, right = min(left, self._screen.columns), min(right, self._screen.columns)
            if left > 0 and not line.get(left, self._screen.default_char).data:
                left -= 1
            text = "".join(line.get(column, self._screen.default_char).data for column in range(left, right))
            result.append(text.rstrip())
        return "\n".join(result)

    def copy(self) -> None:
        if self.has_selection():
            QApplication.clipboard().setText(self.selected_text())

    def _position_at(self, point: QPoint) -> tuple[int, int]:
        with self._screen_guard():
            return self._position_at_unlocked(point)

    def _position_at_unlocked(self, point: QPoint) -> tuple[int, int]:
        total = len(self._history) + (self._screen.lines if self._screen is not None else 1)
        row = self.verticalScrollBar().value() + (point.y() - self._PADDING) // self._line_height
        row = self._history_base + min(max(0, row), max(0, total - 1))
        column = int((point.x() - self._PADDING + self.horizontalScrollBar().value()) / self._cell_width + 0.5)
        column = min(max(0, column), self._screen.columns if self._screen is not None else 0)
        line = self._line_at(row)
        if line is not None and column > 0 and column < self._screen.columns:
            if not line.get(column, self._screen.default_char).data:
                column -= 1
        return row, column

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.setFocus(Qt.MouseFocusReason)
            position = self._position_at(event.position().toPoint())
            if not event.modifiers() & Qt.ShiftModifier or self._anchor is None:
                self._anchor = position
            self._selection_end = position
            self._dragging = True
            self.viewport().update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._drag_point = event.position().toPoint()
            self._selection_end = self._position_at(self._drag_point)
            if not self.viewport().rect().contains(self._drag_point):
                self._selection_timer.start()
            else:
                self._selection_timer.stop()
            self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self._selection_timer.stop()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._screen is None:
            super().mouseDoubleClickEvent(event)
            return
        with self._screen_guard():
            row, column = self._position_at_unlocked(event.position().toPoint())
            line = self._line_at(row)
            if line is None:
                return
            column = min(column, self._screen.columns - 1)
            def word(index):
                value = line.get(index, self._screen.default_char).data
                return not value or any(char.isalnum() or char in "_./~:@%+-\\" for char in value)
            left, right = column, column + 1
            if word(column):
                while left > 0 and word(left - 1):
                    left -= 1
                while right < self._screen.columns and word(right):
                    right += 1
        self._anchor, self._selection_end = (row, left), (row, right)
        self._dragging = False
        self._selection_timer.stop()
        self.viewport().update()
        event.accept()

    def _auto_scroll_selection(self) -> None:
        if not self._dragging:
            self._selection_timer.stop()
            return
        delta = -2 if self._drag_point.y() < 0 else (2 if self._drag_point.y() >= self.viewport().height() else 0)
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + delta)
        self._selection_end = self._position_at(self._drag_point)
        self.viewport().update()

    def wheelEvent(self, event) -> None:
        delta = event.pixelDelta().y()
        if not delta:
            delta = event.angleDelta().y() / 120 * 3 * self._line_height
        self._wheel_remainder += delta / self._line_height
        rows = math.trunc(self._wheel_remainder)
        self._wheel_remainder -= rows
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - rows)
        event.accept()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        # Cached row images are reused; no terminal text is re-parsed on scroll.
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_screen"):
            self._update_scrollbars()

    def event(self, event) -> bool:
        if event.type() == QEvent.DevicePixelRatioChange and hasattr(self, "_line_cache"):
            self._refresh_metrics()
            self._geometry = None
            self._update_scrollbars()
            self.viewport().update()
            self.geometryChanged.emit()
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self.keyPressEvent(event)
            return True
        if event.type() == QEvent.ShortcutOverride and event.modifiers() & Qt.ControlModifier:
            if Qt.Key_A <= event.key() <= Qt.Key_Z:
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:
        if event.modifiers() & Qt.ShiftModifier and event.key() in (Qt.Key_PageUp, Qt.Key_PageDown):
            direction = -1 if event.key() == Qt.Key_PageUp else 1
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + direction * bar.pageStep())
        else:
            copying = event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_C and self.has_selection()
            if not copying:
                if self.has_selection():
                    self.clear_selection()
                self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self.keyPressed.emit(event)
        event.accept()

    def inputMethodEvent(self, event) -> None:
        self._preedit = event.preeditString()
        if event.commitString():
            self.clear_selection()
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self.textCommitted.emit(event.commitString())
        if self._cursor is not None:
            self._update_row(self._cursor[0])
        event.accept()

    def inputMethodQuery(self, query):
        if query == Qt.ImEnabled:
            return True
        if query == Qt.ImFont:
            return self.font()
        if query == Qt.ImCursorRectangle:
            row, column = self._cursor[:2] if self._cursor is not None else (self._history_base, 0)
            x = self._PADDING + int(column * self._cell_width) - self.horizontalScrollBar().value()
            y = self._PADDING + (row - self._history_base - self.verticalScrollBar().value()) * self._line_height
            point = self.viewport().mapTo(self, QPoint(x, y))
            return QRect(point.x(), point.y(), max(1, math.ceil(self._cell_width)), self._line_height)
        if query in (Qt.ImSurroundingText, Qt.ImCurrentSelection):
            return ""
        if query in (Qt.ImCursorPosition, Qt.ImAnchorPosition):
            return 0
        return super().inputMethodQuery(query)

    def _blink_cursor(self) -> None:
        self._cursor_on = not self._cursor_on
        if self._cursor is not None:
            self._update_row(self._cursor[0])

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._cursor_on = True
        self._blink_timer.start()
        if self._cursor is not None:
            self._update_row(self._cursor[0])

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self._blink_timer.stop()
        self._preedit = ""
        if self._cursor is not None:
            self._update_row(self._cursor[0])

    def hideEvent(self, event) -> None:
        self._blink_timer.stop()
        self._selection_timer.stop()
        self._dragging = False
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._cursor_on = True
        if self.hasFocus():
            self._blink_timer.start()
        self.geometryChanged.emit()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and hasattr(self, "_line_cache"):
            self._refresh_metrics()
            self._update_scrollbars()
            self.viewport().update()
            self.geometryChanged.emit()

    def shutdown(self) -> None:
        self._blink_timer.stop()
        self._selection_timer.stop()
        self._line_cache.clear()
        self._cache_bytes = 0
