"""Application translations and saved language selection, defaulting to English."""

from __future__ import annotations

import os
import sys
from datetime import datetime

from . import en, es, ja, ko, zh_tw


LANGUAGES = {
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "es": "Español",
}
_CATALOGS = {
    "zh_CN": {}, "zh_TW": zh_tw.MESSAGES, "en": en.MESSAGES,
    "ja": ja.MESSAGES, "ko": ko.MESSAGES, "es": es.MESSAGES,
}
_language = "en"
_TIME_ZONE_LANGUAGES = {
    "China Standard Time": "zh_CN",
    "Asia/Shanghai": "zh_CN", "Asia/Chongqing": "zh_CN",
    "Asia/Harbin": "zh_CN", "Asia/Urumqi": "zh_CN",
    "PRC": "zh_CN",
    "Taipei Standard Time": "zh_TW", "Asia/Taipei": "zh_TW",
    "Asia/Hong_Kong": "zh_TW", "Asia/Macau": "zh_TW",
    "Tokyo Standard Time": "ja", "Asia/Tokyo": "ja", "Japan": "ja",
    "Korea Standard Time": "ko", "Asia/Seoul": "ko", "ROK": "ko",
    "Romance Standard Time": "es", "Europe/Madrid": "es",
    "Atlantic/Canary": "es", "Africa/Ceuta": "es",
    "Pacific Standard Time (Mexico)": "es", "Mountain Standard Time (Mexico)": "es",
    "Central Standard Time (Mexico)": "es", "Eastern Standard Time (Mexico)": "es",
    "Cuba Standard Time": "es", "SA Pacific Standard Time": "es",
    "Venezuela Standard Time": "es", "Pacific SA Standard Time": "es",
    "Argentina Standard Time": "es", "Paraguay Standard Time": "es",
    "Montevideo Standard Time": "es", "Central America Standard Time": "es",
    "America/Mexico_City": "es", "America/Tijuana": "es", "America/Cancun": "es",
    "America/Monterrey": "es", "America/Merida": "es", "America/Mazatlan": "es",
    "America/Hermosillo": "es", "America/Chihuahua": "es",
    "America/Bogota": "es", "America/Lima": "es", "America/Guayaquil": "es",
    "America/Caracas": "es", "America/Santiago": "es", "America/Asuncion": "es",
    "America/Montevideo": "es", "America/La_Paz": "es", "America/Havana": "es",
    "America/Guatemala": "es", "America/El_Salvador": "es", "America/Managua": "es",
    "America/Costa_Rica": "es", "America/Panama": "es", "America/Tegucigalpa": "es",
    "America/Santo_Domingo": "es", "America/Puerto_Rico": "es",
}


def system_time_zone() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
                return str(winreg.QueryValueEx(key, "TimeZoneKeyName")[0]).rstrip("\x00")
        except OSError:
            return ""
    configured = os.environ.get("TZ", "").lstrip(":")
    if configured:
        return configured
    zone = datetime.now().astimezone().tzinfo
    return str(getattr(zone, "key", "") or "")


def detect_language(time_zone: str | None = None) -> str:
    zone = system_time_zone() if time_zone is None else time_zone
    if zone.startswith("America/Argentina/") or zone == "America/Argentina":
        return "es"
    return _TIME_ZONE_LANGUAGES.get(zone, "en")


def initialize(saved_language: object = None) -> str:
    global _language
    _language = saved_language if isinstance(saved_language, str) and saved_language in LANGUAGES else "en"
    return _language


def current_language() -> str:
    return _language


class TranslatedText(str):
    """A displayed string retaining its source, never inferred from user text."""

    def __new__(cls, source: str, args: tuple[object, ...] = (), parts: tuple[object, ...] = ()):
        instance = super().__new__(cls, cls._render(source, args, parts))
        instance.source = source
        instance.args = args
        instance.parts = parts
        return instance

    @staticmethod
    def _render(source: str, args: tuple[object, ...], parts: tuple[object, ...]) -> str:
        if parts:
            return "".join(render_text(part) for part in parts)
        value = source if _language == "zh_CN" else _CATALOGS[_language].get(source, en.MESSAGES.get(source, source))
        values = tuple(render_text(value) if isinstance(value, TranslatedText) else value for value in args)
        return value.format(*values) if args else value

    def render(self) -> str:
        return self._render(self.source, self.args, self.parts)

    def __add__(self, other: str) -> str:
        if not isinstance(other, str):
            return NotImplemented
        return TranslatedText("", parts=(self, other))

    def __radd__(self, other: str) -> str:
        if not isinstance(other, str):
            return NotImplemented
        return TranslatedText("", parts=(other, self))


def render_text(value: object) -> str:
    return value.render() if isinstance(value, TranslatedText) else str(value)


def tr(source: str, *args: object) -> str:
    return TranslatedText(source, args)
