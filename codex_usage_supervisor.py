"""Windowless supervisor for the packaged Codex usage widget."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence


RESTART_DELAY_SECONDS = 3.0
MISSING_FILE_DELAY_SECONDS = 30.0
SUPERVISOR_MUTEX = "Local\\CodexUsageWidgetSupervisor"
WIDGET_MUTEX = "Local\\CodexUsageWidget"
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000


def _acquire_mutex(name: str) -> Optional[tuple[object, int]]:
    if os.name != "nt":
        return object(), 1
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    handle = create_mutex(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return kernel32, int(handle)


def _close_mutex(owned: Optional[tuple[object, int]]) -> None:
    if owned is None or os.name != "nt":
        return
    kernel32, handle = owned
    kernel32.CloseHandle(handle)


def _widget_is_running() -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
    open_mutex.restype = ctypes.c_void_p
    handle = open_mutex(SYNCHRONIZE, False, WIDGET_MUTEX)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _widget_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().with_name("CodexUsageWidget.exe")
    return Path(__file__).resolve().with_name("codex_usage_widget.py")


def _widget_command() -> Sequence[str]:
    widget_path = _widget_path()
    if getattr(sys, "frozen", False):
        return [str(widget_path)]
    return [sys.executable, str(widget_path)]


def main() -> None:
    supervisor_mutex = _acquire_mutex(SUPERVISOR_MUTEX)
    if supervisor_mutex is None:
        return
    try:
        command = _widget_command()
        widget_path = Path(command[-1])
        while True:
            if not widget_path.is_file():
                time.sleep(MISSING_FILE_DELAY_SECONDS)
                continue
            if _widget_is_running():
                time.sleep(1.0)
                continue

            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(widget_path.parent),
                    shell=False,
                )
                process.wait()
            except OSError:
                pass
            time.sleep(RESTART_DELAY_SECONDS)
    finally:
        _close_mutex(supervisor_mutex)


if __name__ == "__main__":
    main()
