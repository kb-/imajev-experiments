# Imajev Drawing Game

A Python/PyQt6 desktop tic-tac-toe game. Draw an X with multiple mouse strokes, submit it to your local Imajev image service, and let the model choose O. The deterministic rules engine validates every action. Immediate O wins and single-cell blocks are enforced by the visible tactical guard, while Imajev proposes each computer move and chooses ordinary moves. Accepted handwriting stays on the board; ambiguity stays editable.

See the [implementation guide](docs/implementation.md) for architecture, turn flow, model prompting, retries, persistence, and explanatory Mermaid diagrams.

## Run the complete app

The app needs **two running processes**: the local model service and the desktop GUI. `uv run imajev-game` starts only the GUI; it does not start or install the model service. Run all commands below from this repository's root directory.

### First-time setup (online)

Install Git, Bash, and [uv](https://docs.astral.sh/uv/). The default Imajev-4B NF4 service needs an NVIDIA GPU with working CUDA access. On Windows, run the service commands inside WSL2; check that `nvidia-smi` works there. The GUI needs a desktop display: native Linux, WSLg, or a separate Windows checkout.

Prepare the GUI environment and the model service:

```sh
uv sync --locked
bash scripts/setup_inference.sh
```

The second command installs the separate inference environment, including bitsandbytes, downloads the pinned 4B model and Imajev adapter, and records the asset manifest. Allow several GB of disk space and let setup finish before launching. Downloads happen during this setup, not during gameplay. If you use a native Windows GUI, run `uv sync --locked` in that Windows checkout and run the inference setup in the WSL checkout.

### Every time you play

**Terminal 1 — start the model service and leave it running:**

```sh
bash scripts/launch_inference.sh
```

This verifies the prepared assets and starts the default NF4 service offline at `http://127.0.0.1:8765`. Start only one service on this port. You do not also need to run `launch_inference_4b_nf4.sh`: the standard launcher already delegates to it.

**Terminal 2 — start the GUI:**

```sh
uv run --locked imajev-game
```

For mouse logging, inline Diagnostics, and automatic session recordings, use this instead:

```sh
uv run --locked imajev-game --debug-input
```

To keep the opening suggestion disabled during debugging:

```sh
uv run --locked imajev-game --config config.no-opening.yaml --debug-input
```

The GUI may show **Warming up** while the service loads and its first image request runs. Drawing becomes available when the panel shows **Ready**. If the service is already running, start only the GUI. Closing the GUI leaves the service running; press Ctrl+C in Terminal 1 when you want to stop it.

After first-time setup, you can add `--offline` to the GUI command (`uv run --locked --offline imajev-game`) to prevent uv from using the network. The GUI environment must already be installed for this to work.

### Check startup or recover

From the environment running the GUI, check the service:

```sh
curl --fail http://127.0.0.1:8765/v1/models
curl --fail http://127.0.0.1:8765/v1/status
```

The models response must report `loaded: true`, `backend: torch`, and `model: imajev-4b-nf4`. Status reports whether it is busy. Native Windows PowerShell equivalents are in the [deployment guide](docs/deployment.md#gui-setup-linux-or-windows).

If the GUI shows **Needs attention**, read the message, fix the service issue, and click Retry. Missing assets or a missing bitsandbytes installation require rerunning `bash scripts/setup_inference.sh` while online. Use the supplied launcher rather than a bare upstream server command: it configures NF4 loading, the trained readout, calibration, offline mode, and safe retry handling. The inference launcher uses its prepared environment without syncing it; manually syncing that environment can remove the separately installed bitsandbytes dependency.

The GUI and inference environments are separate; no model or Torch is loaded in the GUI environment. The default `config.yaml` and `config.no-opening.yaml` expect NF4. For the legacy 2B profile, use its matching configuration and explicit scripts documented in [local deployment](docs/deployment.md#optional-imajev-2b-profile). Configure another loopback service using `uv run --locked imajev-game --config /path/to/config.yaml`. Missing service, timeout, malformed responses, and model mismatches remain visible and offer Retry.

## Controls and records

- Draw inside the square board; mouse release ends a stroke. Submit explicitly ends your turn.
- Undo removes one whole pending stroke; Clear removes all pending ink.
- The first game starts with you. Each click on New game alternates the starter between you (X) and Imajev (O); the right panel identifies who started.
- New game stays available during inference. The previous job must finish before another request starts; old replies cannot change the new board.
- Expand Diagnostics for model scores and timing. Recognition scores include the unknown probability; opponent scores represent preference.
- Export session saves versioned JSON with full strokes, canonical state, requests and responses. With `diagnostics.save_sessions: true`, JSON and observation PNGs are written under `sessions/`. All records stay local.

## Verify

```sh
uv run pytest
QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_ui.py
uv run python scripts/evaluate.py --help
```

Tests use an explicit fake only in the test suite; normal gameplay always requires Imajev. The exhaustive engine test enumerates all reachable boards. Lifecycle tests exercise reset, stale responses, double submission and recovery. A tiny second game tests the generic controller contract.

Real handwriting accuracy, offline GPU play and RTX 3070 memory/latency have **not** been established in this environment. See [acceptance status](docs/evaluation/STATUS.md). Do not interpret unit tests as model or hardware validation.

## Debug mouse input

```sh
uv run imajev-game --debug-input
```

This writes mouse press/move/release coordinates, buttons, stroke completion and ignored-click reasons to the terminal and `logs/input-debug.log` (rotating, local files). `--log-file /path/to/game.log` changes the location. With `--debug-input`, the Diagnostics panel also opens and full session JSON/observation PNGs are saved under `sessions/` for recognition replay. Detailed pointer logging is opt-in; normal launches log model lifecycle and rejected drawing presses to the terminal. Restart the app to enable the flag.

Drawing is disabled until local model startup and image warm-up succeed. The hint below the board and its tooltip explain why input is locked. If no ink appears and the panel shows Warming up or Needs attention, start the local service using `scripts/launch_inference.sh` after completing the setup in `docs/deployment.md`, then Retry.

To replay a saved recognition failure against the current prompt:

```sh
uv run python scripts/replay_recognition.py sessions/<session-id>/session.json
```

Use `--original-prompt` to compare the exact recorded request. Exported sessions with embedded PNGs work too. The replay prints proposed actions and scores; it never changes the live game. Rejections now show whether symbol/cell confidence or abstention caused the failure, including the required threshold.

## Opponent tactics

`opponent.tactical_guard: true` is enabled in `config.yaml`. Imajev receives the board, legal moves, win condition and any immediate O win or X threat. The app records its proposed move. If it overlooks an immediate O win or the one cell needed to block an X win next turn, the rules engine commits that tactical move and labels the correction on the board and in Diagnostics. This is a one-turn rule, not a minimax opponent; Imajev still chooses moves without such a tactic and can miss longer-term threats or forks.

Set `opponent.tactical_guard: false` to measure the model's unassisted play. Restart the app after changing the setting. `scripts/evaluate.py opponent` evaluates raw model choices; add `--with-tactical-guard` to measure the assisted gameplay policy. The records distinguish `model_proposed_action` from `accepted_action` and give the correction reason.

## Resume a paused game

```sh
uv run imajev-game --debug-input --resume sessions/<session-id>/session.json
```

The app validates and loads the saved board and strokes into a new session, preserving the original record. If it was Imajev's turn, the app reconnects and continues that turn. Retry after an abstention now sends a changed question; it never commits an abstained answer. The first O move includes specific opening guidance: center after an X corner or edge, or a corner after an X center. The same move was checked with the tic-tac-toe oracle for all nine X openings. If a later model request still abstains, the board remains intact and Retry remains available.

To test the model without an opening suggestion, run `uv run --locked imajev-game --config config.no-opening.yaml --debug-input`. This removes the suggested opening from the first O request and retries while keeping the tactical guard on. The window title marks this mode, and saved session records include `opening_suggestion: false`.
