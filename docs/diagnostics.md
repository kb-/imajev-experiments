# Diagnosing a frozen or vanished game window

Every app launch installs lifecycle diagnostics, including ordinary launches without `--debug-input`. Files live beside the selected application log: normally `logs/`, or the directory supplied with `--log-file`.

| File | Evidence |
| --- | --- |
| `app-lifecycle.log` | Rotating INFO/error history without mouse-move traces: startup, session/revision, GUI state, pending request and age, owned model PIDs, Python/Qt exceptions, worker tracebacks, and cleanup. |
| `app-run-<PID>.json` | Latest GUI heartbeat, session file, game, revision, phase, readiness, pending-stroke count, active request, GUI RSS, model processes, and final exit/cleanup status. Updated on state changes and roughly every two seconds while the Qt loop runs. |
| `app-crash-<PID>.log` | Python fatal-signal traces for all threads, watchdog-requested thread dumps, and raw termination-signal notices. Kept open for the duration of the app. |
| `app-watchdog-<PID>.log` | Independent Linux/WSL process monitoring: heartbeat stalls, stack-dump requests, recovery, and GUI disappearance. |
| `app-run-<PID>.exit.json` | Written by the watchdog if the GUI disappears without a completed lifecycle record. Includes its last known session/request and which recorded model processes still survive. |

The PID appears in the startup log and filenames. Python's main, thread and unraisable exception hooks preserve tracebacks; Qt warnings, critical messages and fatal messages are logged with their source. Background request exceptions include their ticket and full traceback. Managed Python inference processes enable `PYTHONFAULTHANDLER` by default, sending native Python crash stacks to their existing service stderr logs.

On Linux/WSL, an independent watchdog checks the GUI process identity, including its start time to avoid mistaking a reused PID for the original app. If the Qt heartbeat stops for 30 seconds, it logs the last request/state and requests an all-thread stack dump via `SIGUSR1` when available. If the GUI recovers, that is logged separately. This monitor does not kill models or alter game state. A slow background inference request with a responsive Qt loop is recorded as a long-running request, rather than a frozen GUI.

`SIGINT`, `SIGTERM`, and `SIGHUP` are recorded and dispatched through the existing window-close/owned-service cleanup flow once the Qt loop can respond. Normal completion records a final marker and whether cleanup was confirmed. Configuration and resume errors are logged before their error dialogs appear. Per-process artifacts retain evidence across launches; the rotating lifecycle log is separate from the high-volume pointer log.

For a new occurrence, inspect the latest run JSON and its matching crash/watchdog logs first, then the lifecycle and relevant managed-service log. The snapshot links to the saved session when recording is enabled. It contains diagnostic state only and does not replace session recording or save pending ink itself.

A missing heartbeat identifies an unresponsive event loop, not its cause; the thread dump supplies that context. SIGKILL cannot run Python handlers. If the watchdog survives, it can record abrupt GUI disappearance and surviving model PIDs, but OS/kernel logs are still needed to distinguish an OOM kill from another external kill. A whole-WSL shutdown can also terminate the watchdog, leaving only the last snapshot and an incomplete lifecycle marker. The independent process monitor uses Linux `/proc`; other platforms retain the in-process exception, fatal-trace, signal, and heartbeat logging.
