import logging
import logging.handlers
import os
import queue
import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from claude_usage_client import (
    CLAUDE_MAX_REFRESH_SECONDS,
    CLAUDE_RATE_LIMIT_BACKOFF_SECONDS,
    CLAUDE_REFRESH_SECONDS,
    ClaudeConnectionError,
    ClaudeLoggedOutError,
    ClaudeRateLimitedError,
    ClaudeUnavailableError,
    ClaudeUsageClient,
    parse_iso_timestamp,
    parse_limit_window,
)
from usage_alerts import AlertTracker, describe_alert
from codex_usage_widget import (
    BORDER,
    COMPACT_HEIGHT,
    COMPACT_RESET_TEXT,
    COMPACT_WIDTH,
    GREEN,
    LOG,
    MUTED,
    RED,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    CodexAppServerClient,
    CodexConnectionError,
    CodexUsageWidget,
    RateWindow,
    UiMessage,
    UsagePoller,
    UsageSnapshot,
    UsageSnapshotStore,
    WindowState,
    WindowStateStore,
    _TaskbarLayout,
    _acquire_single_instance,
    _compact_initial_position,
    _fit_position_to_monitors,
    _taskbar_layout,
    format_compact_reset_time,
    format_percent,
    format_reset_time,
    resolve_codex_command,
    setup_logging,
    select_claude_windows,
    select_usage_windows,
)


def window(used, minutes, reset=1_800_000_000):
    return {
        "usedPercent": used,
        "windowDurationMins": minutes,
        "resetsAt": reset,
    }


class SelectionTests(unittest.TestCase):
    def test_ignores_53_meter_and_uses_default_weekly(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": window(27, 10_080),
                "secondary": None,
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": window(27, 10_080),
                    "secondary": None,
                },
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": window(0, 300),
                    "secondary": window(0, 10_080),
                },
            },
        }

        snapshot = select_usage_windows(result)

        self.assertEqual(snapshot.limit_id, "codex")
        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.remaining_percent, 73)
        self.assertIsNone(snapshot.five_hour_limit_id)
        self.assertEqual(snapshot.weekly_limit_id, "codex")

    def test_duration_not_primary_secondary_name_controls_selection(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": window(60, 10_080),
                    "secondary": window(25, 300),
                }
            }
        }

        snapshot = select_usage_windows(result)

        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.used_percent, 60)

    def test_specialized_weekly_never_replaces_default_weekly(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": window(27, 10_080),
            },
            "rateLimitsByLimitId": {
                "special": {
                    "limitId": "special",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": window(10, 300),
                    "secondary": window(0, 10_080),
                },
            },
        }

        snapshot = select_usage_windows(result)

        self.assertEqual(snapshot.limit_id, "codex")
        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.used_percent, 27)

    def test_falls_back_to_coherent_legacy_pair(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": window("12.5", "300"),
                "secondary": window("44", "10080"),
            }
        }

        snapshot = select_usage_windows(result)

        self.assertFalse(snapshot.complete)
        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.remaining_percent, 56)
        self.assertTrue(snapshot.available)

    def test_unnamed_specialized_five_hour_is_not_guessed(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": window(20, 10_080),
            },
            "rateLimitsByLimitId": {
                "five": {"primary": window(10, 300)},
            }
        }

        snapshot = select_usage_windows(result)

        self.assertFalse(snapshot.complete)
        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.remaining_percent, 80)
        self.assertTrue(snapshot.available)

    def test_multiple_named_53_five_hour_meters_are_ambiguous(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": window(20, 10_080),
            },
            "rateLimitsByLimitId": {
                "model_a": {
                    "limitName": "GPT-5.3-Codex-Spark A",
                    "primary": window(10, 300),
                    "secondary": window(20, 10_080),
                },
                "model_b": {
                    "limitName": "GPT-5.3-Codex-Spark B",
                    "primary": window(30, 300),
                    "secondary": window(40, 10_080),
                },
            }
        }

        snapshot = select_usage_windows(result)

        self.assertFalse(snapshot.complete)
        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.remaining_percent, 80)

    def test_conflicting_duplicate_durations_are_not_guessed(self):
        result = {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": window(10, 300),
                    "secondary": window(20, 300),
                }
            }
        }

        snapshot = select_usage_windows(result)

        self.assertFalse(snapshot.complete)

    def test_remaining_formula_and_bar_clamp_are_separate(self):
        value = RateWindow(used_percent=105, window_duration_mins=300, resets_at=None)

        self.assertEqual(value.remaining_percent, -5)
        self.assertEqual(value.progress_percent, 0)
        self.assertEqual(format_percent(value.remaining_percent), "-5%")

    def test_reset_timestamp_is_rendered_in_supplied_local_timezone(self):
        local_tz = timezone(timedelta(hours=9))
        now = datetime(2027, 1, 15, 12, 0, tzinfo=local_tz)
        reset = datetime(2027, 1, 15, 20, 30, tzinfo=local_tz).timestamp()

        self.assertEqual(
            format_reset_time(reset, now=now, local_tz=local_tz),
            "Reset 20:30",
        )
        self.assertEqual(
            format_compact_reset_time(reset, now=now, local_tz=local_tz),
            "R 20:30",
        )

        later = datetime(2027, 1, 22, 20, 30, tzinfo=local_tz).timestamp()
        self.assertEqual(
            format_compact_reset_time(later, now=now, local_tz=local_tz),
            "R 1/22(금)20:30",
        )


FAKE_SERVER = r"""
import json
import sys

initialized = False
for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({"method": "configWarning", "params": {"summary": "test"}}), flush=True)
        print(json.dumps({"id": message["id"], "result": {"platformOs": "windows"}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "account/rateLimits/read":
        if not initialized:
            print(json.dumps({"id": message["id"], "error": {"code": -1, "message": "Not initialized"}}), flush=True)
            continue
        print(json.dumps({"method": "test/request", "id": message["id"], "params": {}}), flush=True)
        print(json.dumps({"method": "remoteControl/status/changed", "params": {"status": "disabled"}}), flush=True)
        print(json.dumps({
            "id": message["id"],
            "result": {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 27, "windowDurationMins": 300, "resetsAt": 1800000000},
                        "secondary": {"usedPercent": 59, "windowDurationMins": 10080, "resetsAt": 1800100000}
                    }
                }
            }
        }), flush=True)
"""


def make_fake_app_server_client():
    return CodexAppServerClient(
        command=(sys.executable, "-u", "-c", FAKE_SERVER),
        request_timeout=3,
    )


class ClientTests(unittest.TestCase):
    def make_client(self):
        return make_fake_app_server_client()

    def test_handshake_and_id_matching_ignore_notifications(self):
        client = self.make_client()
        try:
            result = client.read_rate_limits()
            snapshot = select_usage_windows(result)
            self.assertIsNone(snapshot.five_hour)
            self.assertEqual(snapshot.weekly.remaining_percent, 41)
        finally:
            client.shutdown()

    def test_dead_server_is_restarted_on_next_request(self):
        client = self.make_client()
        try:
            client.read_rate_limits()
            first_connection = client._connection
            self.assertIsNotNone(first_connection)
            first_connection.process.kill()
            first_connection.process.wait(timeout=3)

            result = client.read_rate_limits()

            self.assertIn("rateLimitsByLimitId", result)
            self.assertIsNot(first_connection, client._connection)
        finally:
            client.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows Codex discovery test")
    def test_resolves_codex_desktop_install_when_scheduled_path_is_minimal(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_path = (
                Path(directory)
                / "OpenAI"
                / "Codex"
                / "bin"
                / "current"
                / "codex.exe"
            )
            codex_path.parent.mkdir(parents=True)
            codex_path.touch()

            with mock.patch("codex_usage_widget.shutil.which", return_value=None):
                with mock.patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": directory},
                    clear=False,
                ):
                    self.assertEqual(resolve_codex_command(), (str(codex_path),))

    def test_default_codex_resolution_is_lazy(self):
        client = CodexAppServerClient(request_timeout=0.1)
        try:
            with mock.patch(
                "codex_usage_widget.resolve_codex_command",
                side_effect=CodexConnectionError("missing"),
            ):
                with self.assertRaisesRegex(CodexConnectionError, "missing"):
                    client.read_rate_limits()
        finally:
            client.shutdown()


class _FlakyClient:
    def __init__(self):
        self.calls = 0
        self.is_disconnected = False
        self.shutdown_called = False

    def read_rate_limits(self):
        self.calls += 1
        if self.calls == 1:
            raise CodexConnectionError("dead")
        return {
            "rateLimits": {
                "limitId": "codex",
                "primary": window(27, 300),
                "secondary": window(59, 10_080),
            }
        }

    def shutdown(self):
        self.shutdown_called = True


class _WeeklyOnlyClient:
    is_disconnected = False

    def __init__(self):
        self.shutdown_called = False

    def read_rate_limits(self):
        return {
            "rateLimits": {
                "limitId": "codex",
                "primary": window(27, 10_080),
                "secondary": None,
            }
        }

    def shutdown(self):
        self.shutdown_called = True


STUB_CLAUDE_PAYLOAD = {
    "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"utilization": 8, "resets_at": None},
        "seven_day": {"utilization": 3, "resets_at": None},
    },
}


class _StubClaudeClient:
    """Stand-in so no test ever spawns the real ``claude`` binary."""

    is_disconnected = False

    def __init__(self, payload=STUB_CLAUDE_PAYLOAD, *, fail=False):
        self.payload = payload
        self.fail = fail
        self.shutdown_called = False

    def read_rate_limits(self):
        if self.fail:
            raise ClaudeConnectionError("stub")
        return self.payload

    def shutdown(self):
        self.shutdown_called = True


class ClaudeSelectionTests(unittest.TestCase):
    def test_both_account_windows_are_selected_and_model_scoped_ignored(self):
        snapshot = select_claude_windows(
            {
                "rate_limits_available": True,
                "rate_limits": {
                    "five_hour": {
                        "utilization": 8,
                        "resets_at": "2026-09-16T06:30:00.154466+00:00",
                    },
                    "seven_day": {
                        "utilization": 3,
                        "resets_at": "2026-09-20T13:00:00+00:00",
                    },
                    "seven_day_opus": None,
                    "model_scoped": [{"display_name": "Fable", "utilization": 99}],
                },
            }
        )

        self.assertTrue(snapshot.complete)
        self.assertEqual(snapshot.five_hour.used_percent, 8)
        self.assertEqual(snapshot.five_hour.window_duration_mins, 300)
        self.assertEqual(snapshot.weekly.used_percent, 3)
        self.assertEqual(snapshot.weekly.window_duration_mins, 10_080)
        self.assertEqual(
            snapshot.five_hour.resets_at,
            datetime(2026, 9, 16, 6, 30, 0, 154466, tzinfo=timezone.utc).timestamp(),
        )

    def test_missing_or_reshaped_fields_never_raise(self):
        self.assertFalse(select_claude_windows({}).available)
        self.assertFalse(select_claude_windows({"rate_limits": None}).available)
        self.assertFalse(
            select_claude_windows({"rate_limits": {"five_hour": {}}}).available
        )

        partial = select_claude_windows(
            {"rate_limits": {"five_hour": {"utilization": 12, "resets_at": None}}}
        )
        self.assertTrue(partial.available)
        self.assertFalse(partial.complete)
        self.assertIsNone(partial.five_hour.resets_at)

    def test_iso_timestamps_and_utilization_bounds(self):
        self.assertIsNone(parse_iso_timestamp("nonsense"))
        self.assertIsNone(parse_iso_timestamp(None))
        self.assertEqual(
            parse_iso_timestamp("2026-09-20T13:00:00Z"),
            datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(parse_limit_window({"utilization": 140})[0], 100)
        self.assertEqual(parse_limit_window({"utilization": -3})[0], 0)
        self.assertIsNone(parse_limit_window({"utilization": "nope"}))


class PollerTests(unittest.TestCase):
    def test_claude_poller_labels_its_messages_with_its_source(self):
        client = _StubClaudeClient(
            {
                "rate_limits": {
                    "five_hour": {"utilization": 8, "resets_at": None},
                    "seven_day": {"utilization": 3, "resets_at": None},
                }
            }
        )
        messages = queue.Queue()
        poller = UsagePoller(
            client,
            messages,
            source="claude",
            parse=select_claude_windows,
            connection_errors=(ClaudeConnectionError, OSError),
            refresh_seconds=60,
        )
        poller.start()
        try:
            message = messages.get(timeout=2)
            self.assertEqual(message.source, "claude")
            self.assertEqual(message.kind, "snapshot")
            self.assertEqual(message.snapshot.five_hour.remaining_percent, 92)
            self.assertEqual(message.snapshot.weekly.remaining_percent, 97)
        finally:
            poller.stop()
            poller.join(timeout=2)
        self.assertTrue(client.shutdown_called)

    def test_weekly_only_default_meter_is_still_a_live_snapshot(self):
        client = _WeeklyOnlyClient()
        messages = queue.Queue()
        poller = UsagePoller(client, messages, refresh_seconds=60)
        poller.start()
        try:
            message = messages.get(timeout=2)
            self.assertEqual(message.kind, "snapshot")
            self.assertIsNone(message.snapshot.five_hour)
            self.assertEqual(message.snapshot.weekly.remaining_percent, 73)
        finally:
            poller.stop()
            poller.join(timeout=2)
        self.assertTrue(client.shutdown_called)

    def test_poller_retries_after_connection_failure(self):
        client = _FlakyClient()
        messages = queue.Queue()
        poller = UsagePoller(
            client,
            messages,
            refresh_seconds=60,
            reconnect_delays=(0.01,),
        )
        poller.start()
        try:
            first = messages.get(timeout=2)
            second = messages.get(timeout=2)
            self.assertEqual(first.kind, "reconnecting")
            self.assertEqual(second.kind, "snapshot")
            self.assertIsNone(second.snapshot.five_hour)
            self.assertEqual(second.snapshot.weekly.remaining_percent, 41)
        finally:
            poller.stop()
            poller.join(timeout=2)
        self.assertFalse(poller.is_alive())
        self.assertTrue(client.shutdown_called)

    def test_poller_reconnects_if_server_dies_while_waiting(self):
        client = make_fake_app_server_client()
        messages = queue.Queue()
        poller = UsagePoller(
            client,
            messages,
            refresh_seconds=60,
            reconnect_delays=(0.01,),
        )
        poller.start()
        try:
            first = messages.get(timeout=3)
            self.assertEqual(first.kind, "snapshot")
            first_connection = client._connection
            self.assertIsNotNone(first_connection)

            first_connection.process.kill()
            first_connection.process.wait(timeout=3)

            deadline = time.monotonic() + 4
            recovered = None
            while time.monotonic() < deadline:
                message = messages.get(timeout=max(0.01, deadline - time.monotonic()))
                if message.kind == "snapshot":
                    recovered = message
                    break

            self.assertIsNotNone(recovered)
            self.assertTrue(recovered.snapshot.available)
            self.assertIsNotNone(recovered.snapshot.weekly)
            self.assertIsNot(first_connection, client._connection)
        finally:
            poller.stop()
            client.shutdown()
            poller.join(timeout=3)
        self.assertFalse(poller.is_alive())


class WindowStateStoreTests(unittest.TestCase):
    def test_l_shaped_monitor_gap_is_not_treated_as_visible(self):
        monitors = [
            (0, 0, 1920, 1080),
            (1920, 0, 3840, 900),
            (0, 1080, 1280, 2160),
        ]

        self.assertIsNone(
            _fit_position_to_monitors((2000, 1300), 226, 32, monitors)
        )
        self.assertEqual(
            _fit_position_to_monitors((3700, 100), 226, 32, monitors),
            (3614, 100),
        )

    def test_round_trip_uses_versioned_mode_specific_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "settings.json"
            store = WindowStateStore(path)
            expected = WindowState(
                compact_mode=False,
                compact_position=(111, 222),
                detail_position=(333, 444),
            )

            self.assertTrue(store.save(expected))
            self.assertEqual(store.load(), expected)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_corrupt_or_wrong_version_settings_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            store = WindowStateStore(path)

            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(store.load())

            path.write_text(
                '{"version":99,"mode":"compact","positions":{}}',
                encoding="utf-8",
            )
            self.assertIsNone(store.load())

    @unittest.skipUnless(sys.platform == "win32", "Windows mutex test")
    def test_single_instance_mutex_releases_cleanly(self):
        name = f"Local\\CodexUsageWidgetTest-{os.getpid()}-{time.time_ns()}"
        first = _acquire_single_instance(name)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(_acquire_single_instance(name))
        finally:
            first.release()

        replacement = _acquire_single_instance(name)
        self.assertIsNotNone(replacement)
        replacement.release()


class UsageSnapshotStoreTests(unittest.TestCase):
    def test_round_trip_keeps_both_sources_and_no_account_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "last_snapshot.json"
            store = UsageSnapshotStore(path)
            codex = UsageSnapshot(
                five_hour=None,
                weekly=RateWindow(41, 10_080, 1_800_100_000),
                limit_id="codex",
                five_hour_label="CX 5h",
                weekly_label="CX 주",
                weekly_limit_id="codex",
            )
            claude = UsageSnapshot(
                five_hour=RateWindow(8, 300, 1_800_050_000),
                weekly=RateWindow(3, 10_080, 1_800_100_000),
                limit_id="claude",
                five_hour_label="CL 5h",
                weekly_label="CL 주",
                five_hour_limit_id="claude",
                weekly_limit_id="claude",
            )

            self.assertTrue(store.save({"codex": codex, "claude": claude}))
            self.assertEqual(store.load(), {"codex": codex, "claude": claude})
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("account", raw.lower())
            self.assertNotIn("token", raw.lower())

    def test_invalid_or_stale_version_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "last_snapshot.json"
            store = UsageSnapshotStore(path)

            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(store.load(), {})

            path.write_text(
                '{"version":1,"weekly":{"usedPercent":2,"windowDurationMins":10080}}',
                encoding="utf-8",
            )
            self.assertEqual(store.load(), {})

            path.write_text(
                '{"version":3,"sources":{"codex":{"limitId":"codex",'
                '"weekly":{"usedPercent":2,"windowDurationMins":300}}}}',
                encoding="utf-8",
            )
            self.assertEqual(store.load(), {})


class CompactPlacementTests(unittest.TestCase):
    """Deterministic taskbar maths, independent of the live taskbar."""

    class _FakeRoot:
        @staticmethod
        def winfo_screenwidth():
            return 1920

        @staticmethod
        def winfo_screenheight():
            return 1080

    def test_hud_keeps_a_gap_from_the_task_buttons(self):
        layout = _TaskbarLayout(
            left=0, top=1032, right=1920, bottom=1080,
            task_buttons_left=451, dpi=96,
        )
        with mock.patch("codex_usage_widget._taskbar_layout", return_value=layout):
            x, y = _compact_initial_position(self._FakeRoot())

        self.assertLessEqual(x + COMPACT_WIDTH + 12, layout.task_buttons_left)
        self.assertEqual(y, 1032 + (48 - COMPACT_HEIGHT) // 2)

    def test_autohidden_taskbar_falls_back_to_the_work_area(self):
        with mock.patch("codex_usage_widget._taskbar_layout", return_value=None):
            x, y = _compact_initial_position(self._FakeRoot())

        self.assertGreaterEqual(x, 0)
        self.assertLess(x + COMPACT_WIDTH, 1920)
        self.assertLess(y + COMPACT_HEIGHT, 1080)


class AlertTrackerTests(unittest.TestCase):
    def test_each_threshold_fires_once_per_quota_window(self):
        tracker = AlertTracker()
        reset = 1_800_000_000.0

        self.assertIsNone(tracker.evaluate("codex:weekly", "CX 주", 70, reset))

        warn = tracker.evaluate("codex:weekly", "CX 주", 81, reset)
        self.assertIsNotNone(warn)
        self.assertEqual(warn.level_name, "WARN")
        self.assertIsNone(tracker.evaluate("codex:weekly", "CX 주", 85, reset))

        critical = tracker.evaluate("codex:weekly", "CX 주", 91, reset)
        self.assertIsNotNone(critical)
        self.assertEqual(critical.level_name, "CRITICAL")
        self.assertIsNone(tracker.evaluate("codex:weekly", "CX 주", 99, reset))

    def test_a_wobbling_reset_time_is_the_same_window(self):
        """Codex moves ``resetsAt`` by a second between polls.

        Treating that as a new quota window re-announced the same limit every
        30 seconds, which is exactly what it did in the field.
        """

        tracker = AlertTracker()
        self.assertIsNotNone(
            tracker.evaluate("codex:weekly", "CX 주", 96, 1_789_810_463.0)
        )
        for drift in (1_789_810_462.0, 1_789_810_464.0, 1_789_810_461.0):
            self.assertIsNone(tracker.evaluate("codex:weekly", "CX 주", 96, drift))

    def test_a_new_reset_time_re_arms_the_thresholds(self):
        tracker = AlertTracker()
        self.assertIsNotNone(tracker.evaluate("cl:5h", "CL 5h", 95, 100.0))
        self.assertIsNone(tracker.evaluate("cl:5h", "CL 5h", 95, 100.0))

        # Window rolled over: the same usage is news again.
        event = tracker.evaluate("cl:5h", "CL 5h", 95, 100.0 + 5 * 3600)
        self.assertIsNotNone(event)
        self.assertEqual(event.level_name, "CRITICAL")

    def test_dropping_below_a_threshold_re_arms_it_within_the_same_window(self):
        tracker = AlertTracker()
        self.assertIsNotNone(tracker.evaluate("cl:5h", "CL 5h", 82, 100.0))
        self.assertIsNone(tracker.evaluate("cl:5h", "CL 5h", 40, 100.0))
        self.assertIsNotNone(tracker.evaluate("cl:5h", "CL 5h", 82, 100.0))

    def test_sources_and_windows_are_tracked_independently(self):
        tracker = AlertTracker()
        self.assertIsNotNone(tracker.evaluate("codex:weekly", "CX 주", 95, 1.0))
        self.assertIsNotNone(tracker.evaluate("claude:weekly", "CL 주", 95, 1.0))

    def test_missing_or_nonsense_usage_never_alerts(self):
        tracker = AlertTracker()
        self.assertIsNone(tracker.evaluate("k", "l", None, 1.0))
        self.assertIsNone(tracker.evaluate("k", "l", float("nan"), 1.0))

    def test_alert_text_reports_used_and_remaining(self):
        tracker = AlertTracker()
        event = tracker.evaluate("codex:weekly", "CX 주", 92.0, None)
        title, body = describe_alert(event, "R 9/19(토)18:34")

        self.assertIn("CX 주", title)
        self.assertIn("92%", body)
        self.assertIn("8%", body)
        self.assertIn("R 9/19(토)18:34", body)


class LoggingTests(unittest.TestCase):
    def test_rotating_log_is_created_and_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "widget.log"
            try:
                self.assertEqual(setup_logging(path), path)
                LOG.info("hello %s", "world")
                for handler in LOG.handlers:
                    handler.flush()
                self.assertIn("hello world", path.read_text(encoding="utf-8"))
            finally:
                for handler in list(LOG.handlers):
                    if isinstance(handler, logging.handlers.RotatingFileHandler):
                        LOG.removeHandler(handler)
                        handler.close()

    def test_an_unwritable_log_path_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "blocker"
            blocker.write_text("not a directory", encoding="utf-8")
            self.assertIsNone(setup_logging(blocker / "nested" / "widget.log"))


class _RecordingNotifier:
    def __init__(self, succeed=True):
        self.calls = []
        self.closed = 0
        self.succeed = succeed

    def notify(self, handle, title, message, *, critical):
        self.calls.append((title, message, critical))
        return self.succeed

    def close(self):
        self.closed += 1


class AlertIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows HUD alert test")
    def test_crossing_a_threshold_notifies_and_flashes_once(self):
        notifier = _RecordingNotifier()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
            notifier=notifier,
            alerts_enabled=True,
        )
        hot = select_usage_windows(
            {"rateLimits": {"limitId": "codex", "primary": window(93, 10_080)}}
        )
        try:
            widget._apply_message(UiMessage("snapshot", snapshot=hot))
            root.update_idletasks()

            self.assertEqual(len(notifier.calls), 1)
            title, body, critical = notifier.calls[0]
            self.assertTrue(critical)
            self.assertIn("CX 주", title)
            self.assertIn("93%", body)
            self.assertIsNotNone(widget._flash_after_id)

            # The same reading must not keep re-announcing itself.
            widget._apply_message(UiMessage("snapshot", snapshot=hot))
            self.assertEqual(len(notifier.calls), 1)
        finally:
            widget.close()
            widget.shutdown()

        self.assertGreaterEqual(notifier.closed, 1)

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD alert test")
    def test_flashing_restores_the_normal_border(self):
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
            notifier=_RecordingNotifier(),
            alerts_enabled=True,
        )
        try:
            widget._flash_border(RED, remaining=1)
            self.assertEqual(
                widget.compact_panel.cget("highlightbackground"), RED
            )
            widget._flash_border(RED, remaining=0)
            self.assertEqual(
                widget.compact_panel.cget("highlightbackground"), BORDER
            )
            self.assertIsNone(widget._flash_after_id)
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD alert test")
    def test_a_restart_does_not_re_announce_the_cached_level(self):
        notifier = _RecordingNotifier()
        with tempfile.TemporaryDirectory() as directory:
            store = UsageSnapshotStore(Path(directory) / "last_snapshot.json")
            hot = select_usage_windows(
                {"rateLimits": {"limitId": "codex", "primary": window(93, 10_080)}}
            )
            self.assertTrue(store.save({"codex": hot}))

            root = tk.Tk()
            root.withdraw()
            widget = CodexUsageWidget(
                root,
                client=_FlakyClient(),
                claude_client=_StubClaudeClient(),
                autostart=False,
                persist_state=False,
                snapshot_store=store,
                persist_snapshot=True,
                notifier=notifier,
                alerts_enabled=True,
            )
            try:
                widget._apply_message(UiMessage("snapshot", snapshot=hot))
                self.assertEqual(notifier.calls, [])

                # Getting worse is still worth a shout only if it crosses a
                # threshold that has not been announced yet.
                warmer = select_usage_windows(
                    {
                        "rateLimits": {
                            "limitId": "codex",
                            "primary": window(96, 10_080),
                        }
                    }
                )
                widget._apply_message(UiMessage("snapshot", snapshot=warmer))
                self.assertEqual(notifier.calls, [])
            finally:
                widget.close()
                widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD alert test")
    def test_alerts_are_off_unless_asked_for(self):
        notifier = _RecordingNotifier()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
            notifier=notifier,
        )
        try:
            self.assertFalse(widget.alerts_enabled)
            widget._apply_message(
                UiMessage(
                    "snapshot",
                    snapshot=select_usage_windows(
                        {"rateLimits": {"limitId": "codex", "primary": window(97, 10_080)}}
                    ),
                )
            )
            self.assertEqual(notifier.calls, [])
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD alert test")
    def test_alerts_can_be_switched_off(self):
        notifier = _RecordingNotifier()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
            notifier=notifier,
            alerts_enabled=False,
        )
        try:
            widget._apply_message(
                UiMessage(
                    "snapshot",
                    snapshot=select_usage_windows(
                        {"rateLimits": {"limitId": "codex", "primary": window(99, 10_080)}}
                    ),
                )
            )
            self.assertEqual(notifier.calls, [])
        finally:
            widget.close()
            widget.shutdown()


class TopmostHandleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows HWND resolution")
    def test_handle_is_the_visible_toplevel_not_the_inner_tk_window(self):
        """The z-order fix is worthless if we raise the wrong window.

        ``winfo_id`` is the inner Tk window and ``GetAncestor(GA_ROOT)`` returns
        it unchanged, so only the ``TkTopLevel`` wrapper may be used.
        """

        import ctypes
        from ctypes import wintypes

        root = tk.Tk()
        widget = CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
        )
        try:
            root.update()
            handle = widget._handle()

            user32 = ctypes.windll.user32
            enum_proc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
            )
            visible_toplevels = []

            def collect(hwnd, _lparam):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(
                    ctypes.c_void_p(hwnd), ctypes.byref(pid)
                )
                if pid.value == os.getpid() and user32.IsWindowVisible(
                    ctypes.c_void_p(hwnd)
                ):
                    name = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(ctypes.c_void_p(hwnd), name, 64)
                    if name.value == "TkTopLevel":
                        visible_toplevels.append(hwnd)
                return True

            user32.EnumWindows(enum_proc(collect), None)

            self.assertIn(handle, visible_toplevels)
            self.assertNotEqual(handle, root.winfo_id())
        finally:
            widget.close()
            widget.shutdown()


class WidgetSmokeTests(unittest.TestCase):
    def test_offscreen_window_recovers_without_display_change(self):
        root = tk.Tk()
        widget = CodexUsageWidget(
            root, client=_FlakyClient(), claude_client=_StubClaudeClient(),
            autostart=False, persist_state=False,
        )
        try:
            root.update()
            widget._set_window_geometry(308, 32, 185, 9000000)
            root.update()
            widget._display_signature = widget._current_display_signature()
            widget._watch_display()
            root.update()
            self.assertGreaterEqual(root.winfo_y(), 0)
            self.assertLess(root.winfo_y() + root.winfo_height(), root.winfo_screenheight() + 1)
            widget._position = (185, 9000000)
            widget._finish_drag()
            root.update()
            self.assertLess(root.winfo_y() + root.winfo_height(), root.winfo_screenheight() + 1)
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD smoke test")
    def test_window_has_requested_hud_attributes(self):
        client = _FlakyClient()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=client,
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
        )
        try:
            root.deiconify()
            root.update_idletasks()
            self.assertEqual(root.winfo_width(), COMPACT_WIDTH)
            self.assertEqual(root.winfo_height(), COMPACT_HEIGHT)
            self.assertTrue(widget._compact_mode)

            layout = _taskbar_layout()
            if layout is not None and layout.horizontal:
                self.assertEqual(
                    root.winfo_y(),
                    layout.top + (layout.bottom - layout.top - COMPACT_HEIGHT) // 2,
                )
                # The centred Win11 task buttons slide as apps open, so only the
                # taskbar band itself is a stable assertion here; the gap maths
                # is covered deterministically in CompactPlacementTests.
                self.assertGreaterEqual(root.winfo_x(), layout.left)
                self.assertLessEqual(root.winfo_x() + COMPACT_WIDTH, layout.right)

            widget.show_details()
            root.update_idletasks()
            self.assertEqual(root.winfo_width(), WINDOW_WIDTH)
            self.assertEqual(root.winfo_height(), WINDOW_HEIGHT)
            self.assertFalse(widget._compact_mode)

            widget.show_compact()
            root.update_idletasks()
            self.assertEqual(root.winfo_width(), COMPACT_WIDTH)
            self.assertEqual(root.winfo_height(), COMPACT_HEIGHT)
            self.assertEqual(bool(root.overrideredirect()), True)
            self.assertEqual(bool(root.attributes("-topmost")), True)
            self.assertLess(float(root.attributes("-alpha")), 1.0)
            self.assertIsNotNone(widget._topmost_after_id)
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows compact HUD test")
    def test_live_snapshot_populates_compact_and_detail_views(self):
        client = _FlakyClient()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=client,
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
            alerts_enabled=False,
        )
        snapshot = select_usage_windows(
            {"rateLimits": {"limitId": "codex", "primary": window(27, 10_080)}}
        )
        claude = select_claude_windows(
            {
                "rate_limits": {
                    "five_hour": {"utilization": 8, "resets_at": None},
                    "seven_day": {"utilization": 3, "resets_at": None},
                }
            }
        )
        try:
            widget._apply_message(UiMessage("snapshot", snapshot=snapshot))
            root.update_idletasks()

            self.assertEqual(widget.compact_weekly.label.cget("text"), "CX 주 73%")
            self.assertEqual(widget.weekly_row.percent.cget("text"), "73%")
            self.assertTrue(widget.compact_weekly.reset.cget("text").startswith("R "))
            self.assertEqual(widget.compact_weekly.reset.cget("fg"), COMPACT_RESET_TEXT)
            # Claude has not answered yet, so the shared status stays amber.
            self.assertEqual(widget.status_label.cget("text"), "CONNECTING")

            widget._apply_message(
                UiMessage("snapshot", snapshot=claude, source="claude")
            )
            root.update_idletasks()

            self.assertEqual(widget.status_label.cget("text"), "LIVE")
            self.assertEqual(
                widget.compact_claude_five_hour.label.cget("text"), "CL 5h 92%"
            )
            self.assertEqual(widget.compact_claude_weekly.label.cget("text"), "CL 주 97%")
            self.assertEqual(widget.claude_five_hour_row.percent.cget("text"), "92%")
            self.assertEqual(widget.claude_weekly_row.percent.cget("text"), "97%")
            # A Codex update must not blank the Claude columns.
            self.assertEqual(widget.compact_weekly.label.cget("text"), "CX 주 73%")
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows compact HUD test")
    def test_repeated_identical_snapshot_does_not_rewrite_labels(self):
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
            alerts_enabled=False,
        )
        snapshot = select_usage_windows(
            {"rateLimits": {"limitId": "codex", "primary": window(27, 10_080)}}
        )
        try:
            widget._apply_message(UiMessage("snapshot", snapshot=snapshot))
            root.update_idletasks()

            writes = []
            original = widget.compact_weekly.label.configure

            def counting_configure(*args, **kwargs):
                writes.append(kwargs or args)
                return original(*args, **kwargs)

            widget.compact_weekly.label.configure = counting_configure
            for _ in range(5):
                widget._apply_message(UiMessage("snapshot", snapshot=snapshot))
            root.update_idletasks()

            self.assertEqual(writes, [])
            self.assertEqual(widget.compact_weekly.label.cget("text"), "CX 주 73%")
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows cached HUD test")
    def test_cached_snapshot_stays_visible_while_reconnecting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UsageSnapshotStore(Path(directory) / "last_snapshot.json")
            cached = UsageSnapshot(
                five_hour=None,
                weekly=RateWindow(41, 10_080, 1_800_100_000),
                limit_id="codex",
                five_hour_label="CX 5h",
                weekly_label="CX 주",
                weekly_limit_id="codex",
            )
            self.assertTrue(store.save({"codex": cached}))

            root = tk.Tk()
            root.withdraw()
            widget = CodexUsageWidget(
                root,
                client=_FlakyClient(),
                claude_client=_StubClaudeClient(),
                autostart=False,
                persist_state=False,
                snapshot_store=store,
                persist_snapshot=True,
            )
            try:
                root.update_idletasks()
                self.assertEqual(widget.compact_weekly.label.cget("text"), "CX 주 59%")
                self.assertEqual(widget.status_label.cget("text"), "CACHED")

                widget._apply_message(UiMessage("reconnecting", detail="offline"))
                root.update_idletasks()

                self.assertEqual(widget.compact_weekly.label.cget("text"), "CX 주 59%")
                self.assertEqual(widget.status_label.cget("text"), "OFFLINE")
                self.assertEqual(widget.compact_status.cget("fg"), "#f0b45a")
            finally:
                widget.close()
                widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD integration test")
    def test_close_stops_active_worker_and_app_server(self):
        client = make_fake_app_server_client()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=client,
            claude_client=_StubClaudeClient(),
            autostart=True,
            persist_state=False,
        )
        connection = None
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                root.update()
                connection = client._connection
                if connection is not None and widget.status_label.cget("text") == "LIVE":
                    break
                time.sleep(0.01)

            self.assertIsNotNone(connection)
            self.assertEqual(widget.status_label.cget("text"), "LIVE")
            self.assertIsNone(connection.process.poll())
        finally:
            widget.close()
            widget.shutdown()

        self.assertFalse(widget.poller.is_alive())
        self.assertIsNotNone(connection.process.poll())

    @unittest.skipUnless(sys.platform == "win32", "Windows persistence test")
    def test_close_and_reopen_restore_mode_and_both_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WindowStateStore(Path(directory) / "settings.json")

            first_root = tk.Tk()
            first_root.withdraw()
            first = CodexUsageWidget(
                first_root,
                client=_FlakyClient(),
                claude_client=_StubClaudeClient(),
                autostart=False,
                state_store=store,
            )
            try:
                first._set_window_geometry(COMPACT_WIDTH, COMPACT_HEIGHT, 111, 222)
                first._finish_drag()
                first.show_details()
                first._set_window_geometry(280, 130, 333, 444)
                first._finish_drag()
            finally:
                first.close()
                first.shutdown()

            second_root = tk.Tk()
            second_root.withdraw()
            second = CodexUsageWidget(
                second_root,
                client=_FlakyClient(),
                claude_client=_StubClaudeClient(),
                autostart=False,
                state_store=store,
            )
            try:
                second_root.update_idletasks()
                self.assertFalse(second._compact_mode)
                self.assertEqual(second._position, (333, 444))

                second.show_compact()
                second_root.update_idletasks()
                self.assertEqual(second._position, (111, 222))
            finally:
                second.close()
                second.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows persistence test")
    def test_offscreen_saved_position_falls_back_to_visible_default(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WindowStateStore(Path(directory) / "settings.json")
            store.save(
                WindowState(
                    compact_mode=True,
                    compact_position=(9_000_000, 9_000_000),
                )
            )
            root = tk.Tk()
            root.withdraw()
            widget = CodexUsageWidget(
                root,
                client=_FlakyClient(),
                claude_client=_StubClaudeClient(),
                autostart=False,
                state_store=store,
            )
            try:
                root.update_idletasks()
                self.assertLess(root.winfo_x(), root.winfo_screenwidth())
                self.assertLess(root.winfo_y(), root.winfo_screenheight())
            finally:
                widget.close()
                widget.shutdown()


class ClaudeRateLimitTests(unittest.TestCase):
    """The usage endpoint answered 429 with retry-after 3600 in the field."""

    def _client_returning(self, payload, probe=None):
        client = ClaudeUsageClient(command=("unused",), header_probe=probe is not None)
        client._run_get_usage = lambda warm=False: (probe if warm else payload)
        return client

    def test_a_limited_endpoint_falls_back_to_live_header_values(self):
        live = {
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": 19, "resets_at": "2026-10-13T06:10:00.000Z"},
                "seven_day": {"utilization": 20, "resets_at": "2026-10-18T13:00:00.000Z"},
            },
        }
        client = self._client_returning(
            {"rate_limits_available": True, "rate_limits": None}, probe=live
        )
        with self.assertRaises(ClaudeRateLimitedError) as caught:
            client.read_rate_limits()

        snapshot = select_claude_windows(caught.exception.payload)
        self.assertEqual(snapshot.five_hour.used_percent, 19)
        self.assertEqual(snapshot.weekly.used_percent, 20)

    def test_a_failed_probe_leaves_no_payload(self):
        client = self._client_returning(
            {"rate_limits_available": True, "rate_limits": None},
            probe={"rate_limits_available": True, "rate_limits": None},
        )
        with self.assertRaises(ClaudeRateLimitedError) as caught:
            client.read_rate_limits()
        self.assertIsNone(caught.exception.payload)

    def test_the_probe_is_a_minimal_turn(self):
        """It spends quota, so it must stay tiny and leave no session behind."""

        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = list(command)
            raise OSError("stop here")

        client = ClaudeUsageClient(command=("claude.exe",))
        with mock.patch("claude_usage_client.subprocess.Popen", side_effect=fake_popen):
            with self.assertRaises(ClaudeConnectionError):
                client._run_get_usage(warm=True)

        command = captured["command"]
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--system-prompt"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--model") + 1], "haiku")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--setting-sources") + 1], "local")

    def test_empty_rate_limits_from_a_subscriber_means_rate_limited(self):
        client = self._client_returning(
            {"subscription_type": "team", "rate_limits_available": True, "rate_limits": None}
        )
        with self.assertRaises(ClaudeRateLimitedError):
            client.read_rate_limits()

    def test_an_account_without_limits_is_still_reported_as_unavailable(self):
        client = self._client_returning(
            {"subscription_type": "api", "rate_limits_available": False}
        )
        with self.assertRaises(ClaudeUnavailableError) as caught:
            client.read_rate_limits()
        self.assertNotIsInstance(caught.exception, ClaudeLoggedOutError)

    def test_a_cleared_cli_login_is_reported_as_logged_out(self):
        """Seen in the field: the refresh token expired and the CLI wiped it."""

        client = self._client_returning(
            {"subscription_type": None, "rate_limits_available": False, "rate_limits": None}
        )
        with self.assertRaises(ClaudeLoggedOutError) as caught:
            client.read_rate_limits()
        self.assertIn("claude auth login", str(caught.exception))

    def test_the_poller_reports_logout_and_keeps_polling(self):
        class _LoggedOut:
            is_disconnected = False

            def __init__(self):
                self.calls = 0

            def read_rate_limits(self):
                self.calls += 1
                raise ClaudeLoggedOutError("logged out")

            def shutdown(self):
                pass

        client = _LoggedOut()
        messages = queue.Queue()
        poller = UsagePoller(
            client,
            messages,
            source="claude",
            parse=select_claude_windows,
            rpc_errors=(ClaudeUnavailableError,),
            connection_errors=(ClaudeConnectionError, OSError),
            refresh_seconds=0.05,
        )
        poller.start()
        try:
            message = messages.get(timeout=2)
            self.assertEqual(message.kind, "logged_out")
            time.sleep(0.3)
            # It keeps checking, so a fresh login is picked up on its own.
            self.assertGreater(client.calls, 1)
        finally:
            poller.stop()
            poller.join(timeout=2)

    def test_polls_skip_the_users_own_hooks(self):
        """Every poll used to fire the user's SessionStart hooks (~960/day)."""

        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = list(command)
            raise OSError("stop here")

        client = ClaudeUsageClient(command=("claude.exe",))
        with mock.patch("claude_usage_client.subprocess.Popen", side_effect=fake_popen):
            with self.assertRaises(ClaudeConnectionError):
                client.read_rate_limits()

        command = captured["command"]
        self.assertIn("--setting-sources", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "local")

    def test_polling_is_fast_but_bounded(self):
        # Fresh numbers matter, but never back to the 90 s that tripped it.
        self.assertLessEqual(CLAUDE_REFRESH_SECONDS, 180)
        self.assertGreater(CLAUDE_REFRESH_SECONDS, 90)
        self.assertGreater(CLAUDE_MAX_REFRESH_SECONDS, CLAUDE_REFRESH_SECONDS)
        # The first retry while limited comes well before an hour...
        self.assertLessEqual(CLAUDE_RATE_LIMIT_BACKOFF_SECONDS[0], 600)
        # ...and the schedule only ever gets slower.
        self.assertEqual(
            list(CLAUDE_RATE_LIMIT_BACKOFF_SECONDS),
            sorted(CLAUDE_RATE_LIMIT_BACKOFF_SECONDS),
        )

    def test_poller_backs_off_instead_of_retrying_into_the_limit(self):
        class _Limited:
            is_disconnected = False

            def __init__(self):
                self.calls = 0

            def read_rate_limits(self):
                self.calls += 1
                raise ClaudeRateLimitedError("limited")

            def shutdown(self):
                pass

        client = _Limited()
        messages = queue.Queue()
        poller = UsagePoller(
            client,
            messages,
            source="claude",
            parse=select_claude_windows,
            rpc_errors=(ClaudeUnavailableError,),
            connection_errors=(ClaudeConnectionError, OSError),
            cooldown_errors=(ClaudeRateLimitedError,),
            cooldown_delays=(60,),
            refresh_seconds=0.01,
            reconnect_delays=(0.01,),
        )
        poller.start()
        try:
            message = messages.get(timeout=2)
            self.assertEqual(message.kind, "rate_limited")
            self.assertEqual(message.source, "claude")
            time.sleep(0.3)
            # A 10 ms refresh would have retried dozens of times by now.
            self.assertEqual(client.calls, 1)
        finally:
            poller.stop()
            poller.join(timeout=2)

    def _poller(self, client, **kwargs):
        options = dict(
            source="claude",
            parse=select_claude_windows,
            rpc_errors=(ClaudeUnavailableError,),
            connection_errors=(ClaudeConnectionError, OSError),
            cooldown_errors=(ClaudeRateLimitedError,),
            refresh_seconds=120,
            max_refresh_seconds=600,
            relax_after_successes=3,
        )
        options.update(kwargs)
        return UsagePoller(client, queue.Queue(), **options)

    def test_a_rate_limit_slows_the_normal_interval_and_a_clean_run_restores_it(self):
        poller = self._poller(_StubClaudeClient())

        poller._note_rate_limit()
        self.assertEqual(poller.refresh_seconds, 240)
        poller._note_rate_limit()
        poller._note_rate_limit()
        poller._note_rate_limit()
        self.assertEqual(poller.refresh_seconds, 600)  # capped

        for streak in range(1, 7):
            poller._note_success(streak)
        self.assertEqual(poller.refresh_seconds, 150)  # halved twice
        for streak in range(7, 30):
            poller._note_success(streak)
        self.assertEqual(poller.refresh_seconds, 120)  # never below the base

    def test_fallback_values_are_shown_live_while_limited(self):
        live = {
            "rate_limits": {
                "five_hour": {"utilization": 19, "resets_at": None},
                "seven_day": {"utilization": 20, "resets_at": None},
            }
        }

        class _LimitedWithFallback:
            is_disconnected = False

            def read_rate_limits(self):
                raise ClaudeRateLimitedError("limited", payload=live)

            def shutdown(self):
                pass

        messages = queue.Queue()
        poller = UsagePoller(
            _LimitedWithFallback(),
            messages,
            source="claude",
            parse=select_claude_windows,
            rpc_errors=(ClaudeUnavailableError,),
            connection_errors=(ClaudeConnectionError, OSError),
            cooldown_errors=(ClaudeRateLimitedError,),
            cooldown_delays=(60,),
            refresh_seconds=120,
        )
        poller.start()
        try:
            message = messages.get(timeout=2)
            self.assertEqual(message.kind, "snapshot")
            self.assertEqual(message.snapshot.five_hour.remaining_percent, 81)
        finally:
            poller.stop()
            poller.join(timeout=2)

    def test_retries_while_limited_follow_the_backoff_schedule(self):
        waits = []

        class _AlwaysLimited:
            is_disconnected = False

            def read_rate_limits(self):
                raise ClaudeRateLimitedError("limited")

            def shutdown(self):
                pass

        poller = self._poller(_AlwaysLimited(), cooldown_delays=(5, 10, 20))

        def fake_wait(seconds):
            waits.append(seconds)
            return len(waits) >= 4  # stop after four sleeps

        poller.stop_event.wait = fake_wait
        poller.run()

        self.assertEqual(waits, [5, 10, 20, 20])
        # Only the first limit of a streak slows the normal interval.
        self.assertEqual(poller.refresh_seconds, 240)


class StaleDisplayTests(unittest.TestCase):
    def _widget(self):
        root = tk.Tk()
        root.withdraw()
        return CodexUsageWidget(
            root,
            client=_FlakyClient(),
            claude_client=_StubClaudeClient(),
            autostart=False,
            persist_state=False,
        )

    def _claude_snapshot(self):
        return select_claude_windows(
            {
                "rate_limits": {
                    "five_hour": {"utilization": 8, "resets_at": None},
                    "seven_day": {"utilization": 3, "resets_at": None},
                }
            }
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD test")
    def test_a_long_failing_source_is_greyed_out(self):
        widget = self._widget()
        try:
            widget._apply_message(
                UiMessage("snapshot", snapshot=self._claude_snapshot(), source="claude")
            )
            self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), GREEN)

            widget._last_live["claude"] -= 10_000
            widget._apply_message(UiMessage("rate_limited", detail="x", source="claude"))

            self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), MUTED)
            self.assertEqual(widget.claude_weekly_row.percent.cget("fg"), MUTED)
            # The number itself is kept; only its colour says "old".
            self.assertEqual(
                widget.compact_claude_five_hour.label.cget("text"), "CL 5h 92%"
            )
            # Codex is unaffected.
            self.assertEqual(widget._is_stale("codex"), False)
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD test")
    def test_logout_shows_login_and_greys_at_once_then_recovers(self):
        widget = self._widget()
        try:
            widget._apply_message(
                UiMessage("snapshot", snapshot=self._claude_snapshot(), source="claude")
            )
            widget._apply_message(UiMessage("logged_out", detail="x", source="claude"))

            self.assertEqual(widget.status_label.cget("text"), "LOGIN")
            self.assertEqual(widget.compact_status.cget("fg"), RED)
            self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), MUTED)

            widget._apply_message(
                UiMessage("snapshot", snapshot=self._claude_snapshot(), source="claude")
            )
            self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), GREEN)
            self.assertNotEqual(widget.status_label.cget("text"), "LOGIN")
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD test")
    def test_one_failed_poll_does_not_grey_a_fresh_value(self):
        widget = self._widget()
        try:
            widget._apply_message(
                UiMessage(
                    "snapshot",
                    snapshot=select_usage_windows(
                        {"rateLimits": {"limitId": "codex", "primary": window(27, 10_080)}}
                    ),
                )
            )
            widget._apply_message(
                UiMessage("snapshot", snapshot=self._claude_snapshot(), source="claude")
            )
            widget._apply_message(UiMessage("rate_limited", detail="x", source="claude"))

            self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), GREEN)
            self.assertEqual(widget.status_label.cget("text"), "WAIT")
        finally:
            widget.close()
            widget.shutdown()

    @unittest.skipUnless(sys.platform == "win32", "Windows HUD test")
    def test_a_cached_value_that_never_refreshes_is_greyed_at_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UsageSnapshotStore(Path(directory) / "last_snapshot.json")
            self.assertTrue(store.save({"claude": self._claude_snapshot()}))
            root = tk.Tk()
            root.withdraw()
            widget = CodexUsageWidget(
                root,
                client=_FlakyClient(),
                claude_client=_StubClaudeClient(),
                autostart=False,
                persist_state=False,
                snapshot_store=store,
                persist_snapshot=True,
            )
            try:
                self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), GREEN)
                widget._apply_message(UiMessage("rate_limited", detail="x", source="claude"))
                self.assertEqual(widget.compact_claude_five_hour.label.cget("fg"), MUTED)
            finally:
                widget.close()
                widget.shutdown()


if __name__ == "__main__":
    unittest.main()
