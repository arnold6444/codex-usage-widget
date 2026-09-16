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


CLAUDE_REFRESH_SECONDS = 90.0
CLAUDE_TIMEOUT_SECONDS = 60.0


class ClaudeUsageError(RuntimeError):
    """Base error for the Claude usage client."""


class ClaudeConnectionError(ClaudeUsageError):
    """The CLI could not be started or did not answer."""


class ClaudeUnavailableError(ClaudeUsageError):
    """The CLI answered but this account exposes no subscription limits."""


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
    ) -> None:
        self._command = tuple(command) if command is not None else None
        self._request_timeout = request_timeout
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
            raise ClaudeUnavailableError(
                "이 계정에서는 Claude 구독 한도를 조회할 수 없습니다."
            )
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, Mapping):
            raise ClaudeUnavailableError("Claude 한도 응답에 rate_limits가 없습니다.")
        return dict(payload)

    def _run_get_usage(self) -> dict[str, Any]:
        base_command = self._command or resolve_claude_command()
        command = [
            *base_command,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
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
            except (OSError, ValueError) as exc:
                failure = ClaudeConnectionError(f"claude stdout 읽기 실패: {exc}")
            finally:
                done.set()

        reader = threading.Thread(target=pump, name="claude-usage-stdout", daemon=True)
        reader.start()
        try:
            stdin = process.stdin
            if stdin is None:
                raise ClaudeConnectionError("claude stdin 파이프가 없습니다.")
            try:
                stdin.write(
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise ClaudeConnectionError(f"claude 전송 실패: {exc}") from exc

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
