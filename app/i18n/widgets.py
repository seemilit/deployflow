"""Opt-in Qt text bindings. Only tr() values are refreshed, not user content."""

from weakref import WeakValueDictionary

from PySide6 import QtGui, QtWidgets
from PySide6.QtCore import QObject, QSignalBlocker
from shiboken6 import isValid

from . import TranslatedText, render_text


_bound_widgets = WeakValueDictionary()


def _translated(value):
    if isinstance(value, TranslatedText):
        return True
    return isinstance(value, (list, tuple)) and any(_translated(item) for item in value)


def _render(value):
    if isinstance(value, TranslatedText):
        return render_text(value)
    if isinstance(value, (list, tuple)):
        return type(value)(_render(item) for item in value)
    return value


class _TextBindings:
    def _remember(self, key, method, args):
        bindings = self.__dict__.setdefault("_translation_bindings", {})
        if _translated(args):
            bindings[key] = (method, args)
            _bound_widgets[id(self)] = self
        else:
            bindings.pop(key, None)

    def _refresh_translations(self):
        blocker = QSignalBlocker(self) if isinstance(self, QObject) else None
        try:
            for method, args in self.__dict__.get("_translation_bindings", {}).values():
                method(self, *(_render(arg) for arg in args))
        finally:
            del blocker


def _setter(base, name, indexed=False):
    method = getattr(base, name)

    def set_value(self, *args):
        key = (name, args[0]) if indexed else name
        self._remember(key, method, args)
        return method(self, *(_render(arg) for arg in args))

    return set_value


def _widget(base, text_method=None, indexed=()):
    def init(self, *args, **kwargs):
        base.__init__(self, *(_render(arg) for arg in args), **kwargs)
        if text_method:
            for arg in args:
                if isinstance(arg, TranslatedText):
                    self._remember(text_method, getattr(base, text_method), (arg,))
                    break
        if base is QtWidgets.QTreeWidgetItem:
            for arg in args:
                if isinstance(arg, (list, tuple)):
                    for column, value in enumerate(arg):
                        if isinstance(value, TranslatedText):
                            self._remember(("setText", column), base.setText, (column, value))

    attributes = {"__init__": init}
    methods = {"setToolTip", "setStatusTip", "setWindowTitle", "setPlaceholderText", "setFormat"}
    if text_method:
        methods.add(text_method)
    for name in methods | set(indexed):
        if hasattr(base, name):
            attributes[name] = _setter(base, name, name in indexed)
    return type(base.__name__, (_TextBindings, base), attributes)


QLabel = _widget(QtWidgets.QLabel, "setText")
QPushButton = _widget(QtWidgets.QPushButton, "setText")
QCheckBox = _widget(QtWidgets.QCheckBox, "setText")
QGroupBox = _widget(QtWidgets.QGroupBox, "setTitle")
QAction = _widget(QtGui.QAction, "setText")
QLineEdit = _widget(QtWidgets.QLineEdit)
QPlainTextEdit = _widget(QtWidgets.QPlainTextEdit)
QDialog = _widget(QtWidgets.QDialog)
QMainWindow = _widget(QtWidgets.QMainWindow)
QProgressBar = _widget(QtWidgets.QProgressBar)
QTreeWidgetItem = _widget(QtWidgets.QTreeWidgetItem, indexed=("setText", "setToolTip"))
QTreeWidget = _widget(QtWidgets.QTreeWidget, indexed=())
QTreeWidget.setHeaderLabels = _setter(QtWidgets.QTreeWidget, "setHeaderLabels")


class QProgressDialog(_TextBindings, QtWidgets.QProgressDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*(_render(arg) for arg in args), **kwargs)
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
            self._remember("setLabelText", QtWidgets.QProgressDialog.setLabelText, (args[0],))
            self._remember("setCancelButtonText", QtWidgets.QProgressDialog.setCancelButtonText, (args[1],))

    setLabelText = _setter(QtWidgets.QProgressDialog, "setLabelText")
    setCancelButtonText = _setter(QtWidgets.QProgressDialog, "setCancelButtonText")
    setWindowTitle = _setter(QtWidgets.QProgressDialog, "setWindowTitle")


class QFormLayout(QtWidgets.QFormLayout):
    def addRow(self, *args):
        if len(args) == 2 and isinstance(args[0], TranslatedText):
            args = (QLabel(args[0]), args[1])
        return super().addRow(*args)


class QTabWidget(_TextBindings, QtWidgets.QTabWidget):
    def addTab(self, widget, *args):
        index = super().addTab(widget, *(_render(arg) for arg in args))
        self.setTabText(index, args[-1])
        return index

    def setTabText(self, index, text):
        # Associate translations with the page, since closing tabs changes indices.
        page = self.widget(index)
        if page is not None:
            page._translated_tab_title = text
            _bound_widgets[id(self)] = self
        super().setTabText(index, _render(text))

    def _refresh_translations(self):
        for index in range(self.count()):
            text = getattr(self.widget(index), "_translated_tab_title", None)
            if isinstance(text, TranslatedText):
                super().setTabText(index, text.render())


class QTabBar(_TextBindings, QtWidgets.QTabBar):
    def addTab(self, *args):
        index = super().addTab(*(_render(arg) for arg in args))
        self.setTabText(index, args[-1])
        return index

    setTabText = _setter(QtWidgets.QTabBar, "setTabText", indexed=True)

    def removeTab(self, index):
        bindings = self.__dict__.get("_translation_bindings", {})
        self._translation_bindings = {
            (name, position - (position > index)): (method, (position - (position > index), *args[1:]))
            for (name, position), (method, args) in bindings.items() if position != index
        }
        super().removeTab(index)


def refresh_translations():
    # Called once per user language change, never on terminal input or a timer.
    for widget in list(_bound_widgets.values()):
        if isValid(widget):
            widget._refresh_translations()
