"""Small always-on-top Windows HUD for Codex usage limits.

The widget talks only to the official ``codex app-server`` subprocess.  It does
not open Codex credential files or handle access tokens itself.
"""

from __future__ import annotations

import ctypes
import json
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
from typing import Any, Mapping, Optional, Sequence


FIVE_HOURS_MINS = 300
ONE_WEEK_MINS = 10_080
REFRESH_SECONDS = 30.0
RPC_TIMEOUT_SECONDS = 20.0
RECONNECT_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 30.0)
TOPMOST_REFRESH_MS = 500

WINDOW_WIDTH = 280
WINDOW_HEIGHT = 130
COMPACT_WIDTH = 114
COMPACT_HEIGHT = 32

BG = "#17191f"
BAR_BG = "#30333b"
TEXT = "#f1f3f5"
MUTED = "#9298a3"
COMPACT_RESET_TEXT = "#d9dde5"
GREEN = "#54d38a"
AMBER = "#f0b45a"
RED = "#ef6a73"
BORDER = "#353944"


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


class WindowStateStore:
    """Tolerant, atomic storage for non-sensitive HUD layout state."""

    VERSION = 2

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
        five_hour_label="5h",
        weekly_label="Week",
        five_hour_limit_id=None,
        weekly_limit_id=canonical.limit_id if weekly is not None else None,
    )


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
        return f"R {reset.month}/{reset.day}({weekday}) {reset.strftime('%H:%M')}"
    return (
        f"R {str(reset.year)[2:]}/{reset.month}/{reset.day}({weekday}) "
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
        self._command = tuple(command) if command is not None else resolve_codex_command()
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
        command = [*self._command, "app-server"]
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
    def __init__(
        self,
        client: CodexAppServerClient,
        output: "queue.Queue[UiMessage]",
        *,
        refresh_seconds: float = REFRESH_SECONDS,
        reconnect_delays: Sequence[float] = RECONNECT_DELAYS_SECONDS,
    ) -> None:
        super().__init__(name="codex-usage-poller", daemon=True)
        self.client = client
        self.output = output
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
                    snapshot = select_usage_windows(result)
                    if snapshot.available:
                        self.output.put(UiMessage("snapshot", snapshot=snapshot))
                    else:
                        self.output.put(
                            UiMessage(
                                "unavailable",
                                detail="기본 Codex 주간 한도를 찾지 못했습니다.",
                            )
                        )
                    failure_count = 0
                    if self._wait_or_disconnect(self.refresh_seconds):
                        break
                except CodexRpcError as exc:
                    self.output.put(UiMessage("error", detail=str(exc)))
                    failure_count = 0
                    if self.stop_event.wait(self.refresh_seconds):
                        break
                except (CodexConnectionError, OSError) as exc:
                    self.output.put(UiMessage("reconnecting", detail=str(exc)))
                    delay = self.reconnect_delays[
                        min(failure_count, len(self.reconnect_delays) - 1)
                    ]
                    failure_count += 1
                    if self.stop_event.wait(delay):
                        break
        finally:
            self.client.shutdown()


class UsageRow:
    def __init__(self, parent: tk.Widget, y: int, label: str) -> None:
        self.label = tk.Label(
            parent,
            text=label,
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 10),
            anchor="w",
        )
        self.label.place(x=10, y=y, width=55, height=18)

        self.percent = tk.Label(
            parent,
            text="—",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 10),
            anchor="e",
        )
        self.percent.place(x=190, y=y, width=78, height=18)

        self.bar = tk.Canvas(
            parent,
            bg=BAR_BG,
            highlightthickness=0,
            bd=0,
        )
        self.bar.place(x=10, y=y + 21, width=258, height=6)
        self.fill = self.bar.create_rectangle(0, 0, 0, 6, fill=GREEN, outline="")

        self.reset = tk.Label(
            parent,
            text="Reset —",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
            anchor="e",
        )
        self.reset.place(x=90, y=y + 29, width=178, height=14)

    def set_title(self, title: str) -> None:
        self.label.configure(text=title)

    def set_window(self, window: Optional[RateWindow]) -> None:
        if window is None:
            self.percent.configure(text="—", fg=MUTED)
            self.reset.configure(text="Reset —")
            self.bar.coords(self.fill, 0, 0, 0, 6)
            return

        remaining = window.remaining_percent
        progress = window.progress_percent
        color = GREEN if progress >= 50 else AMBER if progress >= 20 else RED
        self.percent.configure(text=format_percent(remaining), fg=color)
        self.reset.configure(text=format_reset_time(window.resets_at))
        self.bar.coords(self.fill, 0, 0, round(258 * progress / 100.0), 6)
        self.bar.itemconfigure(self.fill, fill=color)


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
        self.label = tk.Label(
            parent,
            text=f"{label} —",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI Semibold", 8),
            anchor="w",
        )
        self.label.place(x=x, y=1, width=width, height=17)

        self.reset = tk.Label(
            parent,
            text="R —",
            bg=BG,
            fg=COMPACT_RESET_TEXT,
            font=("Segoe UI Semibold", 7),
            anchor="w",
        )
        self.reset.place(x=x, y=17, width=reset_width or width, height=12)

    def set_prefix(self, prefix: str) -> None:
        self.prefix = prefix
        self.label.configure(text=f"{prefix} —")

    def set_window(self, window: Optional[RateWindow]) -> None:
        if window is None:
            self.label.configure(text=f"{self.prefix} —", fg=MUTED)
            self.reset.configure(text="R —")
            return

        progress = window.progress_percent
        color = GREEN if progress >= 50 else AMBER if progress >= 20 else RED
        self.label.configure(
            text=f"{self.prefix} {format_percent(window.remaining_percent)}",
            fg=color,
        )
        self.reset.configure(text=format_compact_reset_time(window.resets_at))


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


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


def _initial_position(root: tk.Tk) -> tuple[int, int]:
    margin = 18
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

    x = max(left, right - WINDOW_WIDTH - margin)
    y = max(top, bottom - WINDOW_HEIGHT - margin)
    return x, y


def _compact_initial_position(root: tk.Tk) -> tuple[int, int]:
    layout = _taskbar_layout()
    if layout is not None and layout.horizontal:
        scale = max(0.75, layout.dpi / 96.0)
        x = layout.left + round(162 * scale)
        if layout.task_buttons_left is not None:
            x = min(x, layout.task_buttons_left - COMPACT_WIDTH - round(12 * scale))
        x = max(layout.left + 6, min(x, layout.right - COMPACT_WIDTH - 6))
        y = layout.top + max(0, (layout.bottom - layout.top - COMPACT_HEIGHT) // 2)
        return x, y

    x = max(0, root.winfo_screenwidth() - COMPACT_WIDTH - 18)
    y = max(0, root.winfo_screenheight() - COMPACT_HEIGHT - 18)
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


def _raise_window_above_taskbar(root: tk.Tk) -> bool:
    """Reassert the HUD at the top of the topmost band without activation."""

    if os.name != "nt":
        try:
            root.lift()
            return True
        except tk.TclError:
            return False

    try:
        root.update_idletasks()
        user32 = ctypes.windll.user32
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        get_ancestor.restype = ctypes.c_void_p
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

        find_window = user32.FindWindowW
        find_window.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        find_window.restype = ctypes.c_void_p

        inner_handle = ctypes.c_void_p(root.winfo_id())
        top_handle = find_window(None, root.title())
        if not top_handle:
            top_handle = get_ancestor(inner_handle, 2) or inner_handle.value  # GA_ROOT
        flags = 0x0001 | 0x0002 | 0x0010 | 0x0040
        return bool(
            set_window_pos(
                top_handle,
                ctypes.c_void_p(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                flags,  # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
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
        autostart: bool = True,
        state_store: Optional[WindowStateStore] = None,
        persist_state: bool = True,
    ) -> None:
        self.root = root
        self.client = client or CodexAppServerClient()
        self.messages: "queue.Queue[UiMessage]" = queue.Queue()
        self.poller = UsagePoller(self.client, self.messages)
        self._closed = False
        self._drag_offset = (0, 0)
        self._after_id: Optional[str] = None
        self._topmost_after_id: Optional[str] = None
        self._persist_state = persist_state
        self.state_store = state_store or WindowStateStore()
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
            text="CODEX USAGE",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 9),
            anchor="w",
        )
        self.title_label.place(x=10, y=5, width=105, height=19)

        self.status_label = tk.Label(
            self.detail_panel,
            text="CONNECTING",
            bg=BG,
            fg=AMBER,
            font=("Segoe UI", 7),
            anchor="e",
        )
        self.status_label.place(x=116, y=6, width=94, height=17)

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
        self.detail_toggle_button.place(x=214, y=2, width=28, height=24)

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
        self.close_button.place(x=244, y=2, width=28, height=24)

        self.weekly_row = UsageRow(self.detail_panel, 54, "Week left")

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
        self.compact_status.place(x=3, y=3, width=9, height=13)
        self.compact_weekly = CompactUsage(
            self.compact_panel,
            13,
            60,
            "Week",
            reset_width=99,
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
        self.compact_toggle_button.place(x=74, y=0, width=18, height=17)
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
        self.compact_close_button.place(x=94, y=0, width=18, height=17)

        self._compact_mode = self._window_state.compact_mode
        self._bind_drag_widgets(self.detail_panel)
        self._bind_drag_widgets(self.compact_panel)
        root.bind("<Escape>", lambda _event: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)

        if self._window_state.compact_mode:
            self.show_compact(initial=True)
        else:
            self.show_details(initial=True)

        self._refresh_topmost()
        self._after_id = root.after(100, self._drain_messages)
        if autostart:
            self.poller.start()

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
        was_compact = self._compact_mode
        if not initial:
            self._remember_current_position()

        saved = _restore_visible_position(
            self.root,
            self._window_state.compact_position,
            COMPACT_WIDTH,
            COMPACT_HEIGHT,
        )
        if saved is not None and (initial or not was_compact):
            x, y = saved
        elif initial:
            x, y = _compact_initial_position(self.root)
        else:
            self.root.update_idletasks()
            x = self.root.winfo_x()
            layout = _taskbar_layout()
            if layout is not None and layout.horizontal:
                x = max(layout.left + 6, min(x, layout.right - COMPACT_WIDTH - 6))
                y = layout.top + max(
                    0, (layout.bottom - layout.top - COMPACT_HEIGHT) // 2
                )
            else:
                _, y = _compact_initial_position(self.root)

        self.detail_panel.place_forget()
        self.compact_panel.place(x=0, y=0, width=COMPACT_WIDTH, height=COMPACT_HEIGHT)
        self._set_window_geometry(COMPACT_WIDTH, COMPACT_HEIGHT, x, y)
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
        was_compact = self._compact_mode
        if not initial:
            self._remember_current_position()

        saved = _restore_visible_position(
            self.root,
            self._window_state.detail_position,
            WINDOW_WIDTH,
            WINDOW_HEIGHT,
        )
        if saved is not None and (initial or was_compact):
            x, y = saved
        elif initial:
            x, y = _initial_position(self.root)
        else:
            self.root.update_idletasks()
            x = self.root.winfo_x()
            layout = _taskbar_layout()
            if layout is not None and layout.horizontal:
                x = max(layout.left, min(x, layout.right - WINDOW_WIDTH))
                if layout.top >= self.root.winfo_screenheight() // 2:
                    y = layout.top - WINDOW_HEIGHT - 8
                else:
                    y = layout.bottom + 8
            else:
                x, y = _initial_position(self.root)

        self.compact_panel.place_forget()
        self.detail_panel.place(x=0, y=0, width=WINDOW_WIDTH, height=WINDOW_HEIGHT)
        self._set_window_geometry(WINDOW_WIDTH, WINDOW_HEIGHT, x, y)
        self._compact_mode = False
        self._window_state = replace(
            self._window_state,
            compact_mode=False,
            detail_position=(x, y),
        )
        if not initial:
            self._save_window_state()

    def _set_window_geometry(self, width: int, height: int, x: int, y: int) -> None:
        self.root.resizable(True, True)
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.update_idletasks()
        self.root.resizable(False, False)
        _raise_window_above_taskbar(self.root)

    def _refresh_topmost(self) -> None:
        if self._closed:
            return
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            return
        _raise_window_above_taskbar(self.root)
        self._topmost_after_id = self.root.after(
            TOPMOST_REFRESH_MS,
            self._refresh_topmost,
        )

    def _start_drag(self, event: tk.Event[Any]) -> None:
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _drag(self, event: tk.Event[Any]) -> None:
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"{x:+d}{y:+d}")

    def _finish_drag(self, _event: Optional[tk.Event[Any]] = None) -> None:
        self._save_window_state()

    def _remember_current_position(self) -> None:
        try:
            self.root.update_idletasks()
            position = (self.root.winfo_x(), self.root.winfo_y())
        except tk.TclError:
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
        latest: Optional[UiMessage] = None
        while True:
            try:
                latest = self.messages.get_nowait()
            except queue.Empty:
                break

        if latest is not None:
            self._apply_message(latest)
        self._after_id = self.root.after(150, self._drain_messages)

    def _apply_message(self, message: UiMessage) -> None:
        if message.kind == "snapshot" and message.snapshot is not None:
            self.weekly_row.set_title(f"{message.snapshot.weekly_label} left")
            self.compact_weekly.set_prefix(message.snapshot.weekly_label)
            self.weekly_row.set_window(message.snapshot.weekly)
            self.status_label.configure(text="LIVE", fg=GREEN)
            self.compact_weekly.set_window(message.snapshot.weekly)
            self.compact_status.configure(fg=GREEN)
            return

        self.weekly_row.set_window(None)
        self.compact_weekly.set_window(None)
        if message.kind == "reconnecting":
            self.status_label.configure(text="RECONNECTING", fg=AMBER)
            self.compact_status.configure(fg=AMBER)
        elif message.kind == "unavailable":
            self.status_label.configure(text="NO MATCH", fg=RED)
            self.compact_status.configure(fg=RED)
        else:
            self.status_label.configure(text="ERROR", fg=RED)
            self.compact_status.configure(fg=RED)

    def close(self) -> None:
        if self._closed:
            return
        self._save_window_state()
        self._closed = True
        self.poller.stop()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
        if self._topmost_after_id is not None:
            try:
                self.root.after_cancel(self._topmost_after_id)
            except tk.TclError:
                pass
        self.root.destroy()

    def shutdown(self) -> None:
        self.poller.stop()
        self.client.shutdown()
        if self.poller.is_alive():
            self.poller.join(timeout=5.0)


def main() -> None:
    instance = _acquire_single_instance()
    if instance is None:
        return
    widget: Optional[CodexUsageWidget] = None
    try:
        root = tk.Tk()
        widget = CodexUsageWidget(root)
        root.mainloop()
    finally:
        if widget is not None:
            widget.shutdown()
        instance.release()


if __name__ == "__main__":
    main()
