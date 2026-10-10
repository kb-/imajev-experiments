# Boku evaluation fixtures

These files preserve the games and measurements referenced by the capture-trap
and immediate-win experiments. They are committed so a fresh checkout can
inspect the evidence and run the evaluation scripts without local recordings.

| Session fixture | Original session | Decision revisions retained |
| --- | --- | --- |
| [capture-trap-session.json](capture-trap-session.json) | `015e9e8c-b396-44c2-b431-fc059e97bb85` | 25 |
| [missed-win-session.json](missed-win-session.json) | `2fce6e24-d68e-407d-b817-cdd50e448937` | 9, 25, 28, 29, 53 |
| [winning-replay-session.json](winning-replay-session.json) | `4cca755c-7a1f-44d5-ada7-47a7ca1f356a` | 53 |

Session fixtures retain the complete accepted action history, final state,
model settings and relevant decision snapshots (requests, replies and timings).
Ink strokes, image paths, pending ink and unrelated recognition/transport events
are omitted. Every retained state is validated by replay through the rules
engine after removing ink. Board images can be rendered again from these states.
Original recordings remain local and unchanged.

The `boku-*.json` files preserve recorded experiment outputs, including candidate
facts, witness moves, model requests, probabilities and timings where recorded.
Their source-session paths point to the committed fixtures; local model-asset
paths are made relative to the repository, retaining provenance hashes. They are historical
measurements, not expected identical results across hardware or model versions.
Re-running model probes requires the configured inference assets and a running
service; it uses the current code. Fresh output goes to ignored `logs/`, leaving
the archived measurements untouched.

From the repository root, the capture-trap rules check needs no inference service:

```sh
.venv/bin/python -m scripts.evaluate_boku_capture_traps \
  --actions place_D5 place_B3 --output logs/boku-capture-traps-screen.json
```

With the configured inference service already running:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_win_priority \
  --controls --arms named_wins --output logs/boku-win-priority-named-controls.json
```

Both scripts default to the committed session fixtures. See the parent
experiment documents for the exact comparisons and their limitations.
