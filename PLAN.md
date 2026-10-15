# Plan

## Goal and Scope

Build a Windows HUD in Python and tkinter that reads the signed-in Codex account's 5-hour and weekly usage through the official `codex app-server` protocol. The source remains runnable with `python codex_usage_widget.py`, and a standalone Windows executable plus current-user auto-start setup are included and documented in `README.md`.

Out of scope: reading credential files or tokens, changing Codex authentication/configuration, tray integration, an installer/MSI, system-wide or elevated auto-start, and publishing/deployment.

## Current State

- The repository is an empty initial Git repository on `master`; there are no user files or changes to preserve.
- `codex-cli 0.128.0` is installed and `codex app-server` supports the default stdio transport.
- A live probe completed `initialize` -> `initialized` -> `account/rateLimits/read` without reading credentials.
- The live response contains a 300-minute primary window and a 10080-minute secondary window in one `rateLimitsByLimitId` entry; another 10080-minute bucket also exists, so pairing policy matters.
- Python 3.10.11 and tkinter 8.6 are available.

## Requirement Lock

- Always-on-top, draggable, approximately 280x130 dark translucent HUD, initially near the lower-right corner, with a close button.
- Show remaining percentage, progress bar, and local reset time for exact `windowDurationMins` values 300 and 10080.
- Compute displayed remaining usage as `100 - usedPercent`.
- Start and own `codex app-server` as a hidden subprocess; use JSONL stdio and the documented initialize handshake.
- Refresh every 30 seconds and automatically reconnect after app-server failure without freezing tkinter.
- Never open `auth.json`, token files, or access-token values; authentication remains owned by Codex CLI.
- Use only the Python standard library and include Windows run/troubleshooting instructions.

## Requirement Delta (2026-09-04, taskbar placement)

- Change the default presentation to a compact `5h … | Week …` overlay in the empty taskbar area between the left Widgets/weather control and the centered Start/task buttons.
- Preserve the original progress-bar, reset-time, and approximately 280x130 requirements in an expandable detail view immediately above the taskbar.
- Use a topmost borderless overlay rather than unsupported Explorer taskbar reparenting; do not modify Explorer, registry, or taskbar settings.
- Keep drag, close, refresh, reconnect, duration pairing, and credential boundaries unchanged in both views.
- Done when the compact HUD is visibly aligned inside the current 48px taskbar, the detail view toggles without losing live data, and both layouts pass automated and screenshot verification.

## Requirement Delta (2026-09-04, EXE, auto-start, persistence)

- Produce a no-console, standalone `dist\CodexUsageWidget.exe` that does not require invoking Python to run.
- Register a current-user Startup shortcut named `Codex Usage Widget.lnk` that points to the stable executable path; do not require administrator rights or modify system-wide startup settings.
- Persist the last window coordinates and compact/detail mode under `%LOCALAPPDATA%\CodexUsageWidget\settings.json` on drag completion, mode changes, and normal close.
- Restore saved state on the next launch, but reject malformed/off-screen coordinates so display or taskbar changes cannot make the HUD unreachable.
- Prevent duplicate widget processes with a current-user Windows mutex, so an auto-started copy and a later manual launch do not stack overlays.
- Keep the Python one-line launch path, app-server protocol, credential boundary, layout, refresh, and reconnect behavior unchanged.
- Done when the EXE launches and reads live usage, a close/reopen round trip restores the same position and mode, the Startup shortcut launches that EXE, and no owned app-server process remains after shutdown. An actual reboot is not required for local verification.

## Requirement Delta (2026-09-04, compact reset times)

- Remove the two compact progress lines and use that vertical space for a small reset-time line under each remaining percentage.
- Render compact reset times in a brighter high-contrast neutral color and a readable 7-point semibold font.
- Keep the compact window at 226x32 and retain progress bars in the expanded detail view.

## Requirement Delta (2026-09-04, taskbar z-order recovery)

- Reassert the HUD at the top of Windows' topmost z-order band without taking keyboard focus, because Explorer can redraw the taskbar above a still-visible tkinter window.
- Refresh the z-order periodically and after geometry changes so the compact HUD cannot remain hidden behind the taskbar.
- Done when deliberately raising the taskbar above the widget is followed by automatic HUD recovery without restarting the process.

## Requirement Delta (2026-09-04, meter-source correction)

- Treat the backward-compatible `rateLimits`/`limitId=codex` 10080-minute window as the default Codex weekly meter, even when a model-specific entry also contains a weekly window.
- Show a 300-minute window from another limit only when its `limitName` explicitly identifies GPT-5.3, and label it `5.3 5h` so it cannot be mistaken for a general Codex limit.
- Allow a valid weekly-only snapshot; never require a 300/10080 pair and never substitute the GPT-5.3-specific weekly value for the default weekly value.
- Done when the current live response renders the GPT-5.3 300-minute remaining value alongside the default Codex 10080-minute remaining value and their source IDs are verified separately.

## Requirement Delta (2026-09-04, week-only final display)

- Supersede the prior GPT-5.3 display decision: show no 300-minute or model-specific meter anywhere in compact or detail mode.
- Show only the default `codex` 10080-minute weekly meter, its remaining percentage, progress bar in detail mode, and reset time.
- Reduce compact width from 226px to 114px while retaining status, expand, close, drag, persistence, refresh, and z-order recovery.
- Done when the live EXE visibly contains only `Week`, its value matches the current default `codex` response, the compact window measures 114x32, and the private GitHub repository/release are updated.

## Requirement Delta (2026-09-04, weather adjacency and weekday)

- Place the default 114px compact HUD four pixels to the right of the current Windows Widgets/weather button; invalidate the older saved-layout version so the corrected default is applied once.
- Render non-today compact weekly resets as `R M/D(요일)` while preserving the exact local reset time in detail mode.
- Done when the live compact EXE starts at the current weather button's right edge plus four pixels and shows the correct Korean weekday for the API reset date.

## Requirement Delta (2026-09-04, compact reset clock)

- Keep the 114x32 compact size but extend its lower reset row beneath the top-only controls.
- Render the full compact reset as `R M/D(요일) HH:MM`; detail mode continues to show the full local timestamp.

## Requirement Delta (2026-09-04, resilient local installation)

- Keep the last successfully selected weekly value visible during network or app-server outages, with an amber cached/offline status, and replace it only after a fresh successful read.
- Persist only the selected weekly percentage and reset timestamp under `%LOCALAPPDATA%\CodexUsageWidget\last_snapshot.json`; never persist account IDs, credentials, or raw app-server responses.
- Start a hidden current-user supervisor at login so closing or crashing the widget restarts it within a few seconds without elevation.
- Preserve the existing single-instance mutex, reconnect loop, taskbar overlay behavior, and explicit current-user Startup shortcut removal path.
- Done when a cached value survives a simulated disconnect, the installed EXE reaches LIVE, terminating it causes an automatic restart, and the Startup shortcut resolves to the hidden supervisor in the stable Local AppData install directory.

## Key Decisions

- Prefer the backward-compatible `rateLimits` view for the default Codex weekly value, falling back only to an unambiguous `limitId=codex` entry. Ignore every model-specific and 300-minute meter.
- Match JSON-RPC responses by `id` because notifications can appear between responses. Omit the `jsonrpc` header as specified by the Codex wire protocol.
- Run all subprocess I/O in a daemon worker and pass snapshots to tkinter through a thread-safe queue polled with `after`.
- On transport/process errors, close the owned process, show a compact reconnecting status, and retry on a short delay; successful reads return to the 30-second cadence.
- Draw progress bars with tkinter Canvas for reliable dark styling without third-party dependencies.
- Store only non-sensitive UI state as a small versioned JSON file in Local AppData, using atomic replacement and tolerant reads.
- Package with PyInstaller `--onefile --windowed`; resolve and launch the installed Codex CLI at runtime rather than bundling it.
- Use a per-user Startup-folder shortcut instead of the registry, making auto-start visible and removable without elevation.

## Architecture or Flow

`tkinter HUD -> refresh worker -> CodexAppServerClient -> codex app-server (JSONL stdio) -> duration-based parser -> UI queue -> HUD`

## Implementation Steps

1. Add pure usage-window parsing and formatting helpers, including default-vs-model-specific meter selection and malformed/missing-window behavior. Verify with focused `unittest` cases.
2. Add the app-server client: Windows command resolution, hidden subprocess, initialize handshake, id-based response dispatch, timeout/error handling, and owned-process shutdown. Verify with fakes and a live read.
3. Add the 280x130 tkinter HUD, dark/translucent topmost window, drag/close interactions, progress bars, reset labels, background refresh, and reconnect loop. Verify widget attributes and a timed launch smoke test.
4. Add `README.md` with prerequisites, the one-line run command, display semantics, and troubleshooting. Run final compile, tests, live protocol, startup, and requirement audit.
5. Add tested window-state persistence and single-instance handling without changing the protocol/data path.
6. Add repeatable EXE/build and Startup-shortcut scripts, build the artifact, register the shortcut, and verify launch/restore/shutdown end to end.

## Definition of Done

- `python codex_usage_widget.py` opens the requested Windows HUD using no non-standard packages.
- Live values come only from `account/rateLimits/read` after a valid initialize handshake.
- Only the default Codex 10080-minute window drives the label, percentage, detail progress bar, and reset time; every model-specific or 300-minute meter is ignored.
- The UI remains responsive, refreshes every 30 seconds, and recovers after its app-server process exits.
- Closing the HUD shuts down its worker and owned app-server process.
- README Windows instructions are accurate, and targeted tests plus live/local smoke checks pass.
- `dist\CodexUsageWidget.exe` runs without a console window, the current-user Startup shortcut targets it, and reopening restores the last valid location/state without creating duplicate HUDs.

## Progress

- [x] Repository and local runtime inspected.
- [x] Official protocol documentation checked.
- [x] Installed app-server handshake and live rate-limit response verified.
- [x] Parser and tests implemented.
- [x] App-server lifecycle implemented and live-tested.
- [x] HUD implemented and smoke-tested.
- [x] README and final requirement audit completed.
- [x] Compact taskbar placement and detail toggle implemented.
- [x] Compact/detail automated and screenshot verification completed.
- [x] Window position/mode persistence and single-instance behavior implemented and tested.
- [x] Standalone EXE built and smoke-tested.
- [x] Current-user Startup shortcut registered and inspected.
- [x] Close/reopen state restoration verified with the packaged EXE.
- [x] Compact bars replaced with brighter reset-time labels and screenshot-verified.
- [x] Explorer taskbar z-order recovery implemented and live regression-tested.
- [x] Default weekly-only source selection implemented; all model-specific 5-hour groups are ignored.
- [x] Final week-only 114px compact layout, weather adjacency, and Korean reset weekday implemented and live-verified.
- [x] Last-good weekly snapshot persistence and cached/offline rendering implemented and tested.
- [x] Stable Local AppData install, hidden single-instance supervisor, and close/crash restart verified.

## Verification

- 2026-09-05 persistence fix: the HUD process was absent; the previous Startup shortcut only launched at login and could not recover an exited process. Replaced it with the per-user interactive `Codex Usage Widget` scheduled task (login + one-minute recurrence, no time limit, battery allowed).
- Scheduler launch verified from the repository EXE path: parent `svchost.exe` PID 3160, visible HUD at `(162,1040)` with size `114x32`. The attempted LocalAppData copy was not visible to the scheduler, so it is not used by the final task.
- Forced termination of the scheduled HUD and its complete app-server process tree at 14:21:35 recovered automatically at 14:21:50 with new HUD PIDs 27852/24148; window visible at the same coordinates. Codex desktop restart/update itself and Windows reboot were not performed.
- Normal WM_CLOSE at 14:22:37 exited cleanly (task result 0); recurrence at 14:22:51 restarted the HUD as PIDs 24224/30248, visible at `(162,1040,276,1072)`. No manual launch was used for either recovery test.

- Confirmed: `codex --version` -> `codex-cli 0.128.0`.
- Confirmed: `codex app-server --help` exits 0 and lists stdio as the default transport.
- Confirmed: live initialize and `account/rateLimits/read` requests succeeded without direct credential access.
- Passed: `python -W error::ResourceWarning -m unittest discover -v` -> 23 tests, including notification interleaving, meter-source selection, weekly-only snapshots, reconnect, active shutdown, taskbar placement, compact/detail toggling, state persistence, off-screen/L-shaped monitor fallback, single-instance locking, and Windows HUD attributes.
- Passed: `python -m compileall -q .`.
- Passed: final live response probe -> default `codex` 10080-minute remaining `61%`, `five_hour=None`; the packaged EXE uses the same 30-second polling path.
- Passed: live child app-server termination -> a new wrapper PID, fresh handshake, and complete snapshot; no extra app-server remained after shutdown.
- Passed: tkinter live smoke -> `LIVE`, topmost true, weekly percentage/reset populated, and no five-hour widgets present.
- Passed: exact `python codex_usage_widget.py` launch -> widget stayed running with exactly one owned app-server child; the test process tree was removed afterward with no leftover app-server.
- Passed: final live compact screenshot -> 114x32 HUD at `x=162, y=1040`; Windows UI Automation measured the Widgets/weather button ending at `x=158`, producing the requested four-pixel gap.
- Passed: PyInstaller 6.21.0 `--onefile --windowed` build -> `dist\CodexUsageWidget.exe`; direct EXE launch reached live usage through the installed `codex.CMD` app-server chain.
- Passed: current-user `Codex Usage Widget.lnk` created in the Startup folder; COM inspection confirmed the absolute target and working directory, and launching the shortcut opened the live EXE.
- Passed: packaged close/reopen round trip restored compact mode and exact `x=166, y=1040`; old CMD/Node/Codex app-server child processes all exited before the new instance started.
- Passed: launching the EXE again while it was open left the same two PyInstaller process IDs, confirming duplicate suppression.
- Passed: final compact screenshot shows no progress line and displays the bright weekly reset label as `R 9/11(금) 15:17`, including the local reset weekday and clock time.
- Passed: reproduced the real failure with a live process reported visible at `166,1040` but fully covered by `Shell_TrayWnd`; `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)` restored it immediately.
- Passed: final EXE resolves its actual top-level `Codex Usage` HWND every 500ms; after the taskbar was deliberately raised it remained visible and left the foreground window unchanged.
- Not run: a disruptive Windows reboot/sign-out; the actual Startup shortcut execution path was tested directly instead.
- Passed: resilient-install suite -> `26 tests` with cached-value persistence and reconnect display coverage.
- Passed: `build_exe.ps1` selected Python 3.11.9 and PyInstaller 6.21.0 after Python 3.14/Tk 9 proved incompatible with the packaged runtime.
- Passed: final per-user install -> `%LOCALAPPDATA%\Programs\CodexUsageWidget\CodexUsageWidget.exe`, SHA256 `4AEE5401199B892400FF016BB615DB4738E22E6ACDC23F925C91BC92ACC88B6E`.
- Passed: live final install -> exactly one visible responsive `Codex Usage` window, one hidden supervisor, one widget-owned `codex app-server`, and a fresh non-credential weekly cache.
- Passed: force-killing both PyInstaller widget processes -> a new visible window and owned app-server appeared in 5.45 seconds; launching the Startup shortcut again still left exactly one supervisor and one visible widget.
- Passed: killing only the widget-owned app-server -> a replacement app-server appeared and the last-good cache remained present.
- Fixed after installed-runtime feedback: scheduled-task launches did not inherit the Codex Desktop versioned bin directory, causing the packaged `resolve_codex_command` startup exception.
- Passed: Codex Desktop fallback discovery -> `codex-cli 0.153.1` resolved from `%LOCALAPPDATA%\OpenAI\Codex\bin\1e3e57cdf0634c02\codex.exe` even with a minimal scheduled-task PATH.
- Passed: default Codex command resolution is now lazy, so a temporarily missing CLI cannot prevent the cached HUD from opening and retrying.
- Passed: Windows scheduled task `Codex Usage Widget Supervisor` reached Running with one normal `Codex Usage` window, zero PyInstaller error windows, and one owned live app-server.
- Passed: force-killing the scheduled widget runtime -> normal window and live app-server recovered in 6.61 seconds while the scheduled task stayed Running.
- Corrected after destructive supervisor test: Task Scheduler did not apply `RestartOnFailure` to an externally terminated long-running action, so the task now also has a one-minute repeated trigger; `IgnoreNew` plus the supervisor mutex prevents duplicates while providing independent recovery.
- Passed: after adding the daily one-minute repetition pattern, force-killing the scheduled supervisor produced a new supervisor PID in 37.12 seconds while the normal widget window remained visible.
- Replaced the persistent PowerShell supervisor with a PyInstaller `--windowed` `CodexUsageSupervisor.exe`, eliminating recurring CMD/PowerShell windows while preserving login start, one-minute health checks, mutex deduplication, and widget restart.
- Passed: windowless supervisor install -> scheduled task and Startup shortcut both target `CodexUsageSupervisor.exe`; zero legacy PowerShell watchdog processes and zero visible console windows.
- Passed: force-killing the final widget runtime -> a normal window returned in 1.82 seconds with zero error windows and zero visible console windows.

## Requirement Delta (2026-09-16, Claude usage and HUD stability)

- Show Claude subscription usage next to the Codex weekly meter: both the 5-hour window and the weekly window, with reset times visible in compact mode as well.
- Data source: the official `claude` CLI, `get_usage` control request over `--input-format stream-json` with `skip_behaviors: true`. No credential file is read; the CLI owns authentication, exactly as `codex app-server` does on the Codex side.
- One process per poll rather than a resident CLI: the binary is ~230 MB, and a resident idle session would hold that memory for the whole uptime of the HUD. A poll costs about two seconds, so the Claude poller runs at 90 seconds while Codex stays at 30.
- Each source polls on its own thread and tags its `UiMessage` with a source; one failing CLI must never blank or freeze the other column.
- Fix the reported visual defects at the cause, not the symptom: flicker, self-moving position, and intermittent disappearance.

## Verification Delta (2026-09-16)

- Passed: live `get_usage` probe -> `utilization` is a 0-100 percentage (not 0-1) and `resets_at` is an ISO 8601 string with offset, unlike the Codex Unix seconds. Both parsers are separate.
- Passed: `python -m unittest discover` -> 38 tests on Python 3.14.7 and on the deployed Python 3.11.9.
- Passed: live end-to-end read through the widget parser -> 5-hour and weekly windows in 1.9 seconds.
- Passed: deployed to `%LOCALAPPDATA%\Programs\CodexUsageWidget`, killed the running widget, supervisor restarted it, and a taskbar screenshot showed all three columns with reset lines and correct threshold colours (`CX 주 4%` red, both Claude columns green).
- Compact width settled at 308 px: the free taskbar strip between the weather widget and the centred task buttons is about 330 px on this machine, and the buttons slide as apps open, so the gap check moved to a deterministic unit test with a stubbed taskbar layout.
- Passed: detail panel screenshot -> three rows with progress bars at 280x186.
- Cause found for self-moving position: the process never declared DPI awareness, so Tk coordinates were DPI-virtualised while every `user32` rect (taskbar, monitors) was physical. `enable_dpi_awareness()` now runs before `tk.Tk()`, layout constants are scaled through `_s()`, and an auto-hidden taskbar is detected with `ABM_GETSTATE` instead of being anchored to its off-screen rect.
- Cause found for disappearance, part 1: `_refresh_topmost` returned without rescheduling on a single `TclError`, permanently killing the loop. It now reschedules in a `finally`.
- Cause found for disappearance, part 2 (the real one, reproduced live): `Shell_TrayWnd` is topmost too, so after it redraws it can sit above the HUD inside the same band while `WS_EX_TOPMOST` is still set. `SetWindowPos(HWND_TOPMOST)` alone does not move an already-topmost window, so `SWP_SHOWWINDOW` is required to re-insert it; the old build got away with firing that blindly twice a second. The loop now asks `WindowFromPoint` at the centre of the HUD whether we still own our own pixels, and only then issues the repainting raise.
- Cause found for disappearance, part 3: the HWND must be Tk's `wm frame`. `winfo_id()` is the inner Tk window and `GetAncestor(GA_ROOT)` returns it unchanged rather than the `TkTopLevel` wrapper, so raising it moved nothing. It is also resolved fresh each tick: before the first map, `wm frame` still reports the inner window, and caching that value pins the HUD to a window that owns no z-order.
- Passed: burying the live HUD by raising `Shell_TrayWnd` to `HWND_TOPMOST` -> it lost its own centre pixel and recovered in 405 ms. Before the handle fix, the same test never recovered within 10 s.
- Cause found for flicker: `set_prefix` wrote `"Week —"` before `set_window` wrote the value, and the 500 ms topmost timer called `update_idletasks()` with `SWP_SHOWWINDOW` on a layered window. Label writes are now diffed against the last value, the 500 ms timer now only re-inserts z-order when the HUD is really covered, and the snapshot cache is written from `after_idle` instead of the redraw path.

## Requirement Delta (2026-09-16, alerts and logging)

- Alert once when a limit crosses 80% used, and again at 90%, without repeating on every poll. A quota window is identified by its `resets_at`, so a rollover re-arms both thresholds; usage dropping back under a threshold re-arms that threshold too.
- A restart must not re-announce a level the user was already told about, so the tracker is seeded from `last_snapshot.json` without firing.
- Notification path is `Shell_NotifyIcon` with `NIF_INFO`: Windows 10/11 render it as a normal toast and it needs no registered AppUserModelID, unlike the WinRT toast API. The tray icon is added only to carry the notification and removed 12 seconds later, so the HUD leaves nothing in the tray.
- Rotating `widget.log` (256 KB x 3) next to the other state files. Until now every Win32 failure was swallowed silently, which is why the z-order bug could only be found by reproducing it by hand.

## Verification Delta (2026-09-16, alerts and logging)

- Passed: `python -m unittest discover` -> 50 tests on Python 3.14.7 and on the deployed Python 3.11.9.
- Passed: live alert -> a 93% weekly reading produced the toast "CX 주 한도 임박 / 93% 사용 · 남은 7% · R 9/19(토)18:34" and a red border flash on the HUD, both captured in screenshots.
- Passed: deployed install log -> startup, `HUD ready` with mode/position/scale/cached sources, and both `cached -> live` transitions written to `%LOCALAPPDATA%\CodexUsageWidget\widget.log`.
- Passed: restart with Codex already at 96% used produced no toast, confirming the cache seeding.
- Known cosmetic limit: the toast header shows the host process name ("Python"). Changing it needs a Start Menu shortcut carrying an AppUserModelID; not done, because a mismatched AUMID can suppress the notification entirely.

## Requirement Delta (2026-09-16, alerts off by default)

- The HUD already sits in permanent view, so a separate notification is interruption rather than information: threshold alerts are now opt-in (`alerts_enabled=True`) and off by default.
- Bug found in the field before switching them off: Codex returns `resetsAt` a second or two apart between polls (1789810463 then 1789810462), and the tracker compared it exactly, so every poll looked like a fresh quota window and re-announced the same limit every 30 seconds. Cycle comparison is now tolerant (10 minutes); a real rollover moves the reset by hours.
- Passed: `python -m unittest discover` -> 52 tests, including one that replays the observed drift sequence.
- Passed: deployed with alerts off; no further `alert` lines in `widget.log`.

## Requirement Delta (2026-10-13, Claude usage stuck after account switch)

- Symptom: Claude columns froze on 2026-10-12 12:22 values after the user switched the CLI to the F4GE team account and back. `widget.log` showed `claude status live -> error: Claude 한도 응답에 rate_limits가 없습니다.` with no recovery.
- Cause (verified): the CLI debug log showed `fetchUtilization: 200` then `Usage fetch returned a fieldless or non-object body (in-band error)`; one direct call to `/api/oauth/usage` returned `429 rate_limit_error` with `retry-after: 3600`. The widget polled every 90 s (~960 calls/day) and kept polling through the limit, so it never lifted.
- Fix: `rate_limits_available: true` with `rate_limits: null` raises `ClaudeRateLimitedError`; the poller treats it as a cooldown error and reports `rate_limited` (status `WAIT`).
- Revised the same day at the user's request (fresh numbers over caution; a fixed 65-minute wait was too slow): base poll 120 s; while limited retry after 5, 10, 20, 40, then 60 minutes; the first limit of a streak doubles the normal interval (cap 600 s) and every 30 consecutive successes halves it back toward 120 s. The earlier 600 s / 65-minute values are superseded. Polls now run with `--setting-sources local`, because the user's own SessionStart hooks were firing on every poll.
- Values from a source that has failed for longer than its stale window (Codex 5 min, Claude 15 min), or that never refreshed since start-up, are drawn grey instead of in live colours, re-checked every 30 s.
- Passed: `python -m unittest discover` -> 63 tests on the deployed Python 3.11.
- Passed: deployed; log shows `claude status reconnecting -> rate_limited` and the screenshot shows grey Claude columns with live Codex.
- Revised again: the user saw the widget disagree with their real usage while the endpoint stayed limited, and wanted current numbers. Every inference response carries the same 5-hour / weekly utilisation in its headers, and the CLI answers `get_usage` from them once a turn has run (`seedSource: "headers"`). While the endpoint is limited, `ClaudeUsageClient` now runs a one-word Haiku turn (`--system-prompt` replaced, `--tools ""`, `--strict-mcp-config`, `--no-session-persistence`) and asks `get_usage` in the same process; the payload rides on `ClaudeRateLimitedError.payload` and the poller shows it as a live snapshot. Measured cost ~572 input tokens, about $0.0015 equivalent, versus $0.013 with the default system prompt. While limited this runs every 5 minutes.
- Passed: live account, endpoint still limited -> fallback returned 5h 19% / weekly 20% used in 13.5 s; deployed widget logged `claude status cached -> live: rate limited; values from fallback` and the screenshot shows CL 5h 80% / CL 주 80% remaining in live colours.
- Passed: `python -m unittest discover` -> 67 tests.
- Not yet observed: the free endpoint lifting its limit (the widget switches back to it automatically).

## Requirement Delta (2026-10-15, Claude CLI logged out)

- Symptom: Claude columns stopped again; `widget.log` showed `claude status live -> error: 이 계정에서는 Claude 구독 한도를 조회할 수 없습니다.` from 10:39.
- Cause (verified): `claude auth status` reported `loggedIn: false`. `.credentials.json` was rewritten at 10:37 with empty `accessToken`, `refreshToken` and `expiresAt`, while `refreshTokenExpiresAt` was 03:47 the same day. The CLI clears stored tokens on disk when a refresh returns `invalid_grant` (`tengu_oauth_refresh_token_marked_dead_invalid_grant`), so the login simply expired. `get_usage` then answers `subscription_type: null, rate_limits_available: false`, which the widget had reported as a generic error.
- Fix: that signature raises `ClaudeLoggedOutError` (a `ClaudeUnavailableError` with `ui_kind = "logged_out"`). The poller reports the exception's `ui_kind`; the HUD shows `LOGIN` with a red dot and greys the Claude columns at once. Polling continues, so a new `claude auth login` is picked up within one interval with no restart.
- Not fixable in the widget: signing in again is the user's action.
- Passed: 70 tests; live account (logged out) -> `logged_out` detected; deployed HUD shows red dot and grey Claude columns.

## Requirement Delta (2026-10-15, survive login expiry without manual re-login)

- User requirement: re-logging in every couple of days is not acceptable; it must keep working on its own.
- Long-lived tokens from `claude setup-token` are inference-only by design (CLI text: "limited to inference-only for security reasons"), and `get_usage` returns `rate_limits: null` without the profile scope. The stream-json `rate_limit_event` is different: `rate_limit_info.unifiedWindows` is built from the response headers (`Ro(MU())`, utilization as a 0-1 fraction, `resetsAt` in epoch seconds) with no profile-scope gate.
- Design: free `get_usage` stays primary. When it reports a logged-out CLI and a token is stored, run the one-word Haiku probe with `CLAUDE_CODE_OAUTH_TOKEN` set in the child's environment only, collect `rate_limit_event.unifiedWindows`, and fill `rate_limits` from it. This raises `ClaudeTokenFallbackError(payload=...)`; `ClaudeRateLimitedError` shares the new `ClaudeFallbackError` base, so the poller shows the payload live and re-checks the free path every 5 minutes.
- Token storage: Windows Credential Manager generic credential `CodexUsageWidget/ClaudeOAuthToken` (`claude_token_store.py`). One-time setup: `python setup_claude_token.py` (runs `claude setup-token`, takes the token through `getpass`, stores it, then tests it). Never on a command line, never logged.
- Passed: 76 tests, including a fake CLI that emits `rate_limit_event` and checks the token arrived by environment; Credential Manager round trip under a throwaway target.
- Passed: deployed; with no token stored the widget still reports `logged_out` with the new guidance.
- Not yet observed: a real `claude setup-token` token end to end (needs the user's one-time browser approval).
- Correction (2026-10-15): the account was the F4GE team account all along (already `subscriptionType: team` on 2026-09-16), so "it only started after leaving a personal account" was wrong. Verified facts only: CLI unchanged since 2026-09-15 (2.1.272); the login made on 2026-10-13 had a refresh token expiring 2026-10-15 03:47; earlier logins on the same account lasted weeks. Why this one was short is not known.
