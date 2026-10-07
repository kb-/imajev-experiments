# Imajev Drawing Game

A Python/PyQt6 desktop tic-tac-toe game. Draw an X with multiple mouse strokes, submit it to your local Imajev image service, and let the model choose O. The deterministic rules engine validates every action. Immediate O wins and single-cell blocks are enforced by the visible tactical guard, while Imajev proposes each computer move and chooses ordinary moves. Accepted handwriting stays on the board; ambiguity stays editable.

See the [implementation guide](docs/implementation.md) for architecture, turn flow, model prompting, retries, persistence, and explanatory Mermaid diagrams.

## Run the complete app

`uv run imajev-game` starts the desktop GUI and manages its own local Imajev service. It does not install models. Run commands from this repository's root directory after setup.

### First-time setup (online)

Install Git, Bash, and [uv](https://docs.astral.sh/uv/). The default Imajev-4B NF4 service needs an NVIDIA GPU with working CUDA access. On Windows, run the service commands inside WSL2; check that `nvidia-smi` works there. The GUI needs a desktop display: native Linux, WSLg, or a separate Windows checkout.

Prepare the GUI environment and the model service:

```sh
uv sync --locked
bash scripts/setup_inference.sh
```

The second command installs the separate inference environment, including bitsandbytes, downloads the pinned 4B model and Imajev adapter, and records the asset manifest. Allow several GB of disk space and let setup finish before launching. Downloads happen during this setup, not during gameplay. If you use a native Windows GUI, run `uv sync --locked` in that Windows checkout and run the inference setup in the WSL checkout.

### Every time you play

**Start the app:**

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

The GUI shows **Warming up** while the service loads and its first image request runs. Drawing becomes available when the panel shows **Ready**. Closing the app waits for active work and stops its owned processes in the background.

An existing listener on port 8765 is reported without attachment or termination. To keep a separately managed service, run `bash scripts/launch_inference.sh` in one terminal and `uv run --locked imajev-game --external-inference` in another. Closing an external-mode GUI leaves that service running. Native Windows GUIs connecting to a WSL service must use external mode. Ollama coaching requires managed inference.

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
- During coaching or recovery, New game waits until the update succeeds or you explicitly continue with the previous strategy.
- Expand Diagnostics for model scores and timing. Recognition scores include the unknown probability; opponent scores represent preference.
- Export session saves versioned JSON with full strokes, canonical state, requests and responses. With `diagnostics.save_sessions: true`, JSON and observation PNGs are written under `sessions/`. All records stay local.

## Verify

```sh
uv run pytest
QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_ui.py
uv run python scripts/evaluate.py --help
```

Tests use an explicit fake only in the test suite; normal gameplay always requires Imajev. The exhaustive engine test enumerates all reachable boards. Lifecycle tests exercise reset, stale responses, double submission and recovery. A tiny second game tests the generic controller contract.

See [acceptance status](docs/evaluation/STATUS.md) for existing model measurements, and [coached quoted](docs/coached-quoted.md) for the new coaching workflow and validation command. Unit tests do not establish model playing strength.

## Debug mouse input

```sh
uv run imajev-game --debug-input
```

This writes mouse press/move/release coordinates, buttons, stroke completion and ignored-click reasons to the terminal and `logs/input-debug.log` (rotating, local files). `--log-file /path/to/game.log` changes the location. With `--debug-input`, the Diagnostics panel also opens and full session JSON/observation PNGs are saved under `sessions/` for recognition replay. Detailed pointer logging is opt-in; normal launches log model lifecycle and rejected drawing presses to the terminal. Restart the app to enable the flag.

Drawing is disabled until local model startup and image warm-up succeed. The hint below the board and its tooltip explain why input is locked. If startup fails, read the inline error and service log, fix setup or the port conflict, then Retry. External mode requires starting the service separately.

To replay a saved recognition failure against the current prompt:

```sh
uv run python scripts/replay_recognition.py sessions/<session-id>/session.json
```

Use `--original-prompt` to compare the exact recorded request. Exported sessions with embedded PNGs work too. The replay prints proposed actions and scores; it never changes the live game. Rejections now show whether symbol/cell confidence or abstention caused the failure, including the required threshold.

## Opponent tactics

The [non-thinking prompting experiment](docs/evaluation/prompting-strategy-results.md)
compares twelve context/output variants, including the matrix-board prompt and
structured candidate consequences. Expanded consequences improve sampled move
agreement but regress on immediate tactics and full games. Quoted is now the
gameplay default by user choice; the experiment did not establish a holdout
improvement for this variant. The [evaluation guide](docs/evaluation/prompting-strategy.md)
documents the reproducible harness and local artifacts.

`opponent.tactical_guard: true` is enabled in `config.yaml`. Imajev receives the board, legal moves, win condition and any immediate O win or X threat. The app records its proposed move. If it overlooks an immediate O win or the one cell needed to block an X win next turn, the rules engine commits that tactical move and labels the correction on the board and in Diagnostics. This is a one-turn rule, not a minimax opponent; Imajev still chooses moves without such a tactic and can miss longer-term threats or forks.

New games use the experiment's **Quoted** prompt by default. To switch back, select **Original** under **Move prompt** in the GUI, then click **New game**. The current game keeps its prompt until you start another game. Quoted uses the board grid, coordinate grid, five strategy priorities, and per-cell tactical consequences from the tested request, with bare cell IDs as choices. It does not add a suggested opening; the tactical guard still follows your configuration. Saved/exported sessions record the prompt, and resuming restores it.

You can also start directly in this mode:

```bash
uv run --locked --offline imajev-game --config config.quoted.yaml
```

Set `opponent.prompt_variant: legacy` to start with the original prompt. The `config.no-opening.yaml` profile explicitly keeps the original prompt without opening guidance. Older saved sessions without a prompt field resume with the original prompt.

### Coached quoted

Select **Coached quoted**, then New game, or launch directly:

```bash
uv run --locked --offline imajev-game --config config.coached-quoted.yaml
```

It uses Quoted's board and candidate facts, initially with only `win immediately` and `otherwise stop X winning next turn`. Imajev follows the rules without tactical move corrections. After a loss only, the coach revises the rule list in descending priority order using all completed coached games since the last successful update. Draws and wins contribute history but do not trigger coaching; abstentions remain ordinary Retry decisions. Valid updates apply automatically to the next game and persist across restarts.

Shared Qwen coaching is the default. Ollama is optional and requires an explicitly installed model and managed GPU swapping. Rules, revision and coaching Diagnostics appear inline. See [setup, storage and recovery](docs/coached-quoted.md).

Set `opponent.tactical_guard: false` to measure the model's unassisted play. Restart the app after changing the setting. `scripts/evaluate.py opponent` evaluates raw model choices; add `--with-tactical-guard` to measure the assisted gameplay policy. The records distinguish `model_proposed_action` from `accepted_action` and give the correction reason.

## Resume a paused game

```sh
uv run imajev-game --debug-input --resume sessions/<session-id>/session.json
```

The app validates and loads the saved board and strokes into a new session, preserving the original record. If it was Imajev's turn, the app reconnects and continues that turn. Retry after an abstention now sends a changed question; it never commits an abstained answer. The first O move includes specific opening guidance: center after an X corner or edge, or a corner after an X center. The same move was checked with the tic-tac-toe oracle for all nine X openings. If a later model request still abstains, the board remains intact and Retry remains available.

To test the model without an opening suggestion, run `uv run --locked imajev-game --config config.no-opening.yaml --debug-input`. This removes the suggested opening from the first O request and retries while keeping the tactical guard on. The window title marks this mode, and saved session records include `opening_suggestion: false`.
