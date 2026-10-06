"""Qt's built-in dialogs share the application's selected language."""

from PySide6.QtCore import QEvent, QLibraryInfo, QObject, QTranslator
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from . import current_language, tr


class DialogButtonTranslator(QObject):
    _TEXT_BY_BUTTON = {
        QDialogButtonBox.Ok: "确定", QDialogButtonBox.Save: "保存",
        QDialogButtonBox.Cancel: "取消", QDialogButtonBox.Close: "关闭",
        QDialogButtonBox.Discard: "不保存", QDialogButtonBox.Apply: "应用",
        QDialogButtonBox.Reset: "重置", QDialogButtonBox.RestoreDefaults: "恢复默认",
        QDialogButtonBox.Yes: "是", QDialogButtonBox.No: "否",
        QDialogButtonBox.Abort: "中止", QDialogButtonBox.Retry: "重试",
        QDialogButtonBox.Ignore: "忽略", QDialogButtonBox.Help: "帮助",
        QDialogButtonBox.Open: "打开",
    }

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Show and isinstance(watched, QDialog):
            for button_box in watched.findChildren(QDialogButtonBox):
                for role, source in self._TEXT_BY_BUTTON.items():
                    button = button_box.button(role)
                    if button is not None:
                        button.setText(tr(source))
        return False


def install_qt_translations(application) -> None:
    previous = getattr(application, "_deployflow_translator", None)
    if previous is not None:
        application.removeTranslator(previous)
        previous.deleteLater()
    language = current_language()
    translator = QTranslator(application)
    directory = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    if translator.load(f"qtbase_{language}", directory):
        application.installTranslator(translator)
    application._deployflow_translator = translator
    if not hasattr(application, "_deployflow_button_translator"):
        button_filter = DialogButtonTranslator(application)
        application.installEventFilter(button_filter)
        application._deployflow_button_translator = button_filter
    for widget in application.allWidgets():
        if isinstance(widget, QDialogButtonBox):
            for role, source in DialogButtonTranslator._TEXT_BY_BUTTON.items():
                button = widget.button(role)
                if button is not None:
                    button.setText(tr(source))
