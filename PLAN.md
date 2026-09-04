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

## Key Decisions

- Prefer the backward-compatible `rateLimits` view for the default Codex weekly value, falling back only to an unambiguous `limitId=codex` entry. Select a separate 300-minute value only from an explicitly named GPT-5.3 entry and label it accordingly; ignore that model entry's weekly window for the default Week display.
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
- The explicitly named GPT-5.3 300-minute window and default Codex 10080-minute window independently drive their labels, percentages, bars, and reset times; a model-specific weekly value never replaces the default weekly value.
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
- [x] Default-weekly/GPT-5.3-five-hour source selection corrected and verified against the current live response.

## Verification

- Confirmed: `codex --version` -> `codex-cli 0.128.0`.
- Confirmed: `codex app-server --help` exits 0 and lists stdio as the default transport.
- Confirmed: live initialize and `account/rateLimits/read` requests succeeded without direct credential access.
- Passed: `python -W error::ResourceWarning -m unittest discover -v` -> 23 tests, including notification interleaving, meter-source selection, weekly-only snapshots, reconnect, active shutdown, taskbar placement, compact/detail toggling, state persistence, off-screen/L-shaped monitor fallback, single-instance locking, and Windows HUD attributes.
- Passed: `python -m compileall -q .`.
- Passed: current live response probe -> `codex_bengalfox` / `GPT-5.3-Codex-Spark` 300-minute remaining `100%`, default `codex` 10080-minute remaining `70%`; final EXE rendered exactly `5.3 5h 100% | Week 70%` and ignored the model-specific weekly value.
- Passed: live child app-server termination -> a new wrapper PID, fresh handshake, and complete snapshot; no extra app-server remained after shutdown.
- Passed: tkinter live smoke -> `LIVE`, `280x130`, topmost true, both percentages and reset labels populated.
- Passed: exact `python codex_usage_widget.py` launch -> widget stayed running with exactly one owned app-server child; the test process tree was removed afterward with no leftover app-server.
- Passed: live compact screenshot -> 226x32 HUD at `x=166, y=1040` inside the 48px taskbar gap; Widgets ended at `x=158` and task buttons began at `x=473`, with no overlap.
- Passed: live `▴` click and detail screenshot -> 280x130 HUD at `x=166, y=894`, eight pixels above the taskbar, with both progress bars and reset times visible.
- Passed: PyInstaller 6.21.0 `--onefile --windowed` build -> `dist\CodexUsageWidget.exe`; direct EXE launch reached live usage through the installed `codex.CMD` app-server chain.
- Passed: current-user `Codex Usage Widget.lnk` created in the Startup folder; COM inspection confirmed the absolute target and working directory, and launching the shortcut opened the live EXE.
- Passed: packaged close/reopen round trip restored compact mode and exact `x=166, y=1040`; old CMD/Node/Codex app-server child processes all exited before the new instance started.
- Passed: launching the EXE again while it was open left the same two PyInstaller process IDs, confirming duplicate suppression.
- Passed: final compact screenshot shows no progress lines and displays brighter 7-point reset labels (`R HH:MM` / `R MM/DD HH:MM`) under both usage percentages.
- Passed: reproduced the real failure with a live process reported visible at `166,1040` but fully covered by `Shell_TrayWnd`; `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE)` restored it immediately.
- Passed: final EXE periodically reasserted its z-order after the taskbar was deliberately raised, remained visible, and left the foreground window unchanged.
- Not run: a disruptive Windows reboot/sign-out; the actual Startup shortcut execution path was tested directly instead.
