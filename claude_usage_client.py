"""Read Claude subscription rate limits through the official ``claude`` CLI.

The widget never touches ``~/.claude/.credentials.json`` or any OAuth token.  It
spawns the official CLI in stream-json mode and sends a single ``get_usage``
control request, mirroring how the Codex side talks to ``codex app-server``.

The CLI documents this control request as experimental, so every field is parsed
defensively: a missing or reshaped key yields ``None`` instead of an exception.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


# The usage endpoint is rate limited per account. Polling every 90 seconds
# with no back-off tripped it with ``429 rate_limit_error, retry-after: 3600``,
# and polling on through the limit kept it from ever lifting.
#
# The user wants fresh numbers, so start fast and let the poller adapt: each
# rate limit doubles the normal interval (up to the maximum), and a long clean
# run steps it back toward the fast base again.
CLAUDE_REFRESH_SECONDS = 120.0
CLAUDE_MAX_REFRESH_SECONDS = 600.0
# While the usage endpoint is limited, re-check this often. Each check also
# runs the header probe below, so the numbers keep moving during the limit.
CLAUDE_RATE_LIMIT_BACKOFF_SECONDS = (300.0,)

# Fallback while the usage endpoint is limited: every inference response carries
# the same 5-hour / weekly utilisation in its headers, and the CLI answers
# ``get_usage`` from those once a turn has run. A one-word Haiku turn with a
# replaced system prompt, no tools and no MCP costs ~600 input tokens (about
# $0.0015 equivalent), so it is used only while the free endpoint is blocked.
CLAUDE_PROBE_MODEL = "haiku"
CLAUDE_PROBE_SYSTEM_PROMPT = "Reply with one word."
CLAUDE_PROBE_MESSAGE = "ok"
CLAUDE_TIMEOUT_SECONDS = 60.0


class ClaudeUsageError(RuntimeError):
    """Base error for the Claude usage client."""


class ClaudeConnectionError(ClaudeUsageError):
    """The CLI could not be started or did not answer."""


class ClaudeUnavailableError(ClaudeUsageError):
    """The CLI answered but this account exposes no subscription limits."""

    #: Status the HUD shows for this failure.
    ui_kind = "error"


class ClaudeLoggedOutError(ClaudeUnavailableError):
    """The standalone ``claude`` CLI has no usable login.

    The CLI clears its stored tokens on disk when a refresh comes back
    ``invalid_grant`` (for example once the refresh token itself has expired),
    after which ``get_usage`` answers with no subscription at all. Only the user
    can sign in again; the widget keeps polling and recovers by itself after.
    """

    ui_kind = "logged_out"


class ClaudeRateLimitedError(ClaudeUsageError):
    """The usage endpoint is throttling this account; stop polling for a while.

    ``get_usage`` does not surface the HTTP status. When the server rate limits
    the usage fetch, the CLI logs an in-band error and answers with
    ``rate_limits_available: true`` but ``rate_limits: null`` -- that pairing is
    the only signal we get.

    ``payload`` carries live numbers from the header probe when it succeeded,
    so callers can keep showing current values while they back off.
    """

    def __init__(self, message: str, *, payload: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(message)
        self.payload = dict(payload) if payload is not None else None


def resolve_claude_command() -> tuple[str, ...]:
    """Resolve the PATH-visible Claude CLI without invoking a shell."""

    candidate = shutil.which("claude")
    if os.name == "nt" and candidate:
        suffix = os.path.splitext(candidate)[1].lower()
        if suffix not in {".com", ".exe", ".bat", ".cmd"}:
            candidate = None

    if candidate is None and os.name == "nt":
        for name in ("claude.exe", "claude.cmd", "claude.bat"):
            candidate = shutil.which(name)
            if candidate:
                break

    if candidate is None:
        for base in (Path.home() / ".local" / "bin", Path.home() / ".claude" / "bin"):
            for name in ("claude.exe", "claude"):
                path = base / name
                try:
                    if path.is_file():
                        candidate = str(path)
                        break
                except OSError:
                    continue
            if candidate:
                break

    if candidate is None:
        raise ClaudeConnectionError(
            "claude CLI를 찾을 수 없습니다. PATH에서 `claude --version`을 먼저 확인하세요."
        )
    return (candidate,)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_iso_timestamp(value: Any) -> Optional[float]:
    """Convert the CLI's ISO 8601 ``resets_at`` into a POSIX timestamp."""

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def parse_limit_window(raw: Any) -> Optional[tuple[float, Optional[float]]]:
    """Return ``(used_percent, resets_at)`` for one ``rate_limits`` entry.

    ``utilization`` is documented and observed as a 0-100 percentage.
    """

    if not isinstance(raw, Mapping):
        return None
    used_percent = _number(raw.get("utilization"))
    if used_percent is None:
        return None
    used_percent = max(0.0, min(100.0, used_percent))
    return used_percent, parse_iso_timestamp(raw.get("resets_at"))


class ClaudeUsageClient:
    """One-shot ``claude`` CLI client for the ``get_usage`` control request.

    A fresh process is spawned per poll instead of holding a long-lived session:
    the CLI is a large single binary and an idle resident process would keep its
    memory for the whole uptime of the HUD, while a poll costs ~2 seconds.
    """

    def __init__(
        self,
        command: Optional[Sequence[str]] = None,
        *,
        request_timeout: float = CLAUDE_TIMEOUT_SECONDS,
        header_probe: bool = True,
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._request_timeout = request_timeout
        self._header_probe = header_probe
        self._shutdown = threading.Event()
        self._lock = threading.Lock()

    @property
    def is_disconnected(self) -> bool:
        """Always ``False``: there is no long-lived connection to lose."""

        return False

    def shutdown(self) -> None:
        self._shutdown.set()

    def read_rate_limits(self) -> dict[str, Any]:
        """Return the ``get_usage`` payload, or raise ``ClaudeUsageError``."""

        if self._shutdown.is_set():
            raise ClaudeConnectionError("위젯이 종료 중입니다.")

        with self._lock:
            payload = self._run_get_usage()

        if payload.get("rate_limits_available") is False:
            if not payload.get("subscription_type"):
                # A logged-in subscriber always reports its plan here.
                raise ClaudeLoggedOutError(
                    "claude CLI 로그인이 만료되었습니다. 터미널에서 `claude auth login`을 실행하세요."
                )
            raise ClaudeUnavailableError(
                "이 계정에서는 Claude 구독 한도를 조회할 수 없습니다."
            )
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, Mapping):
            if payload.get("rate_limits_available") is True:
                raise ClaudeRateLimitedError(
                    "Claude 사용량 조회가 일시 제한되었습니다(rate_limits 비어 있음).",
                    payload=self._probe_headers(),
                )
            raise ClaudeUnavailableError("Claude 한도 응답에 rate_limits가 없습니다.")
        return dict(payload)

    def _probe_headers(self) -> Optional[dict[str, Any]]:
        """Read live limits from a minimal turn's response headers, or ``None``."""

        if not self._header_probe or self._shutdown.is_set():
            return None
        try:
            with self._lock:
                payload = self._run_get_usage(warm=True)
        except ClaudeUsageError:
            return None
        if isinstance(payload.get("rate_limits"), Mapping):
            return payload
        return None

    def _run_get_usage(self, *, warm: bool = False) -> dict[str, Any]:
        base_command = self._command or resolve_claude_command()
        probe_flags = (
            [
                "--no-session-persistence",
                "--model",
                CLAUDE_PROBE_MODEL,
                "--max-turns",
                "1",
                "--tools",
                "",
                "--system-prompt",
                CLAUDE_PROBE_SYSTEM_PROMPT,
                "--strict-mcp-config",
            ]
            if warm
            else []
        )
        command = [
            *base_command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            # Load only folder-local settings so the user's own SessionStart
            # hooks do not fire on every poll. Auth is unaffected.
            "--setting-sources",
            "local",
            *probe_flags,
        ]
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
            raise ClaudeConnectionError(f"claude CLI 시작 실패: {exc}") from exc

        request_id = f"usage-{os.getpid()}-{int(time.time() * 1000)}"
        request = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        }

        result: dict[str, Any] = {}
        failure: Optional[Exception] = None
        done = threading.Event()

        def send(message: Mapping[str, Any]) -> None:
            stdin = process.stdin
            if stdin is None:
                raise ClaudeConnectionError("claude stdin 파이프가 없습니다.")
            try:
                stdin.write(
                    json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ClaudeConnectionError(f"claude 전송 실패: {exc}") from exc

        def pump() -> None:
            nonlocal failure
            stdout = process.stdout
            if stdout is None:
                failure = ClaudeConnectionError("claude stdout 파이프가 없습니다.")
                done.set()
                return
            try:
                for raw_line in stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(message, Mapping):
                        continue
                    if warm and message.get("type") == "result":
                        # The probe turn finished, so its response headers are
                        # in the CLI's limit store now; ask for them.
                        send(request)
                        continue
                    if message.get("type") != "control_response":
                        continue
                    response = message.get("response")
                    if not isinstance(response, Mapping):
                        continue
                    if response.get("request_id") != request_id:
                        continue
                    if response.get("subtype") != "success":
                        failure = ClaudeConnectionError(
                            f"claude get_usage 오류: {response.get('error', '알 수 없음')}"
                        )
                        done.set()
                        return
                    inner = response.get("response")
                    if isinstance(inner, Mapping):
                        result.update(inner)
                    else:
                        failure = ClaudeConnectionError(
                            "claude get_usage 응답 형식이 올바르지 않습니다."
                        )
                    done.set()
                    return
            except ClaudeConnectionError as exc:
                failure = exc
            except (OSError, ValueError) as exc:
                failure = ClaudeConnectionError(f"claude stdout 읽기 실패: {exc}")
            finally:
                done.set()

        reader = threading.Thread(target=pump, name="claude-usage-stdout", daemon=True)
        reader.start()
        try:
            if warm:
                send(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": CLAUDE_PROBE_MESSAGE},
                    }
                )
            else:
                send(request)

            if not done.wait(self._request_timeout):
                raise ClaudeConnectionError(
                    f"claude 응답 시간 초과 ({self._request_timeout:g}초)"
                )
            if failure is not None:
                raise failure
            if not result:
                stderr_tail = self._read_stderr_tail(process)
                detail = "claude가 사용량 응답 없이 종료했습니다."
                if stderr_tail:
                    detail += f": {stderr_tail}"
                raise ClaudeConnectionError(detail)
            return result
        finally:
            self._terminate(process)

    @staticmethod
    def _read_stderr_tail(process: "subprocess.Popen[str]") -> str:
        stderr = process.stderr
        if stderr is None:
            return ""
        try:
            text = stderr.read() or ""
        except (OSError, ValueError):
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[-1][:200] if lines else ""

    @staticmethod
    def _terminate(process: "subprocess.Popen[str]") -> None:
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
