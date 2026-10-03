# Imajev Drawing Game

A Python/PyQt6 desktop tic-tac-toe game. Draw an X with multiple mouse strokes, submit it to your local Imajev image service, and let the model choose O. The deterministic rules engine validates every action. Immediate O wins and single-cell blocks are enforced by the visible tactical guard, while Imajev proposes each computer move and chooses ordinary moves. Accepted handwriting stays on the board; ambiguity stays editable.

See the [implementation guide](docs/implementation.md) for architecture, turn flow, model prompting, retries, persistence, and explanatory Mermaid diagrams.

## Run the complete app

`uv run imajev-game` starts the GUI and an app-owned local model service. It checks the pinned assets and warms up the model in the background; install the model once before launching. Run all commands below from this repository's root directory.

### First-time setup (online)

Install Git, Bash, and [uv](https://docs.astral.sh/uv/). The default Imajev-4B NF4 service needs an NVIDIA GPU with working CUDA access. On Windows, run the service commands inside WSL2; check that `nvidia-smi` works there. The GUI needs a desktop display: native Linux, WSLg, or a separate Windows checkout.

Prepare the GUI environment and the model service:

```sh
uv sync --locked
bash scripts/setup_inference.sh
```

The second command installs the separate inference environment, including bitsandbytes, downloads the pinned 4B model and Imajev adapter, and records the asset manifest. Allow several GB of disk space and let setup finish before launching. Downloads happen during this setup, not during gameplay. If you use a native Windows GUI, run `uv sync --locked` in that Windows checkout and run the inference setup in the WSL checkout.

### Every time you play

Start the complete app:

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

The GUI may show **Warming up** while the service loads and its first image request runs. Drawing becomes available when the panel shows **Ready**. Closing the GUI shuts down its own service. An occupied port is reported without attaching to or stopping the listener. To use a separately started service, launch with `--external-inference`; closing that GUI leaves the external service running.

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
- New game stays available during ordinary inference. It is disabled during coaching and until a coaching failure is resolved. The previous job must finish before another request starts; old replies cannot change the new board.
- Expand Diagnostics for model scores and timing. Recognition scores include the unknown probability; opponent scores represent preference.
- Export session saves versioned JSON with full strokes, canonical state, requests and responses. With `diagnostics.save_sessions: true`, JSON and observation PNGs are written under `sessions/`. All records stay local.

## Verify

```sh
uv run pytest
QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_ui.py
uv run python scripts/evaluate.py --help
```

Tests use an explicit fake only in the test suite; normal gameplay always requires Imajev. The exhaustive engine test enumerates all reachable boards. Lifecycle tests exercise reset, stale responses, double submission and recovery. A tiny second game tests the generic controller contract.

Real handwriting accuracy and playing-strength improvement have **not** been established. Shared NF4 strategy learning and subsequent real model decisions were exercised on the RTX 3070; see [learning acceptance](docs/evaluation/learning.md) for latency, sampled memory, and scope. See [acceptance status](docs/evaluation/STATUS.md). Do not interpret unit tests as model or hardware validation.

## Debug mouse input

```sh
uv run imajev-game --debug-input
```

This writes mouse press/move/release coordinates, buttons, stroke completion and ignored-click reasons to the terminal and `logs/input-debug.log` (rotating, local files). `--log-file /path/to/game.log` changes the location. With `--debug-input`, the Diagnostics panel also opens and full session JSON/observation PNGs are saved under `sessions/` for recognition replay. Detailed pointer logging is opt-in; normal launches log model lifecycle and rejected drawing presses to the terminal. Restart the app to enable the flag.

Drawing is disabled until local model startup and image warm-up succeed. The hint below the board and its tooltip explain why input is locked. If no ink appears and the panel shows Warming up or Needs attention, complete setup in `docs/deployment.md`, fix the reported error, then Retry. Managed service logs are in `logs/managed-service.log`.

To replay a saved recognition failure against the current prompt:

```sh
uv run python scripts/replay_recognition.py sessions/<session-id>/session.json
```

Use `--original-prompt` to compare the exact recorded request. Exported sessions with embedded PNGs work too. The replay prints proposed actions and scores; it never changes the live game. Rejections now show whether symbol/cell confidence or abstention caused the failure, including the required threshold.

## Opponent tactics

`opponent.tactical_guard: true` is enabled in `config.yaml`. Imajev receives the board, legal moves, win condition and any immediate O win or X threat. The app records its proposed move. If it overlooks an immediate O win or the one cell needed to block an X win next turn, the rules engine commits that tactical move and labels the correction on the board and in Diagnostics. This is a one-turn rule, not a minimax opponent; Imajev still chooses moves without such a tactic and can miss longer-term threats or forks.

Set `opponent.tactical_guard: false` to disable committed tactical corrections. Ordinary prompts still contain tactical advice; learning mode removes all built-in strategic assistance. Restart the app after changing the setting. `scripts/evaluate.py opponent` evaluates raw model choices; add `--with-tactical-guard` to measure the assisted gameplay policy. The records distinguish `model_proposed_action` from `accepted_action` and give the correction reason.

## Resume a paused game

```sh
uv run imajev-game --debug-input --resume sessions/<session-id>/session.json
```

The app validates and loads the saved board and strokes into a new session, preserving the original record. If it was Imajev's turn, the app reconnects and continues that turn. Retry after an abstention now sends a changed question; it never commits an abstained answer. The first O move includes specific opening guidance: center after an X corner or edge, or a corner after an X center. The same move was checked with the tic-tac-toe oracle for all nine X openings. If a later model request still abstains, the board remains intact and Retry remains available.

To test the model without an opening suggestion, run `uv run --locked imajev-game --config config.no-opening.yaml --debug-input`. This removes the suggested opening from the first O request and retries while keeping the tactical guard on. The window title marks this mode, and saved session records include `opening_suggestion: false`.

## Learning mode

```sh
uv run imajev-game --learning
```

The Learning mode checkbox applies to the next game. Each game retains its original mode, strategy text, and revision, including after resume. Learning uses retained prompt context; model weights do not change. Learning prompts retain board state, rules, legal actions and drawing validation, but omit opening hints, tactical recommendations and corrections. Retries rephrase the question without selecting a move.

A completed loss or draw automatically starts coaching while the final board remains visible. Wins are recorded without coaching; abandoned games and rejected moves do not trigger updates. Compact summaries, resumable sessions, atomic coaching attempts and strategy revisions are saved in `learning/`, independently of debug logging. Ordinary sessions are excluded. Successful updates apply to subsequent games. Inline Diagnostics shows the strategy, coaching input, included game IDs, response, duration and errors. Retry coaching or Continue with previous strategy resolves a failed update; Continue checks readiness before enabling New game.

Configuration defaults:

```yaml
learning:
  enabled: false
  coach_backend: shared
  directory: learning
```

Shared coaching reuses the loaded base model with the PEFT adapter temporarily disabled under the inference lock. It uses greedy generation, disabled thinking, a 3,072-token input budget and at most 512 output tokens. Empty or truncated output keeps the previous revision; failures never silently select another backend.

For optional Ollama coaching, install Ollama and explicitly run `ollama pull <model>` during setup, then configure:

```yaml
learning:
  enabled: true
  coach_backend: ollama
  ollama_url: http://127.0.0.1:11434
  model: <installed-model-name>
```

Ollama requires managed Imajev. The app stops its own Imajev process, requests coaching with `keep_alive: 0`, confirms Ollama has unloaded, and reloads and warms Imajev before gameplay resumes. Unrelated loaded models are reported, never stopped. Gameplay never downloads models.
