# Imajev Drawing Game

A Python/PyQt6 desktop tic-tac-toe game. Draw an X with multiple mouse strokes, submit it to your local Imajev image service, and let the model choose O. The deterministic rules engine validates every action. Immediate O wins and single-cell blocks are enforced by the visible tactical guard, while Imajev proposes each computer move and chooses ordinary moves. Accepted handwriting stays on the board; ambiguity stays editable.

## Run

Install [uv](https://docs.astral.sh/uv/), then from this directory:

```sh
uv sync --locked
uv run imajev-game
```

Works on Windows and Linux with a desktop display. No model or Torch is loaded in the GUI environment. Configure the loopback service and expected model in `config.yaml`; use `uv run imajev-game --config /path/to/config.yaml` for another file. The app waits for the service and performs an image warm-up before enabling drawing. Missing service, timeout, malformed responses and model mismatches remain visible and offer Retry. There is no automatic substitute opponent or symbol detector.

The service must run separately. See [local deployment](docs/deployment.md) for the pinned upstream setup and offline asset checks. On Windows, run the GUI on Windows and the inference environment in WSL2. Model downloads happen during setup, never from the app.

## Controls and records

- Draw inside the square board; mouse release ends a stroke. Submit explicitly ends your turn.
- Undo removes one whole pending stroke; Clear removes all pending ink.
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
