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
