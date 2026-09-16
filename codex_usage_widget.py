"""Small always-on-top Windows HUD for Codex and Claude usage limits.

The widget talks only to the official ``codex app-server`` and ``claude``
subprocesses.  It does not open credential files or handle access tokens itself.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import math
import os
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from claude_usage_client import (
    CLAUDE_REFRESH_SECONDS,
    ClaudeConnectionError,
    ClaudeUnavailableError,
    ClaudeUsageClient,
    ClaudeUsageError,
    parse_limit_window,
)
from usage_alerts import (
    AlertEvent,
    AlertTracker,
    TrayNotifier,
    describe_alert,
)


FIVE_HOURS_MINS = 300
ONE_WEEK_MINS = 10_080
REFRESH_SECONDS = 30.0
RPC_TIMEOUT_SECONDS = 20.0
RECONNECT_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 30.0)
TOPMOST_REFRESH_MS = 500
DISPLAY_WATCH_MS = 30_000

SOURCE_CODEX = "codex"
SOURCE_CLAUDE = "claude"
SOURCES = (SOURCE_CODEX, SOURCE_CLAUDE)

LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 2
RAISE_LOG_INTERVAL_SECONDS = 60.0
ALERT_FLASH_STEPS = 6
ALERT_FLASH_MS = 220
ALERT_NOTIFY_LINGER_MS = 12_000

WINDOW_WIDTH = 280
WINDOW_HEIGHT = 186
# Sized to the free taskbar strip between the weather widget and the centred
# task buttons: wider than this and the HUD has to overlap one of them.
COMPACT_WIDTH = 308
COMPACT_HEIGHT = 32
COMPACT_COLUMN_XS = (13, 97, 181)
COMPACT_COLUMN_WIDTH = 80
COMPACT_RESET_WIDTH = 84
DETAIL_ROW_YS = (44, 90, 136)
DETAIL_BAR_WIDTH = 258

BG = "#17191f"
BAR_BG = "#30333b"
TEXT = "#f1f3f5"
MUTED = "#9298a3"
COMPACT_RESET_TEXT = "#d9dde5"
GREEN = "#54d38a"
AMBER = "#f0b45a"
RED = "#ef6a73"
BORDER = "#353944"


_UI_SCALE = 1.0

LOG = logging.getLogger("codex_usage_widget")
LOG.addHandler(logging.NullHandler())


def default_log_path() -> Path:
    return default_settings_path().with_name("widget.log")


def setup_logging(
    path: Optional[Path] = None,
    *,
    level: int = logging.INFO,
) -> Optional[Path]:
    """Attach a rotating file log; never let logging break the HUD.

    Every Win32 failure in here used to be swallowed silently, which is why a
    z-order bug could only be found by reproducing it by hand.
    """

    target = path or default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except (OSError, UnicodeError, ValueError):
        return None

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    LOG.setLevel(level)
    for existing in list(LOG.handlers):
        if isinstance(existing, logging.handlers.RotatingFileHandler):
            LOG.removeHandler(existing)
            existing.close()
    LOG.addHandler(handler)
    LOG.propagate = False
    return target


def enable_dpi_awareness() -> float:
    """Opt the process into physical pixels and return the primary DPI scale.

    Without this the Tk window lives in DPI-virtualised coordinates while every
    ``user32`` rect we read (taskbar, monitors) is in physical pixels.  On any
    display that is not at 100% the two spaces disagree, which is what makes the
    HUD drift to the wrong place.
    """

    global _UI_SCALE
    if os.name != "nt":
        return _UI_SCALE

    user32 = ctypes.windll.user32
    aware = False
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        aware = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except (AttributeError, OSError, ValueError):
        aware = False
    if not aware:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            aware = True
        except (AttributeError, OSError, ValueError):
            aware = False
    if not aware:
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError, ValueError):
            pass

    try:
        get_dpi_for_window = user32.GetDpiForWindow
        get_dpi_for_window.argtypes = [ctypes.c_void_p]
        get_dpi_for_window.restype = ctypes.c_uint
        dpi = int(get_dpi_for_window(ctypes.c_void_p(user32.GetDesktopWindow())))
    except (AttributeError, OSError, ValueError):
        dpi = 0
    if dpi > 0:
        _UI_SCALE = max(0.75, min(4.0, dpi / 96.0))
    return _UI_SCALE


def ui_scale() -> float:
    return _UI_SCALE


def _s(value: float) -> int:
    """Scale a layout constant expressed at 96 DPI into physical pixels."""

    return int(round(value * _UI_SCALE))


def _configure_once(widget: tk.Widget, cache: dict[str, Any], **options: Any) -> None:
    """Apply only the options that actually changed.

    Rewriting a label with the value it already has still queues a redraw, and
    on a layered (``-alpha``) window every redraw is a full composite — that is
    the visible flicker.
    """

    changed = {key: value for key, value in options.items() if cache.get(key) != value}
    if not changed:
        return
    try:
        widget.configure(**changed)
    except tk.TclError:
        return
    cache.update(changed)


class CodexError(RuntimeError):
    """Base error for the app-server client."""


class CodexConnectionError(CodexError):
    """The app-server process or JSONL transport failed."""


class CodexRpcError(CodexError):
    """The app-server returned a JSON-RPC error response."""


@dataclass(frozen=True)
class RateWindow:
    used_percent: float
    window_duration_mins: int
    resets_at: Optional[float]

    @property
    def remaining_percent(self) -> float:
        return 100.0 - self.used_percent

    @property
    def progress_percent(self) -> float:
        return max(0.0, min(100.0, self.remaining_percent))


@dataclass(frozen=True)
class UsageSnapshot:
    five_hour: Optional[RateWindow]
    weekly: Optional[RateWindow]
    limit_id: Optional[str]
    five_hour_label: str = "5h"
    weekly_label: str = "Week"
    five_hour_limit_id: Optional[str] = None
    weekly_limit_id: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.five_hour is not None and self.weekly is not None

    @property
    def available(self) -> bool:
        return self.five_hour is not None or self.weekly is not None


@dataclass(frozen=True)
class UiMessage:
    kind: str
    snapshot: Optional[UsageSnapshot] = None
    detail: str = ""
    source: str = SOURCE_CODEX


@dataclass(frozen=True)
class WindowState:
    compact_mode: bool = True
    compact_position: Optional[tuple[int, int]] = None
    detail_position: Optional[tuple[int, int]] = None


def default_settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Local"
    else:
        base = Path.home() / ".local" / "state"
    return base / "CodexUsageWidget" / "settings.json"


def default_snapshot_path() -> Path:
    return default_settings_path().with_name("last_snapshot.json")


class WindowStateStore:
    """Tolerant, atomic storage for non-sensitive HUD layout state."""

    VERSION = 3

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_settings_path()

    @staticmethod
    def _parse_position(raw: Any) -> Optional[tuple[int, int]]:
        if not isinstance(raw, Mapping):
            return None
        x = raw.get("x")
        y = raw.get("y")
        if type(x) is not int or type(y) is not int:
            return None
        if not (-10_000_000 <= x <= 10_000_000):
            return None
        if not (-10_000_000 <= y <= 10_000_000):
            return None
        return x, y

    def load(self) -> Optional[WindowState]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping) or raw.get("version") != self.VERSION:
            return None

        mode = raw.get("mode")
        positions = raw.get("positions")
        if mode not in {"compact", "detail"} or not isinstance(positions, Mapping):
            return None
        return WindowState(
            compact_mode=mode == "compact",
            compact_position=self._parse_position(positions.get("compact")),
            detail_position=self._parse_position(positions.get("detail")),
        )

    def save(self, state: WindowState) -> bool:
        payload = {
            "version": self.VERSION,
            "mode": "compact" if state.compact_mode else "detail",
            "positions": {
                "compact": self._render_position(state.compact_position),
                "detail": self._render_position(state.detail_position),
            },
        }
        temp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
            return True
        except (OSError, UnicodeError, TypeError, ValueError):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def _render_position(position: Optional[tuple[int, int]]) -> Any:
        if position is None:
            return None
        return {"x": position[0], "y": position[1]}


class _SingleInstanceMutex:
    def __init__(self, handle: Optional[int] = None, kernel32: Any = None) -> None:
        self._handle = handle
        self._kernel32 = kernel32

    def release(self) -> None:
        if self._handle is None or self._kernel32 is None:
            return
        try:
            self._kernel32.CloseHandle(self._handle)
        finally:
            self._handle = None


def _acquire_single_instance(
    name: str = "Local\\CodexUsageWidget",
) -> Optional[_SingleInstanceMutex]:
    """Return ``None`` when this Windows session already owns the HUD mutex."""

    if os.name != "nt":
        return _SingleInstanceMutex()
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_bool

        handle = create_mutex(None, False, name)
        if not handle:
            return _SingleInstanceMutex()
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            close_handle(handle)
            return None
        return _SingleInstanceMutex(handle, kernel32)
    except (AttributeError, OSError, ValueError):
        return _SingleInstanceMutex()


@dataclass
class _RateGroup:
    limit_id: Optional[str]
    limit_name: Optional[str]
    windows: dict[int, RateWindow]


@dataclass
class _StreamFailure:
    detail: str


@dataclass
class _StreamClosed:
    return_code: Optional[int]


@dataclass
class _Connection:
    process: subprocess.Popen[str]
    inbox: "queue.Queue[object]"
    stderr_lines: deque[str]
    disconnected: threading.Event = field(default_factory=threading.Event)
    stop_lock: threading.Lock = field(default_factory=threading.Lock)
    stopped: bool = False


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_window(raw: Any) -> Optional[RateWindow]:
    if not isinstance(raw, Mapping):
        return None

    duration_number = _number(raw.get("windowDurationMins"))
    used_percent = _number(raw.get("usedPercent"))
    if duration_number is None or used_percent is None:
        return None

    duration = int(duration_number)
    if duration <= 0 or duration != duration_number:
        return None

    resets_at = _number(raw.get("resetsAt"))
    return RateWindow(
        used_percent=used_percent,
        window_duration_mins=duration,
        resets_at=resets_at,
    )


def _parse_group(raw: Any, fallback_id: Optional[str] = None) -> Optional[_RateGroup]:
    if not isinstance(raw, Mapping):
        return None

    raw_limit_id = raw.get("limitId", fallback_id)
    limit_id = str(raw_limit_id) if raw_limit_id is not None else None
    raw_limit_name = raw.get("limitName")
    limit_name = str(raw_limit_name) if raw_limit_name is not None else None
    windows: dict[int, RateWindow] = {}
    for slot in ("primary", "secondary"):
        window = _parse_window(raw.get(slot))
        if window is not None:
            existing = windows.get(window.window_duration_mins)
            if existing is not None and existing != window:
                return None
            windows[window.window_duration_mins] = window

    if not windows:
        return None
    return _RateGroup(limit_id=limit_id, limit_name=limit_name, windows=windows)


def default_snapshot_labels(source: str) -> tuple[str, str]:
    """Return the ``(five_hour_label, weekly_label)`` pair for a source."""

    if source == SOURCE_CLAUDE:
        return "CL 5h", "CL 주"
    return "CX 5h", "CX 주"


def select_usage_windows(result: Mapping[str, Any]) -> UsageSnapshot:
    """Select only the default Codex weekly meter.

    The backward-compatible ``rateLimits`` view is the authoritative default
    Codex meter. Model-specific entries in ``rateLimitsByLimitId`` never
    replace its weekly value, and 300-minute model meters are not displayed.
    """

    legacy = _parse_group(result.get("rateLimits"))

    groups: list[_RateGroup] = []
    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, Mapping):
        for key, raw_group in by_limit_id.items():
            group = _parse_group(raw_group, fallback_id=str(key))
            if group is not None:
                groups.append(group)

    canonical: Optional[_RateGroup] = None
    if legacy is not None and ONE_WEEK_MINS in legacy.windows:
        canonical = legacy
    else:
        codex_weekly = [
            group
            for group in groups
            if group.limit_id == "codex" and ONE_WEEK_MINS in group.windows
        ]
        if len(codex_weekly) == 1:
            canonical = codex_weekly[0]

    weekly = canonical.windows.get(ONE_WEEK_MINS) if canonical is not None else None

    return UsageSnapshot(
        five_hour=None,
        weekly=weekly,
        limit_id=canonical.limit_id if canonical is not None else None,
        five_hour_label="CX 5h",
        weekly_label="CX 주",
        five_hour_limit_id=None,
        weekly_limit_id=canonical.limit_id if weekly is not None else None,
    )


def select_claude_windows(result: Mapping[str, Any]) -> UsageSnapshot:
    """Map the ``claude`` CLI ``get_usage`` payload onto a snapshot.

    Only the two account-wide meters are displayed: the 5-hour session window
    and the all-model weekly window.  Model-scoped weekly entries are ignored,
    mirroring how the Codex side ignores its per-model meters.
    """

    rate_limits = result.get("rate_limits")
    if not isinstance(rate_limits, Mapping):
        rate_limits = {}

    five_hour = None
    parsed = parse_limit_window(rate_limits.get("five_hour"))
    if parsed is not None:
        five_hour = RateWindow(
            used_percent=parsed[0],
            window_duration_mins=FIVE_HOURS_MINS,
            resets_at=parsed[1],
        )

    weekly = None
    parsed = parse_limit_window(rate_limits.get("seven_day"))
    if parsed is not None:
        weekly = RateWindow(
            used_percent=parsed[0],
            window_duration_mins=ONE_WEEK_MINS,
            resets_at=parsed[1],
        )

    return UsageSnapshot(
        five_hour=five_hour,
        weekly=weekly,
        limit_id=SOURCE_CLAUDE,
        five_hour_label="CL 5h",
        weekly_label="CL 주",
        five_hour_limit_id=SOURCE_CLAUDE if five_hour is not None else None,
        weekly_limit_id=SOURCE_CLAUDE if weekly is not None else None,
    )


class UsageSnapshotStore:
    """Persist only the last usage windows per source, never any credential."""

    VERSION = 3

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_snapshot_path()

    @staticmethod
    def _render_window(window: Optional[RateWindow]) -> Any:
        if window is None:
            return None
        return {
            "usedPercent": window.used_percent,
            "windowDurationMins": window.window_duration_mins,
            "resetsAt": window.resets_at,
        }

    @staticmethod
    def _load_window(raw: Any, expected_minutes: int) -> Optional[RateWindow]:
        window = _parse_window(raw)
        if window is None or window.window_duration_mins != expected_minutes:
            return None
        return window

    def load(self) -> dict[str, UsageSnapshot]:
        """Return the cached snapshot per source; unknown data is dropped."""

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, Mapping) or raw.get("version") != self.VERSION:
            return {}

        sources = raw.get("sources")
        if not isinstance(sources, Mapping):
            return {}

        snapshots: dict[str, UsageSnapshot] = {}
        for source in SOURCES:
            entry = sources.get(source)
            if not isinstance(entry, Mapping):
                continue
            weekly = self._load_window(entry.get("weekly"), ONE_WEEK_MINS)
            five_hour = self._load_window(entry.get("fiveHour"), FIVE_HOURS_MINS)
            if weekly is None and five_hour is None:
                continue
            raw_limit_id = entry.get("limitId")
            limit_id = str(raw_limit_id) if raw_limit_id is not None else None
            defaults = default_snapshot_labels(source)
            snapshots[source] = UsageSnapshot(
                five_hour=five_hour,
                weekly=weekly,
                limit_id=limit_id,
                five_hour_label=defaults[0],
                weekly_label=defaults[1],
                five_hour_limit_id=limit_id if five_hour is not None else None,
                weekly_limit_id=limit_id if weekly is not None else None,
            )
        return snapshots

    def save(self, snapshots: Mapping[str, UsageSnapshot]) -> bool:
        sources: dict[str, Any] = {}
        for source in SOURCES:
            snapshot = snapshots.get(source)
            if snapshot is None or not snapshot.available:
                continue
            sources[source] = {
                "limitId": snapshot.weekly_limit_id
                or snapshot.five_hour_limit_id
                or snapshot.limit_id,
                "weekly": self._render_window(snapshot.weekly),
                "fiveHour": self._render_window(snapshot.five_hour),
            }
        if not sources:
            return False

        payload = {
            "version": self.VERSION,
            "savedAt": time.time(),
            "sources": sources,
        }
        temp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_path, self.path)
            return True
        except (OSError, UnicodeError, TypeError, ValueError):
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False


def format_percent(value: float) -> str:
    rounded = round(value)
    if math.isclose(value, rounded, abs_tol=0.05):
        return f"{rounded:.0f}%"
    return f"{value:.1f}%"


def format_reset_time(
    resets_at: Optional[float],
    *,
    now: Optional[datetime] = None,
    local_tz: Any = None,
) -> str:
    if resets_at is None:
        return "Reset —"

    if local_tz is None:
        local_tz = datetime.now().astimezone().tzinfo
    if now is None:
        now = datetime.now(local_tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=local_tz)
    else:
        now = now.astimezone(local_tz)

    try:
        reset = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone(local_tz)
    except (OSError, OverflowError, ValueError):
        return "Reset —"

    if reset.date() == now.date():
        rendered = reset.strftime("%H:%M")
    elif reset.year == now.year:
        rendered = reset.strftime("%m/%d %H:%M")
    else:
        rendered = reset.strftime("%Y/%m/%d %H:%M")
    return f"Reset {rendered}"


def format_compact_reset_time(
    resets_at: Optional[float],
    *,
    now: Optional[datetime] = None,
    local_tz: Any = None,
) -> str:
    if resets_at is None:
        return "R —"

    if local_tz is None:
        local_tz = datetime.now().astimezone().tzinfo
    if now is None:
        now = datetime.now(local_tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=local_tz)
    else:
        now = now.astimezone(local_tz)

    try:
        reset = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone(local_tz)
    except (OSError, OverflowError, ValueError):
        return "R —"

    if reset.date() == now.date():
        return f"R {reset.strftime('%H:%M')}"

    weekday = "월화수목금토일"[reset.weekday()]
    if reset.year == now.year:
        return f"R {reset.month}/{reset.day}({weekday}){reset.strftime('%H:%M')}"
    return (
        f"R {str(reset.year)[2:]}/{reset.month}/{reset.day}({weekday})"
        f"{reset.strftime('%H:%M')}"
    )


def resolve_codex_command() -> tuple[str, ...]:
    """Resolve the same PATH-visible Codex CLI without invoking a shell."""

    candidate = shutil.which("codex")
    if os.name == "nt" and candidate:
        suffix = os.path.splitext(candidate)[1].lower()
        if suffix not in {".com", ".exe", ".bat", ".cmd"}:
            candidate = None

    if candidate is None and os.name == "nt":
        for name in ("codex.cmd", "codex.exe", "codex.bat"):
            candidate = shutil.which(name)
            if candidate:
                break

    if candidate is None and os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            codex_bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            try:
                installed_candidates = [
                    path
                    for path in codex_bin_root.glob("*/codex.exe")
                    if path.is_file()
                ]
                installed_candidates.sort(
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                installed_candidates = []
            if installed_candidates:
                candidate = str(installed_candidates[0])

    if candidate is None:
        raise CodexConnectionError(
            "codex CLI를 찾을 수 없습니다. PATH에서 `codex --version`을 먼저 확인하세요."
        )
    return (candidate,)


class CodexAppServerClient:
    """Minimal id-matched JSONL client for ``codex app-server``."""

    def __init__(
        self,
        command: Optional[Sequence[str]] = None,
        *,
        request_timeout: float = RPC_TIMEOUT_SECONDS,
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._request_timeout = request_timeout
        self._state_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._connection: Optional[_Connection] = None
        self._next_request_id = 1
        self._shutdown = threading.Event()

    @property
    def is_disconnected(self) -> bool:
        with self._state_lock:
            connection = self._connection
        return (
            connection is None
            or connection.disconnected.is_set()
            or connection.process.poll() is not None
        )

    def read_rate_limits(self) -> dict[str, Any]:
        result = self._request("account/rateLimits/read")
        if not isinstance(result, dict):
            raise CodexConnectionError("rate-limit 응답의 result 형식이 올바르지 않습니다.")
        return result

    def _request(self, method: str, params: Any = None) -> Any:
        with self._request_lock:
            connection = self._ensure_connection()
            request_id = self._take_request_id()
            message: dict[str, Any] = {"method": method, "id": request_id}
            if params is not None:
                message["params"] = params
            try:
                return self._exchange(connection, message, request_id)
            except CodexRpcError:
                raise
            except (CodexConnectionError, OSError) as exc:
                self._discard_connection(connection)
                if isinstance(exc, CodexConnectionError):
                    raise
                raise CodexConnectionError(str(exc)) from exc

    def _ensure_connection(self) -> _Connection:
        if self._shutdown.is_set():
            raise CodexConnectionError("위젯이 종료 중입니다.")

        with self._state_lock:
            existing = self._connection
            if (
                existing is not None
                and not existing.disconnected.is_set()
                and existing.process.poll() is None
            ):
                return existing
            if existing is not None:
                self._connection = None

        if existing is not None:
            self._stop_connection(existing)

        connection = self._spawn_connection()
        with self._state_lock:
            if self._shutdown.is_set():
                should_stop = True
            else:
                self._connection = connection
                should_stop = False
        if should_stop:
            self._stop_connection(connection)
            raise CodexConnectionError("위젯이 종료 중입니다.")

        try:
            initialize_id = self._take_request_id()
            initialize = {
                "method": "initialize",
                "id": initialize_id,
                "params": {
                    "clientInfo": {
                        "name": "codex_usage_widget",
                        "title": "Codex Usage Widget",
                        "version": "1.0.0",
                    }
                },
            }
            self._exchange(connection, initialize, initialize_id)
            self._send(connection, {"method": "initialized", "params": {}})
            return connection
        except (CodexError, OSError):
            self._discard_connection(connection)
            raise

    def _spawn_connection(self) -> _Connection:
        base_command = self._command or resolve_codex_command()
        command = [*base_command, "app-server"]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=creation_flags,
            )
        except (OSError, ValueError) as exc:
            raise CodexConnectionError(f"codex app-server 시작 실패: {exc}") from exc

        connection = _Connection(
            process=process,
            inbox=queue.Queue(),
            stderr_lines=deque(maxlen=20),
        )
        threading.Thread(
            target=self._read_stdout,
            args=(connection,),
            name="codex-app-server-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(connection,),
            name="codex-app-server-stderr",
            daemon=True,
        ).start()
        return connection

    @staticmethod
    def _read_stdout(connection: _Connection) -> None:
        stdout = connection.process.stdout
        if stdout is None:
            connection.inbox.put(_StreamFailure("app-server stdout 파이프가 없습니다."))
            connection.disconnected.set()
            return

        try:
            for raw_line in stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    connection.inbox.put(
                        _StreamFailure(f"app-server가 잘못된 JSON을 반환했습니다: {exc}")
                    )
                    continue
                connection.inbox.put(message)
        except (OSError, ValueError) as exc:
            connection.inbox.put(_StreamFailure(f"stdout 읽기 실패: {exc}"))
        finally:
            connection.disconnected.set()
            connection.inbox.put(_StreamClosed(connection.process.poll()))

    @staticmethod
    def _read_stderr(connection: _Connection) -> None:
        stderr = connection.process.stderr
        if stderr is None:
            return
        try:
            for raw_line in stderr:
                line = raw_line.strip()
                if line:
                    connection.stderr_lines.append(line)
        except (OSError, ValueError):
            return

    def _take_request_id(self) -> int:
        request_id = self._next_request_id
        self._next_request_id += 1
        return request_id

    @staticmethod
    def _send(connection: _Connection, message: Mapping[str, Any]) -> None:
        stdin = connection.process.stdin
        if stdin is None or connection.process.poll() is not None:
            raise CodexConnectionError("app-server가 실행 중이 아닙니다.")
        try:
            stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise CodexConnectionError(f"app-server 전송 실패: {exc}") from exc

    def _exchange(
        self,
        connection: _Connection,
        message: Mapping[str, Any],
        request_id: int,
    ) -> Any:
        self._send(connection, message)
        deadline = time.monotonic() + self._request_timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexConnectionError(
                    f"app-server 응답 시간 초과 ({self._request_timeout:g}초)"
                )
            try:
                item = connection.inbox.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexConnectionError(
                    f"app-server 응답 시간 초과 ({self._request_timeout:g}초)"
                ) from exc

            if isinstance(item, _StreamFailure):
                raise CodexConnectionError(item.detail)
            if isinstance(item, _StreamClosed):
                stderr_tail = connection.stderr_lines[-1] if connection.stderr_lines else ""
                detail = f"app-server 연결 종료 (exit={item.return_code})"
                if stderr_tail:
                    detail += f": {stderr_tail}"
                raise CodexConnectionError(detail)
            if (
                not isinstance(item, Mapping)
                or "method" in item
                or item.get("id") != request_id
            ):
                continue
            if "error" in item:
                error = item.get("error")
                if isinstance(error, Mapping):
                    code = error.get("code")
                    text = error.get("message", "알 수 없는 RPC 오류")
                    raise CodexRpcError(f"app-server RPC 오류 {code}: {text}")
                raise CodexRpcError(f"app-server RPC 오류: {error}")
            if "result" not in item:
                raise CodexConnectionError("app-server 응답에 result가 없습니다.")
            return item["result"]

    def _discard_connection(self, connection: _Connection) -> None:
        with self._state_lock:
            if self._connection is connection:
                self._connection = None
        self._stop_connection(connection)

    @staticmethod
    def _stop_connection(connection: _Connection) -> None:
        with connection.stop_lock:
            if connection.stopped:
                return
            connection.stopped = True
            connection.disconnected.set()
            process = connection.process

            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

            if process.poll() is None:
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass

            if process.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4.0,
                            check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                else:
                    try:
                        process.terminate()
                    except OSError:
                        pass

            if process.poll() is None:
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=2.0)
                    except (OSError, subprocess.TimeoutExpired):
                        pass

            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._state_lock:
            connection = self._connection
            self._connection = None
        if connection is not None:
            self._stop_connection(connection)


class UsagePoller(threading.Thread):
    """Poll one usage source on its own thread.

    Both sources share this loop; a failure on one never stops the other.  The
    client only has to provide ``read_rate_limits``, ``is_disconnected`` and
    ``shutdown``.
    """

    def __init__(
        self,
        client: Any,
        output: "queue.Queue[UiMessage]",
        *,
        source: str = SOURCE_CODEX,
        parse: Callable[[Mapping[str, Any]], UsageSnapshot] = select_usage_windows,
        unavailable_detail: str = "기본 Codex 주간 한도를 찾지 못했습니다.",
        rpc_errors: tuple[type[BaseException], ...] = (CodexRpcError,),
        connection_errors: tuple[type[BaseException], ...] = (
            CodexConnectionError,
            OSError,
        ),
        refresh_seconds: float = REFRESH_SECONDS,
        reconnect_delays: Sequence[float] = RECONNECT_DELAYS_SECONDS,
    ) -> None:
        super().__init__(name=f"{source}-usage-poller", daemon=True)
        self.client = client
        self.output = output
        self.source = source
        self.parse = parse
        self.unavailable_detail = unavailable_detail
        self.rpc_errors = rpc_errors
        self.connection_errors = connection_errors
        self.refresh_seconds = refresh_seconds
        self.reconnect_delays = tuple(reconnect_delays) or (1.0,)
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def _wait_or_disconnect(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.stop_event.is_set():
            if self.client.is_disconnected:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.stop_event.wait(min(0.25, remaining))
        return True

    def run(self) -> None:
        failure_count = 0
        try:
            while not self.stop_event.is_set():
                try:
                    result = self.client.read_rate_limits()
                    snapshot = self.parse(result)
                    if snapshot.available:
                        self.output.put(
                            UiMessage("snapshot", snapshot=snapshot, source=self.source)
                        )
                    else:
                        self.output.put(
                            UiMessage(
                                "unavailable",
                                detail=self.unavailable_detail,
                                source=self.source,
                            )
                        )
                    failure_count = 0
                    if self._wait_or_disconnect(self.refresh_seconds):
                        break
                except self.rpc_errors as exc:
                    self.output.put(
                        UiMessage("error", detail=str(exc), source=self.source)
                    )
                    failure_count = 0
                    if self.stop_event.wait(self.refresh_seconds):
                        break
                except self.connection_errors as exc:
                    self.output.put(
                        UiMessage("reconnecting", detail=str(exc), source=self.source)
                    )
                    delay = self.reconnect_delays[
                        min(failure_count, len(self.reconnect_delays) - 1)
                    ]
                    failure_count += 1
                    if self.stop_event.wait(delay):
                        break
        finally:
            self.client.shutdown()


def remaining_color(window: RateWindow) -> str:
    progress = window.progress_percent
    return GREEN if progress >= 50 else AMBER if progress >= 20 else RED


class UsageRow:
    def __init__(self, parent: tk.Widget, y: int, label: str) -> None:
        self._label_cache: dict[str, Any] = {}
        self._percent_cache: dict[str, Any] = {}
        self._reset_cache: dict[str, Any] = {}
        self._fill_width: Optional[int] = None
        self._fill_color: Optional[str] = None
        self.title = label

        self.label = tk.Label(
            parent,
            text=label,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        )
        self.label.place(x=_s(10), y=_s(y), width=_s(80), height=_s(18))

        self.percent = tk.Label(
            parent,
            text="—",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 10),
            anchor="e",
        )
        self.percent.place(x=_s(190), y=_s(y), width=_s(78), height=_s(18))

        self.bar = tk.Canvas(
            parent,
            bg=BAR_BG,
            highlightthickness=0,
            bd=0,
        )
        self.bar.place(
            x=_s(10),
            y=_s(y + 21),
            width=_s(DETAIL_BAR_WIDTH),
            height=_s(6),
        )
        self.fill = self.bar.create_rectangle(0, 0, 0, _s(6), fill=GREEN, outline="")

        self.reset = tk.Label(
            parent,
            text="Reset —",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="e",
        )
        self.reset.place(x=_s(90), y=_s(y + 29), width=_s(178), height=_s(14))

    def set_title(self, title: str) -> None:
        self.title = title
        _configure_once(self.label, self._label_cache, text=title)

    def _set_fill(self, width: int, color: str) -> None:
        if width != self._fill_width:
            self.bar.coords(self.fill, 0, 0, width, _s(6))
            self._fill_width = width
        if color != self._fill_color:
            self.bar.itemconfigure(self.fill, fill=color)
            self._fill_color = color

    def set_window(self, window: Optional[RateWindow]) -> None:
        if window is None:
            _configure_once(self.percent, self._percent_cache, text="—", fg=MUTED)
            _configure_once(self.reset, self._reset_cache, text="Reset —")
            self._set_fill(0, self._fill_color or GREEN)
            return

        color = remaining_color(window)
        _configure_once(
            self.percent,
            self._percent_cache,
            text=format_percent(window.remaining_percent),
            fg=color,
        )
        _configure_once(
            self.reset,
            self._reset_cache,
            text=format_reset_time(window.resets_at),
        )
        self._set_fill(
            round(_s(DETAIL_BAR_WIDTH) * window.progress_percent / 100.0),
            color,
        )


class CompactUsage:
    def __init__(
        self,
        parent: tk.Widget,
        x: int,
        width: int,
        label: str,
        *,
        reset_width: Optional[int] = None,
    ) -> None:
        self.prefix = label
        self.width = width
        self._window: Optional[RateWindow] = None
        self._label_cache: dict[str, Any] = {}
        self._reset_cache: dict[str, Any] = {}

        self.label = tk.Label(
            parent,
            text=f"{label} —",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        )
        self.label.place(x=_s(x), y=_s(1), width=_s(width), height=_s(17))

        self.reset = tk.Label(
            parent,
            text="R —",
            bg=BG,
            fg=COMPACT_RESET_TEXT,
            font=("Segoe UI Semibold", 7),
            anchor="w",
        )
        self.reset.place(
            x=_s(x),
            y=_s(17),
            width=_s(reset_width or width),
            height=_s(12),
        )

    def set_prefix(self, prefix: str) -> None:
        """Change the caption without blanking the value already on screen."""

        if prefix == self.prefix:
            return
        self.prefix = prefix
        self._render()

    def set_window(self, window: Optional[RateWindow]) -> None:
        self._window = window
        self._render()

    def _render(self) -> None:
        window = self._window
        if window is None:
            _configure_once(
                self.label,
                self._label_cache,
                text=f"{self.prefix} —",
                fg=MUTED,
            )
            _configure_once(self.reset, self._reset_cache, text="R —")
            return

        _configure_once(
            self.label,
            self._label_cache,
            text=f"{self.prefix} {format_percent(window.remaining_percent)}",
            fg=remaining_color(window),
        )
        _configure_once(
            self.reset,
            self._reset_cache,
            text=format_compact_reset_time(window.resets_at),
        )


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _AppBarData(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hWnd", ctypes.c_void_p),
        ("uCallbackMessage", ctypes.c_uint32),
        ("uEdge", ctypes.c_uint32),
        ("rc", _Rect),
        ("lParam", ctypes.c_ssize_t),
    ]


def _taskbar_autohidden() -> bool:
    """True when the taskbar is in auto-hide mode.

    An auto-hidden taskbar still reports ``IsWindowVisible``, but its rect sits
    mostly off-screen, so anchoring to it parks the HUD where nobody can see it.
    """

    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHAppBarMessage.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(_AppBarData),
        ]
        shell32.SHAppBarMessage.restype = ctypes.c_ssize_t
        data = _AppBarData()
        data.cbSize = ctypes.sizeof(_AppBarData)
        state = shell32.SHAppBarMessage(0x00000004, ctypes.byref(data))  # ABM_GETSTATE
        return bool(int(state) & 0x00000001)  # ABS_AUTOHIDE
    except (AttributeError, OSError, ValueError):
        return False


def compact_size() -> tuple[int, int]:
    return _s(COMPACT_WIDTH), _s(COMPACT_HEIGHT)


def detail_size() -> tuple[int, int]:
    return _s(WINDOW_WIDTH), _s(WINDOW_HEIGHT)


@dataclass(frozen=True)
class _TaskbarLayout:
    left: int
    top: int
    right: int
    bottom: int
    task_buttons_left: Optional[int]
    dpi: int

    @property
    def horizontal(self) -> bool:
        return (self.right - self.left) >= (self.bottom - self.top)


def _taskbar_layout() -> Optional[_TaskbarLayout]:
    if os.name != "nt":
        return None

    user32 = ctypes.windll.user32
    try:
        find_window = user32.FindWindowW
        find_window.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        find_window.restype = ctypes.c_void_p
        find_window_ex = user32.FindWindowExW
        find_window_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
        ]
        find_window_ex.restype = ctypes.c_void_p
        get_window_rect = user32.GetWindowRect
        get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
        get_window_rect.restype = ctypes.c_bool
        is_window_visible = user32.IsWindowVisible
        is_window_visible.argtypes = [ctypes.c_void_p]
        is_window_visible.restype = ctypes.c_bool

        taskbar = find_window("Shell_TrayWnd", None)
        if not taskbar or not is_window_visible(taskbar):
            return None
        if _taskbar_autohidden():
            return None

        rect = _Rect()
        if not get_window_rect(taskbar, ctypes.byref(rect)):
            return None

        task_buttons_left = None
        task_buttons = find_window_ex(taskbar, None, "ReBarWindow32", None)
        if task_buttons:
            buttons_rect = _Rect()
            if get_window_rect(task_buttons, ctypes.byref(buttons_rect)):
                task_buttons_left = int(buttons_rect.left)

        try:
            get_dpi_for_window = user32.GetDpiForWindow
            get_dpi_for_window.argtypes = [ctypes.c_void_p]
            get_dpi_for_window.restype = ctypes.c_uint
            dpi = int(get_dpi_for_window(taskbar)) or 96
        except AttributeError:
            dpi = 96

        return _TaskbarLayout(
            left=int(rect.left),
            top=int(rect.top),
            right=int(rect.right),
            bottom=int(rect.bottom),
            task_buttons_left=task_buttons_left,
            dpi=dpi,
        )
    except (AttributeError, OSError, ValueError):
        return None


def _work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    left = 0
    top = 0
    right = root.winfo_screenwidth()
    bottom = root.winfo_screenheight()

    if os.name == "nt":
        rect = _Rect()
        try:
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        except (AttributeError, OSError):
            pass
    return left, top, right, bottom


def _initial_position(root: tk.Tk) -> tuple[int, int]:
    margin = _s(18)
    width, height = detail_size()
    left, top, right, bottom = _work_area(root)

    x = max(left, right - width - margin)
    y = max(top, bottom - height - margin)
    return x, y


def _compact_initial_position(root: tk.Tk) -> tuple[int, int]:
    width, height = compact_size()
    layout = _taskbar_layout()
    if layout is not None and layout.horizontal:
        scale = max(0.75, layout.dpi / 96.0)
        x = layout.left + round(162 * scale)
        if layout.task_buttons_left is not None:
            x = min(x, layout.task_buttons_left - width - round(12 * scale))
        x = max(layout.left + 6, min(x, layout.right - width - 6))
        y = layout.top + max(0, (layout.bottom - layout.top - height) // 2)
        return x, y

    # No usable taskbar (vertical, hidden, or auto-hide): sit just above the
    # bottom edge of the work area so the HUD stays visible either way.
    left, top, right, bottom = _work_area(root)
    x = max(left, right - width - _s(18))
    y = max(top, bottom - height - _s(4))
    return x, y


def _monitor_rectangles(root: tk.Tk) -> list[tuple[int, int, int, int]]:
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            callback_type = ctypes.WINFUNCTYPE(
                ctypes.c_bool,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(_Rect),
                ctypes.c_ssize_t,
            )
            rectangles: list[tuple[int, int, int, int]] = []

            def collect_monitor(
                _monitor: Any,
                _device_context: Any,
                rect_pointer: Any,
                _data: Any,
            ) -> bool:
                rect = rect_pointer.contents
                rectangles.append(
                    (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                )
                return True

            callback = callback_type(collect_monitor)
            enum_monitors = user32.EnumDisplayMonitors
            enum_monitors.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                callback_type,
                ctypes.c_ssize_t,
            ]
            enum_monitors.restype = ctypes.c_bool
            if enum_monitors(None, None, callback, 0) and rectangles:
                return rectangles
        except (AttributeError, OSError, ValueError):
            pass
    return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]


def _fit_position_to_monitors(
    position: Optional[tuple[int, int]],
    width: int,
    height: int,
    monitors: Sequence[tuple[int, int, int, int]],
) -> Optional[tuple[int, int]]:
    if position is None:
        return None

    x, y = position
    visible_width = min(48, width)
    visible_height = min(24, height)
    best: Optional[tuple[int, int]] = None
    best_area = -1
    for left, top, right, bottom in monitors:
        intersection_width = max(0, min(x + width, right) - max(x, left))
        intersection_height = max(0, min(y + height, bottom) - max(y, top))
        if intersection_width < visible_width or intersection_height < visible_height:
            continue

        max_x = max(left, right - width)
        max_y = max(top, bottom - height)
        candidate = (max(left, min(x, max_x)), max(top, min(y, max_y)))
        area = intersection_width * intersection_height
        if area > best_area:
            best = candidate
            best_area = area
    return best


def _restore_visible_position(
    root: tk.Tk,
    position: Optional[tuple[int, int]],
    width: int,
    height: int,
) -> Optional[tuple[int, int]]:
    return _fit_position_to_monitors(
        position,
        width,
        height,
        _monitor_rectangles(root),
    )


def _window_handle(root: tk.Tk) -> Optional[int]:
    """Resolve this HUD's own top-level HWND.

    ``wm frame`` is Tk's own answer and the only reliable one here: ``winfo_id``
    returns the inner Tk window, and ``GetAncestor(GA_ROOT)`` on it comes back
    unchanged rather than reaching the ``TkTopLevel`` wrapper that actually owns
    the z-order.  Looking the window up by title is not an option either --
    ``FindWindowW(None, title)`` matches the first window in the whole session
    with that caption, which may belong to another process entirely.
    """

    if os.name != "nt":
        return None
    try:
        frame = root.wm_frame()
        if frame:
            handle = int(frame, 16)
            if _is_window(handle):
                return handle
    except (tk.TclError, TypeError, ValueError):
        pass

    try:
        user32 = ctypes.windll.user32
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        get_ancestor.restype = ctypes.c_void_p
        inner = root.winfo_id()
        handle = get_ancestor(ctypes.c_void_p(inner), 2) or inner  # GA_ROOT
        return int(handle) if handle else None
    except (AttributeError, OSError, ValueError, tk.TclError):
        return None


def _is_window(handle: Optional[int]) -> bool:
    if not handle or os.name != "nt":
        return False
    try:
        is_window = ctypes.windll.user32.IsWindow
        is_window.argtypes = [ctypes.c_void_p]
        is_window.restype = ctypes.c_bool
        return bool(is_window(ctypes.c_void_p(handle)))
    except (AttributeError, OSError, ValueError):
        return False


def _has_topmost_style(handle: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        ex_style = int(get_long(ctypes.c_void_p(handle), -20))  # GWL_EXSTYLE
        return bool(ex_style & 0x00000008)  # WS_EX_TOPMOST
    except (AttributeError, OSError, ValueError):
        return False


def _window_is_covered(handle: Optional[int]) -> bool:
    """True when something else owns the pixels at the centre of the HUD.

    ``WS_EX_TOPMOST`` staying set is not proof of visibility: ``Shell_TrayWnd``
    is topmost too, and after it redraws it can sit above us inside the same
    band.  Asking who owns our own centre point is the only check that matches
    what the user actually sees.
    """

    if not handle or os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        rect = _Rect()
        get_window_rect = user32.GetWindowRect
        get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
        get_window_rect.restype = ctypes.c_bool
        if not get_window_rect(ctypes.c_void_p(handle), ctypes.byref(rect)):
            return False
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return False

        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        point = _Point(
            (rect.left + rect.right) // 2,
            (rect.top + rect.bottom) // 2,
        )
        window_from_point = user32.WindowFromPoint
        window_from_point.argtypes = [_Point]
        window_from_point.restype = ctypes.c_void_p
        hit = window_from_point(point)
        if not hit:
            return False

        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        get_ancestor.restype = ctypes.c_void_p
        root_hit = get_ancestor(ctypes.c_void_p(hit), 2) or hit  # GA_ROOT
        return int(root_hit) != int(handle)
    except (AttributeError, OSError, ValueError):
        return False


def _raise_window_above_taskbar(root: tk.Tk, handle: Optional[int] = None) -> bool:
    """Reassert the HUD at the top of the topmost band without activation.

    ``SWP_SHOWWINDOW`` is required here: ``SetWindowPos(HWND_TOPMOST)`` on a
    window that already carries ``WS_EX_TOPMOST`` does not move it inside the
    band, so without it the taskbar stays on top.  It repaints a layered
    window, which is why callers only reach this when the HUD is really
    covered rather than on every timer tick.
    """

    if os.name != "nt":
        try:
            root.lift()
            return True
        except tk.TclError:
            return False

    if handle is None:
        handle = _window_handle(root)
    if not handle:
        return False

    try:
        user32 = ctypes.windll.user32
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        set_window_pos.restype = ctypes.c_bool
        # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
        flags = 0x0001 | 0x0002 | 0x0010 | 0x0040
        return bool(
            set_window_pos(
                ctypes.c_void_p(handle),
                ctypes.c_void_p(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                flags,
            )
        )
    except (AttributeError, OSError, ValueError, tk.TclError):
        return False


class CodexUsageWidget:
    def __init__(
        self,
        root: tk.Tk,
        *,
        client: Optional[CodexAppServerClient] = None,
        claude_client: Optional[Any] = None,
        autostart: bool = True,
        state_store: Optional[WindowStateStore] = None,
        persist_state: bool = True,
        snapshot_store: Optional[UsageSnapshotStore] = None,
        persist_snapshot: Optional[bool] = None,
        alert_tracker: Optional[AlertTracker] = None,
        notifier: Optional[TrayNotifier] = None,
        alerts_enabled: bool = False,
    ) -> None:
        self.root = root
        self.client = client or CodexAppServerClient()
        self.claude_client = claude_client or ClaudeUsageClient()
        self.messages: "queue.Queue[UiMessage]" = queue.Queue()
        self.poller = UsagePoller(self.client, self.messages)
        self.claude_poller = UsagePoller(
            self.claude_client,
            self.messages,
            source=SOURCE_CLAUDE,
            parse=select_claude_windows,
            unavailable_detail="Claude 구독 한도를 찾지 못했습니다.",
            rpc_errors=(ClaudeUnavailableError,),
            connection_errors=(ClaudeConnectionError, ClaudeUsageError, OSError),
            refresh_seconds=CLAUDE_REFRESH_SECONDS,
        )
        self._closed = False
        self._drag_offset = (0, 0)
        self._position: Optional[tuple[int, int]] = None
        self._after_id: Optional[str] = None
        self._topmost_after_id: Optional[str] = None
        self._display_after_id: Optional[str] = None
        self._save_after_id: Optional[str] = None
        self._flash_after_id: Optional[str] = None
        self._notify_after_id: Optional[str] = None
        self._hwnd: Optional[int] = None
        self._raise_failures = 0
        self._last_raise_log = 0.0
        self._covered_raises = 0
        self.alerts_enabled = alerts_enabled
        self.alert_tracker = alert_tracker or AlertTracker()
        self.notifier = notifier or TrayNotifier()
        self._persist_state = persist_state
        self.state_store = state_store or WindowStateStore()
        self._persist_snapshot = (
            persist_state if persist_snapshot is None else persist_snapshot
        )
        self.snapshot_store = snapshot_store or UsageSnapshotStore()
        self._snapshots: dict[str, UsageSnapshot] = (
            self.snapshot_store.load() if self._persist_snapshot else {}
        )
        self._source_status: dict[str, str] = {
            source: "cached" if source in self._snapshots else "pending"
            for source in SOURCES
        }
        self._window_state = (
            self.state_store.load() if self._persist_state else None
        ) or WindowState()

        root.title("Codex Usage")
        root.configure(bg=BORDER)
        root.overrideredirect(True)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.92)
        except tk.TclError:
            pass
        try:
            root.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        self.detail_panel = tk.Frame(
            root,
            bg=BG,
            highlightbackground=BORDER,
            highlightthickness=1,
        )

        self.title_label = tk.Label(
            self.detail_panel,
            text="USAGE 남은 %",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        )
        self.title_label.place(x=_s(10), y=_s(5), width=_s(105), height=_s(19))

        self.status_label = tk.Label(
            self.detail_panel,
            text="CONNECTING",
            bg=BG,
            fg=AMBER,
            font=("Segoe UI", 7),
            anchor="e",
        )
        self.status_label.place(x=_s(116), y=_s(6), width=_s(94), height=_s(17))
        self._status_cache: dict[str, Any] = {}
        self._compact_status_cache: dict[str, Any] = {}

        self.detail_toggle_button = tk.Button(
            self.detail_panel,
            text="▾",
            command=self.show_compact,
            bg=BG,
            fg=MUTED,
            activebackground="#2a2d35",
            activeforeground=TEXT,
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
            cursor="hand2",
            takefocus=False,
        )
        self.detail_toggle_button.place(x=_s(214), y=_s(2), width=_s(28), height=_s(24))

        self.close_button = tk.Button(
            self.detail_panel,
            text="×",
            command=self.close,
            bg=BG,
            fg=MUTED,
            activebackground="#2a2d35",
            activeforeground=TEXT,
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 12),
            cursor="hand2",
            takefocus=False,
        )
        self.close_button.place(x=_s(244), y=_s(2), width=_s(28), height=_s(24))

        codex_labels = default_snapshot_labels(SOURCE_CODEX)
        claude_labels = default_snapshot_labels(SOURCE_CLAUDE)
        self.weekly_row = UsageRow(self.detail_panel, DETAIL_ROW_YS[0], codex_labels[1])
        self.claude_five_hour_row = UsageRow(
            self.detail_panel, DETAIL_ROW_YS[1], claude_labels[0]
        )
        self.claude_weekly_row = UsageRow(
            self.detail_panel, DETAIL_ROW_YS[2], claude_labels[1]
        )

        self.compact_panel = tk.Frame(
            root,
            bg=BG,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        self.compact_status = tk.Label(
            self.compact_panel,
            text="●",
            bg=BG,
            fg=AMBER,
            font=("Segoe UI", 7),
            anchor="center",
        )
        self.compact_status.place(x=_s(3), y=_s(3), width=_s(9), height=_s(13))
        self.compact_weekly = CompactUsage(
            self.compact_panel,
            COMPACT_COLUMN_XS[0],
            COMPACT_COLUMN_WIDTH,
            codex_labels[1],
            reset_width=COMPACT_RESET_WIDTH,
        )
        self.compact_claude_five_hour = CompactUsage(
            self.compact_panel,
            COMPACT_COLUMN_XS[1],
            COMPACT_COLUMN_WIDTH,
            claude_labels[0],
            reset_width=COMPACT_RESET_WIDTH,
        )
        self.compact_claude_weekly = CompactUsage(
            self.compact_panel,
            COMPACT_COLUMN_XS[2],
            COMPACT_COLUMN_WIDTH,
            claude_labels[1],
            reset_width=COMPACT_RESET_WIDTH,
        )
        self.compact_toggle_button = tk.Button(
            self.compact_panel,
            text="▴",
            command=self.show_details,
            bg=BG,
            fg=MUTED,
            activebackground="#2a2d35",
            activeforeground=TEXT,
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 8),
            cursor="hand2",
            takefocus=False,
        )
        self.compact_toggle_button.place(
            x=_s(COMPACT_WIDTH - 40), y=0, width=_s(18), height=_s(17)
        )
        self.compact_close_button = tk.Button(
            self.compact_panel,
            text="×",
            command=self.close,
            bg=BG,
            fg=MUTED,
            activebackground="#2a2d35",
            activeforeground=TEXT,
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 9),
            cursor="hand2",
            takefocus=False,
        )
        self.compact_close_button.place(
            x=_s(COMPACT_WIDTH - 20), y=0, width=_s(18), height=_s(17)
        )

        self._compact_mode = self._window_state.compact_mode
        self._bind_drag_widgets(self.detail_panel)
        self._bind_drag_widgets(self.compact_panel)
        root.bind("<Escape>", lambda _event: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)

        if self._window_state.compact_mode:
            self.show_compact(initial=True)
        else:
            self.show_details(initial=True)

        self._render_all()
        self._render_status()
        self._seed_alerts_from_cache()

        self._display_signature = self._current_display_signature()
        LOG.info(
            "HUD ready: mode=%s position=%s scale=%.2f cached=%s",
            "compact" if self._compact_mode else "detail",
            self._position,
            ui_scale(),
            sorted(self._snapshots),
        )
        self._refresh_topmost()
        self._after_id = root.after(100, self._drain_messages)
        self._display_after_id = root.after(DISPLAY_WATCH_MS, self._watch_display)
        if autostart:
            self.poller.start()
            self.claude_poller.start()

    def _bind_drag_widgets(self, widget: tk.Widget) -> None:
        if not isinstance(widget, tk.Button):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._finish_drag)
        for child in widget.winfo_children():
            self._bind_drag_widgets(child)

    def show_compact(self, *, initial: bool = False) -> None:
        if self._closed:
            return
        if not initial:
            self._remember_current_position()

        width, height = compact_size()
        # A saved position is the user's choice: honour it in every case, and
        # only fall back to the taskbar slot when there is nothing usable saved.
        saved = _restore_visible_position(
            self.root,
            self._window_state.compact_position,
            width,
            height,
        )
        if saved is not None:
            x, y = saved
        else:
            x, y = _compact_initial_position(self.root)

        self.detail_panel.place_forget()
        self.compact_panel.place(x=0, y=0, width=width, height=height)
        self._set_window_geometry(width, height, x, y)
        self._compact_mode = True
        self._window_state = replace(
            self._window_state,
            compact_mode=True,
            compact_position=(x, y),
        )
        if not initial:
            self._save_window_state()

    def show_details(self, *, initial: bool = False) -> None:
        if self._closed:
            return
        if not initial:
            self._remember_current_position()

        width, height = detail_size()
        saved = _restore_visible_position(
            self.root,
            self._window_state.detail_position,
            width,
            height,
        )
        if saved is not None:
            x, y = saved
        else:
            x, y = _initial_position(self.root)

        self.compact_panel.place_forget()
        self.detail_panel.place(x=0, y=0, width=width, height=height)
        self._set_window_geometry(width, height, x, y)
        self._compact_mode = False
        self._window_state = replace(
            self._window_state,
            compact_mode=False,
            detail_position=(x, y),
        )
        if not initial:
            self._save_window_state()

    def _set_window_geometry(self, width: int, height: int, x: int, y: int) -> None:
        # ``overrideredirect`` windows are not user-resizable, so the old
        # resizable(True)/update/resizable(False) dance only produced a visible
        # jump on every mode switch.
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self._position = (x, y)
        _raise_window_above_taskbar(self.root, self._handle())

    def _handle(self) -> Optional[int]:
        # Resolved fresh every time on purpose: before the window is first
        # mapped ``wm frame`` still reports the inner Tk window, and that stale
        # value stays a valid HWND forever, so caching it would silently pin us
        # to a window that owns no z-order.  The lookup is a single Tcl call.
        self._hwnd = _window_handle(self.root)
        return self._hwnd

    def _refresh_topmost(self) -> None:
        """Keep the HUD in the topmost band, and never stop trying."""

        if self._closed:
            return
        try:
            handle = self._handle()
            if handle is None or not _has_topmost_style(handle):
                self.root.attributes("-topmost", True)
                needs_raise = True
            else:
                # Cheap check every tick; the expensive, repainting raise only
                # happens when the HUD is actually hidden behind something.
                needs_raise = _window_is_covered(handle)

            if needs_raise:
                self._covered_raises += 1
                if _raise_window_above_taskbar(self.root, handle):
                    self._raise_failures = 0
                else:
                    self._raise_failures += 1
                    self._hwnd = None
                self._log_raise_activity()
        except (tk.TclError, OSError, ValueError) as exc:
            self._raise_failures += 1
            self._hwnd = None
            LOG.warning("topmost refresh failed: %s", exc)
        finally:
            if not self._closed:
                self._topmost_after_id = self.root.after(
                    TOPMOST_REFRESH_MS,
                    self._refresh_topmost,
                )

    def _log_raise_activity(self) -> None:
        """Summarise z-order work at most once a minute.

        A fullscreen app can keep the HUD covered, so this must never log per
        tick -- but a silent HUD that keeps re-raising is exactly the evidence
        needed the next time it misbehaves.
        """

        now = time.monotonic()
        if now - self._last_raise_log < RAISE_LOG_INTERVAL_SECONDS:
            return
        self._last_raise_log = now
        LOG.info(
            "re-raised above covering window (%d raise(s), %d failure(s) since last report)",
            self._covered_raises,
            self._raise_failures,
        )
        self._covered_raises = 0

    def _current_display_signature(self) -> tuple[Any, ...]:
        try:
            monitors = tuple(_monitor_rectangles(self.root))
        except (tk.TclError, OSError, ValueError):
            return ()
        layout = _taskbar_layout()
        taskbar = (
            (layout.left, layout.top, layout.right, layout.bottom, layout.dpi)
            if layout is not None
            else None
        )
        return (monitors, taskbar)

    def _watch_display(self) -> None:
        """Re-place the HUD when the monitor layout or taskbar actually moves."""

        if self._closed:
            return
        try:
            signature = self._current_display_signature()
            if signature and signature != self._display_signature:
                LOG.info(
                    "display layout changed: %s -> %s",
                    self._display_signature,
                    signature,
                )
                self._display_signature = signature
                if self._compact_mode:
                    self.show_compact()
                else:
                    self.show_details()
        except (tk.TclError, OSError, ValueError) as exc:
            LOG.warning("display watch failed: %s", exc)
        finally:
            if not self._closed:
                self._display_after_id = self.root.after(
                    DISPLAY_WATCH_MS,
                    self._watch_display,
                )

    def _start_drag(self, event: tk.Event[Any]) -> None:
        origin = self._position or (self.root.winfo_x(), self.root.winfo_y())
        self._drag_offset = (event.x_root - origin[0], event.y_root - origin[1])

    def _drag(self, event: tk.Event[Any]) -> None:
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"{x:+d}{y:+d}")
        self._position = (x, y)

    def _finish_drag(self, _event: Optional[tk.Event[Any]] = None) -> None:
        # A drag can end off-screen; snap back before the position is stored.
        width, height = compact_size() if self._compact_mode else detail_size()
        current = self._position
        fitted = _restore_visible_position(self.root, current, width, height)
        if fitted is not None and fitted != current:
            self.root.geometry(f"{fitted[0]:+d}{fitted[1]:+d}")
            self._position = fitted
        self._save_window_state()

    def _remember_current_position(self) -> None:
        # Track our own position instead of asking Tk: ``winfo_x`` lags behind a
        # geometry request until the window manager processes it.
        position = self._position
        if position is None:
            return
        if self._compact_mode:
            self._window_state = replace(
                self._window_state,
                compact_mode=True,
                compact_position=position,
            )
        else:
            self._window_state = replace(
                self._window_state,
                compact_mode=False,
                detail_position=position,
            )

    def _save_window_state(self) -> None:
        if not self._persist_state:
            return
        self._remember_current_position()
        self.state_store.save(self._window_state)

    def _drain_messages(self) -> None:
        if self._closed:
            return
        # Keep the newest message per source: a Codex update must not be thrown
        # away just because a Claude message arrived in the same tick.
        latest: dict[str, UiMessage] = {}
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            latest[message.source] = message

        for source in SOURCES:
            message = latest.get(source)
            if message is not None:
                self._apply_message(message)
        self._after_id = self.root.after(150, self._drain_messages)

    def _apply_message(self, message: UiMessage) -> None:
        source = message.source
        previous_status = self._source_status.get(source)
        if message.kind == "snapshot" and message.snapshot is not None:
            self._snapshots[source] = message.snapshot
            self._source_status[source] = "live"
            self._render_source(source)
            self._schedule_snapshot_save()
            self._check_alerts(source, message.snapshot)
        else:
            self._source_status[source] = message.kind
            if source not in self._snapshots:
                self._render_source(source)

        if self._source_status[source] != previous_status:
            level = logging.INFO if message.kind == "snapshot" else logging.WARNING
            LOG.log(
                level,
                "%s status %s -> %s%s",
                source,
                previous_status,
                self._source_status[source],
                f": {message.detail}" if message.detail else "",
            )
        self._render_status()

    def _seed_alerts_from_cache(self) -> None:
        """Record the cached levels without announcing them.

        Otherwise every restart would re-toast a limit the user was already
        told about before the HUD went down.
        """

        for source, snapshot in self._snapshots.items():
            for key, window in (
                (f"{source}:five_hour", snapshot.five_hour),
                (f"{source}:weekly", snapshot.weekly),
            ):
                if window is None:
                    continue
                self.alert_tracker.evaluate(
                    key,
                    "",
                    window.used_percent,
                    window.resets_at,
                )

    def _check_alerts(self, source: str, snapshot: UsageSnapshot) -> None:
        if not self.alerts_enabled:
            return
        labels = default_snapshot_labels(source)
        candidates = (
            (f"{source}:five_hour", snapshot.five_hour_label or labels[0], snapshot.five_hour),
            (f"{source}:weekly", snapshot.weekly_label or labels[1], snapshot.weekly),
        )
        for key, label, window in candidates:
            if window is None:
                continue
            event = self.alert_tracker.evaluate(
                key,
                label,
                window.used_percent,
                window.resets_at,
            )
            if event is not None:
                self._fire_alert(event, window)

    def _fire_alert(self, event: AlertEvent, window: RateWindow) -> None:
        LOG.warning(
            "alert %s %s used=%.1f%% resets_at=%s",
            event.level_name,
            event.key,
            event.used_percent,
            event.resets_at,
        )
        critical = event.level >= 2
        self._flash_border(RED if critical else AMBER)

        title, body = describe_alert(event, format_compact_reset_time(window.resets_at))
        if self.notifier.notify(self._handle(), title, body, critical=critical):
            if self._notify_after_id is not None:
                try:
                    self.root.after_cancel(self._notify_after_id)
                except tk.TclError:
                    pass
            # Drop the tray icon once the toast has had time to show, so the
            # HUD does not leave a permanent icon behind.
            self._notify_after_id = self.root.after(
                ALERT_NOTIFY_LINGER_MS,
                self._close_notifier,
            )
        else:
            LOG.warning("alert notification could not be shown")

    def _close_notifier(self) -> None:
        self._notify_after_id = None
        self.notifier.close()

    def _flash_border(self, color: str, remaining: int = ALERT_FLASH_STEPS) -> None:
        if self._closed:
            return
        value = color if remaining % 2 == 1 else BORDER
        for panel in (self.detail_panel, self.compact_panel):
            try:
                panel.configure(highlightbackground=value)
            except tk.TclError:
                pass
        try:
            self.root.configure(bg=value)
        except tk.TclError:
            pass

        if remaining > 0:
            self._flash_after_id = self.root.after(
                ALERT_FLASH_MS,
                self._flash_border,
                color,
                remaining - 1,
            )
        else:
            self._flash_after_id = None

    def _schedule_snapshot_save(self) -> None:
        """Write the cache off the redraw path, at most once per idle tick."""

        if not self._persist_snapshot or self._closed:
            return
        if self._save_after_id is not None:
            return
        self._save_after_id = self.root.after_idle(self._save_snapshots)

    def _save_snapshots(self) -> None:
        self._save_after_id = None
        if self._closed:
            return
        self.snapshot_store.save(self._snapshots)

    def _render_all(self) -> None:
        for source in SOURCES:
            self._render_source(source)

    def _render_source(self, source: str) -> None:
        snapshot = self._snapshots.get(source)
        if source == SOURCE_CODEX:
            labels = default_snapshot_labels(SOURCE_CODEX)
            weekly_label = snapshot.weekly_label if snapshot else labels[1]
            self.weekly_row.set_title(weekly_label)
            self.compact_weekly.set_prefix(weekly_label)
            weekly = snapshot.weekly if snapshot else None
            self.weekly_row.set_window(weekly)
            self.compact_weekly.set_window(weekly)
            return

        labels = default_snapshot_labels(SOURCE_CLAUDE)
        five_hour_label = snapshot.five_hour_label if snapshot else labels[0]
        weekly_label = snapshot.weekly_label if snapshot else labels[1]
        five_hour = snapshot.five_hour if snapshot else None
        weekly = snapshot.weekly if snapshot else None

        self.claude_five_hour_row.set_title(five_hour_label)
        self.claude_weekly_row.set_title(weekly_label)
        self.compact_claude_five_hour.set_prefix(five_hour_label)
        self.compact_claude_weekly.set_prefix(weekly_label)
        self.claude_five_hour_row.set_window(five_hour)
        self.claude_weekly_row.set_window(weekly)
        self.compact_claude_five_hour.set_window(five_hour)
        self.compact_claude_weekly.set_window(weekly)

    def _source_indicator(self, source: str) -> tuple[str, str, int]:
        """Return ``(text, colour, severity)`` for one source."""

        kind = self._source_status.get(source, "pending")
        has_data = source in self._snapshots
        if kind == "live":
            return "LIVE", GREEN, 0
        if kind == "cached":
            return "CACHED", AMBER, 1
        if kind == "pending":
            return "CONNECTING", AMBER, 1
        if kind == "reconnecting":
            return ("OFFLINE" if has_data else "RECONNECTING", AMBER, 1 if has_data else 2)
        if kind == "unavailable":
            return ("STALE" if has_data else "NO MATCH", AMBER if has_data else RED,
                    1 if has_data else 3)
        return ("STALE" if has_data else "ERROR", AMBER if has_data else RED,
                1 if has_data else 3)

    def _render_status(self) -> None:
        worst_text = "LIVE"
        worst_color = GREEN
        worst_severity = -1
        for source in SOURCES:
            text, color, severity = self._source_indicator(source)
            if severity > worst_severity:
                worst_text, worst_color, worst_severity = text, color, severity

        _configure_once(
            self.status_label,
            self._status_cache,
            text=worst_text,
            fg=worst_color,
        )
        _configure_once(self.compact_status, self._compact_status_cache, fg=worst_color)

    def close(self) -> None:
        if self._closed:
            return
        self._save_window_state()
        if self._persist_snapshot:
            self._save_snapshots()
        self._closed = True
        self.poller.stop()
        self.claude_poller.stop()
        for after_id in (
            self._after_id,
            self._topmost_after_id,
            self._display_after_id,
            self._save_after_id,
            self._flash_after_id,
            self._notify_after_id,
        ):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        self.notifier.close()
        LOG.info("HUD closed at %s", self._position)
        self.root.destroy()

    def shutdown(self) -> None:
        self.poller.stop()
        self.claude_poller.stop()
        self.client.shutdown()
        self.claude_client.shutdown()
        for poller in (self.poller, self.claude_poller):
            if poller.is_alive():
                poller.join(timeout=5.0)


def main() -> None:
    instance = _acquire_single_instance()
    if instance is None:
        return
    setup_logging()
    scale = enable_dpi_awareness()
    LOG.info("starting widget (pid=%d, ui scale=%.2f)", os.getpid(), scale)
    widget: Optional[CodexUsageWidget] = None
    try:
        root = tk.Tk()
        widget = CodexUsageWidget(root)
        root.mainloop()
    except BaseException:
        LOG.exception("widget terminated by an unhandled error")
        raise
    finally:
        if widget is not None:
            widget.shutdown()
        instance.release()
        LOG.info("widget stopped")


if __name__ == "__main__":
    main()
