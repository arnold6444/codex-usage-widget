import os
import queue
import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_usage_widget import (
    COMPACT_HEIGHT,
    COMPACT_RESET_TEXT,
    COMPACT_WIDTH,
    CodexAppServerClient,
    CodexConnectionError,
    CodexUsageWidget,
    RateWindow,
    UiMessage,
    UsagePoller,
    UsageSnapshot,
    WindowState,
    WindowStateStore,
    _acquire_single_instance,
    _fit_position_to_monitors,
    _taskbar_layout,
    format_compact_reset_time,
    format_percent,
    format_reset_time,
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
            "R 1/22(금) 20:30",
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


class PollerTests(unittest.TestCase):
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


class WidgetSmokeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows HUD smoke test")
    def test_window_has_requested_hud_attributes(self):
        client = _FlakyClient()
        root = tk.Tk()
        root.withdraw()
        widget = CodexUsageWidget(
            root,
            client=client,
            autostart=False,
            persist_state=False,
        )
        try:
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
                if layout.task_buttons_left is not None:
                    self.assertLessEqual(
                        root.winfo_x() + COMPACT_WIDTH,
                        layout.task_buttons_left,
                    )

            widget.show_details()
            root.update_idletasks()
            self.assertEqual(root.winfo_width(), 280)
            self.assertEqual(root.winfo_height(), 130)
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
            autostart=False,
            persist_state=False,
        )
        snapshot = UsageSnapshot(
            five_hour=None,
            weekly=RateWindow(27, 10_080, 1_800_100_000),
            limit_id="codex",
            weekly_limit_id="codex",
        )
        try:
            widget._apply_message(UiMessage("snapshot", snapshot=snapshot))
            root.update_idletasks()

            self.assertEqual(widget.compact_weekly.label.cget("text"), "Week 73%")
            self.assertEqual(widget.status_label.cget("text"), "LIVE")
            self.assertEqual(widget.weekly_row.percent.cget("text"), "73%")
            self.assertTrue(widget.compact_weekly.reset.cget("text").startswith("R "))
            self.assertEqual(widget.compact_weekly.reset.cget("fg"), COMPACT_RESET_TEXT)
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
                autostart=False,
                state_store=store,
            )
            try:
                second_root.update_idletasks()
                self.assertFalse(second._compact_mode)
                self.assertEqual((second_root.winfo_x(), second_root.winfo_y()), (333, 444))

                second.show_compact()
                second_root.update_idletasks()
                self.assertEqual((second_root.winfo_x(), second_root.winfo_y()), (111, 222))
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


if __name__ == "__main__":
    unittest.main()
