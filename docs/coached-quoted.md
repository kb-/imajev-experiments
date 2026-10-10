# Coached quoted play

In tic-tac-toe, Coached quoted keeps Quoted's matrix board, coordinate grid, bare cell choices and candidate fields (`position`, `wins_now`, `blocks_X_win_next_turn`, `allows_X_win_next_turn`). Its first strategy contains only:

1. win immediately
2. otherwise stop X winning next turn

The list is prompt context, not executable rules or model training. Earlier rules override later ones. The coach can rewrite or reorder every rule; the app does not enforce the initial two rules or supply advanced fork advice. Recognition validation, legal moves and forced moves still apply. Ordinary Original and Quoted retain their configured assistance.

Boku has the same three prompt choices and a Move variety selector. Its quoted and initial coached strategy is `win immediately`, then `otherwise prevent Black winning next turn`. The coach receives the complete Boku rules and axial geometry in every stage, including capture and reserve rules. Its strategy and history never mix with tic-tac-toe. See [Boku details](boku-implementation.md).

## Run and select a backend

After the normal inference setup:

```bash
uv run --locked --offline imajev-game --config config.coached-quoted.yaml
```

Alternatively select **Coached quoted** in the GUI before your first move; the selection applies immediately on an empty new game, even during warm-up. After play begins, click New game to apply a different mode. Resumed games retain their saved settings. Entering coached mode before play snapshots the current rule list and revision; changing variety alone preserves that snapshot. Normal startup launches the pinned Imajev child, checks readiness and warms up. Closing waits for active work and stops owned processes. Port conflicts are visible; an existing listener is never silently attached to or terminated.

For a separately managed service, use `--external-inference`. Shared coaching requires restarting that service with the updated local launcher so `/v1/status` reports `coaching: true` and `coach_protocol: 2`.

Shared Qwen is the default: it uses the loaded NF4 base's language head with the Imajev PEFT adapter temporarily disabled. Greedy generation disables thinking and limits input to 4,096 tokens and output to 512 tokens. The adapter is restored on every exit and the trained decision readout stays intact.

For Ollama, explicitly install Ollama and the desired model outside gameplay, then configure:

```yaml
learning:
  coach_backend: ollama
  model: your-installed-model:tag
  ollama_url: http://127.0.0.1:11434
  directory: learning/coached-quoted
```

Ollama requires managed Imajev. The app reuses a daemon or launches its own `ollama serve`, never downloads models, and never unloads unrelated models. Loaded models that prevent exclusive GPU use are reported. Generation uses `stream: false`, `keep_alive: 0`, temperature zero, thinking disabled, 8,192 context tokens and 512 output tokens. Context packing uses a conservative 18,000-byte limit for the complete messages; oversized history is summarized rather than silently truncated.

`config.coached-quoted.ollama.yaml` selects the locally tested `qwen3.6:latest` model. Use it only after that model is installed. The shared-base configuration remains `config.coached-quoted.yaml`.

## Updates and history

Only a completed human win triggers coaching. Draws and Imajev wins are recorded for the next update. Abandonment, recognition rejection and decision abstention do not trigger coaching. An abstention leaves the board intact and offers ordinary Retry with the same strategy.

The coach receives the previous ordered list and all completed coached games since the last successful update, with window outcome counts. The triggering loss comes first. Compact records retain accepted move sequences, starting player, model proposal counts, abstentions and attempts. Raw ink, screenshots and transport details are excluded.

Coaching first requests a diagnosis of the triggering loss, then supplies that diagnosis to a separate rule-revision request. The diagnosis identifies a mistaken move, the opponent's continuation, a legal alternative and a general lesson. Empty, oversized or truncated diagnoses stop the update. The same backend performs both stages under the managed GPU workflow. Thinking is disabled through the Ollama API or shared tokenizer setting; prompt instructions do not control that option.

Each game explicitly labels the result for O. The triggering loss also includes boards before and after accepted O moves, legal cells and the next accepted X move. The eight winning lines are supplied as coordinates. This context is reconstructed from the game rules; it contains no solver-selected alternatives or prescribed new strategy. The prompt asks the coach to identify the earliest avoidable mistake and retain useful existing rules without a generic checklist.

If the window exceeds context, the pipeline summarizes batches, reduces those summaries as needed and retains small triggering losses verbatim. Long Boku losses are reduced through ordered replay segments, retaining an exact terminal board and complete coverage. All stages include the game’s rules and geometry. Coverage IDs and intermediate results remain recorded locally. Oversized indivisible segments or failed reduction produce a visible failure rather than omitted history.

The coach returns numbered rules, as in the earlier learning experiment; the API normalizes them into an ordered `strategy` list. A JSON strategy object is also accepted, including harmless surrounding code fences or backticks. Extra prose and malformed content are rejected. The normalized list contains: 1–12 nonempty rules, at most 160 characters per rule and 2,048 UTF-8 bytes overall. Empty, malformed, oversized or truncated responses leave the prior strategy intact. The prompt asks for at most 120 words. Structural validation does not prove tactical correctness.

A successful atomic ledger commit saves the new revision and consumes the history window together. Failed coaching or **Continue with previous strategy** leaves the window available for the next loss. **Retry coaching** retries the update. Continue first establishes that Imajev is ready; it cannot bypass active GPU work or an unconfirmed Ollama unload. New game stays disabled until success or successful recovery.

The panel displays the numbered strategy and revision. Inline Diagnostics displays a readable **COACH DIAGNOSIS** as soon as it arrives, while rule revision continues. It also includes requests, history coverage, responses, timings and failures. Diagnosis remains visible if revision fails and is preserved across resume. The status changes between Diagnosing loss, Studying games, Updating strategy and Reloading Imajev. The completed board remains visible during coaching. A displayed diagnosis is the model's explanation; structural checks do not establish that it is tactically correct.

## Storage and resume

Tic-tac-toe’s default local directory is `learning/coached-quoted/`, ignored by Git. Boku uses its isolated `boku/` subdirectory. It contains `strategy.json`, completed summaries under `games/`, resumable records under `sessions/` and coaching attempts under `attempts/`. Coached sessions are saved even when debug logging is off. Existing `learning/` free-text experiments and ordinary sessions are not imported.

```bash
uv run --locked --offline imajev-game --config config.coached-quoted.yaml \
  --resume learning/coached-quoted/sessions/<game-id>/session.json
```

A resumed coached game retains its original logical ID and strategy snapshot. The ledger prevents a restored terminal loss from committing a second successful update. Old ordinary records remain readable. To start an independent experiment, configure a fresh learning directory; retain the old directory as an archive.

## Verification

```bash
QT_QPA_PLATFORM=offscreen uv run --offline pytest
QT_QPA_PLATFORM=offscreen uv run --offline python -m scripts.smoke_ui
QT_QPA_PLATFORM=offscreen uv run --offline python -m scripts.validate_coached
```

Live validation owns its service and an isolated ledger under `logs/`. It plays up to six games with a scripted legal X opponent until a loss produces an update, then plays another game and checks strategy injection. The solver controls the test's human opponent only. It never supplies decisions or lessons to Imajev. The validation reports a failure if no loss occurs; it does not fabricate a loss to claim success. Run it with the GUI and existing service closed. Use `--config` for an explicitly configured Ollama model.

Passing lifecycle tests establishes persistence and safe execution, not stronger play. As the coach improves the rules, draws may become common again.

See the [loss coach context experiments](evaluation/loss-coach-context.md) for measured shared-base, Qwen3.5 and Qwen3.6 results and their limitations.


### Windows Ollama with a WSL GUI

If Ollama is already running on Windows but its localhost API is unavailable inside WSL, the service manager starts an owned loopback relay using Windows PowerShell. Windows Ollama continues to use its own localhost endpoint; no Windows binding or firewall change is required. The relay forwards only the model listing, status, coaching and unload APIs, and is stopped with the app. The Windows daemon itself is reused and left running. Install the selected model explicitly in Windows before starting; no Linux Ollama installation is required for this route.
