"""Threshold alerts for the usage HUD.

Two pieces, deliberately separable: :class:`AlertTracker` is pure state
machinery that decides *whether* to alert, and :class:`TrayNotifier` is the
Windows side that shows it.  Only the tracker needs tests.
"""

from __future__ import annotations

import ctypes
import math
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Optional


WARN_USED_PERCENT = 80.0
CRITICAL_USED_PERCENT = 90.0

LEVEL_NONE = 0
LEVEL_WARN = 1
LEVEL_CRITICAL = 2

LEVEL_NAMES = {LEVEL_WARN: "WARN", LEVEL_CRITICAL: "CRITICAL"}


@dataclass(frozen=True)
class AlertEvent:
    key: str
    label: str
    level: int
    used_percent: float
    resets_at: Optional[float]

    @property
    def level_name(self) -> str:
        return LEVEL_NAMES.get(self.level, str(self.level))


class AlertTracker:
    """Fire once per threshold per limit window, not once per poll.

    A window is identified by its ``resets_at``: when that moves, the quota has
    rolled over and the same thresholds may fire again.  Usage falling back
    below a threshold also re-arms it, which matters for the 5-hour window.
    """

    def __init__(
        self,
        *,
        warn_percent: float = WARN_USED_PERCENT,
        critical_percent: float = CRITICAL_USED_PERCENT,
    ) -> None:
        self.warn_percent = warn_percent
        self.critical_percent = critical_percent
        self._levels: dict[str, int] = {}
        self._cycles: dict[str, Optional[float]] = {}

    def level_for(self, used_percent: float) -> int:
        if used_percent >= self.critical_percent:
            return LEVEL_CRITICAL
        if used_percent >= self.warn_percent:
            return LEVEL_WARN
        return LEVEL_NONE

    def evaluate(
        self,
        key: str,
        label: str,
        used_percent: Optional[float],
        resets_at: Optional[float],
    ) -> Optional[AlertEvent]:
        if used_percent is None or not math.isfinite(used_percent):
            return None

        if key in self._cycles and self._cycles[key] != resets_at:
            # New quota window: forget what we already announced.
            self._levels[key] = LEVEL_NONE
        self._cycles[key] = resets_at

        level = self.level_for(used_percent)
        previous = self._levels.get(key, LEVEL_NONE)
        self._levels[key] = level
        if level <= previous:
            return None
        return AlertEvent(
            key=key,
            label=label,
            level=level,
            used_percent=used_percent,
            resets_at=resets_at,
        )


class _NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


_NIF_ICON = 0x02
_NIF_TIP = 0x04
_NIF_INFO = 0x10
_NIM_ADD = 0
_NIM_MODIFY = 1
_NIM_DELETE = 2
_NIIF_INFO = 0x01
_NIIF_WARNING = 0x02
_IDI_INFORMATION = 32516
_IDI_WARNING = 32515


class TrayNotifier:
    """Show a Windows notification through a short-lived tray icon.

    ``Shell_NotifyIcon`` balloons are rendered as ordinary toasts on Windows
    10/11 and need no registered AppUserModelID, unlike the WinRT toast API.
    The icon is added only to carry a notification and removed again by
    :meth:`close`, so the HUD does not sit in the tray permanently.
    """

    def __init__(self, uid: int = 1) -> None:
        self.uid = uid
        self._data: Optional[_NotifyIconData] = None

    @property
    def active(self) -> bool:
        return self._data is not None

    def notify(self, handle: Optional[int], title: str, message: str, *, critical: bool) -> bool:
        if os.name != "nt" or not handle:
            return False
        try:
            shell32 = ctypes.windll.shell32
            user32 = ctypes.windll.user32
            icon_id = _IDI_WARNING if critical else _IDI_INFORMATION

            data = self._data or _NotifyIconData()
            data.cbSize = ctypes.sizeof(_NotifyIconData)
            data.hWnd = handle
            data.uID = self.uid
            data.uFlags = _NIF_ICON | _NIF_TIP | _NIF_INFO
            data.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(icon_id))
            data.szTip = "Codex / Claude Usage"
            # Windows truncates these silently; keep them short on purpose.
            data.szInfoTitle = title[:63]
            data.szInfo = message[:255]
            data.dwInfoFlags = _NIIF_WARNING if critical else _NIIF_INFO

            message_id = _NIM_MODIFY if self._data is not None else _NIM_ADD
            if not shell32.Shell_NotifyIconW(message_id, ctypes.byref(data)):
                if message_id == _NIM_MODIFY:
                    # The shell dropped our icon (Explorer restart); re-add it.
                    self._data = None
                    if not shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
                        return False
                else:
                    return False
            self._data = data
            return True
        except (AttributeError, OSError, ValueError):
            self._data = None
            return False

    def close(self) -> None:
        data = self._data
        self._data = None
        if data is None or os.name != "nt":
            return
        try:
            ctypes.windll.shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
        except (AttributeError, OSError, ValueError):
            pass


def describe_alert(event: AlertEvent, reset_text: str) -> tuple[str, str]:
    """Return the ``(title, body)`` pair shown in the notification."""

    remaining = max(0.0, 100.0 - event.used_percent)
    headline = "한도 임박" if event.level >= LEVEL_CRITICAL else "한도 경고"
    title = f"{event.label} {headline}"
    body = f"{event.used_percent:.0f}% 사용 · 남은 {remaining:.0f}% · {reset_text}"
    return title, body
