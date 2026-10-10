# Boku immediate-win prompt experiment

Tested on 2026-10-10 after committing capture-trap integration as `82c79dd`.
This experiment changes requests sent to the external service; it does not
modify the running GUI, its strategy ledger or production prompt builder.

## Missed win and controlled comparison

Use the saved revision-53 decision from session
`2fce6e24-d68e-407d-b817-cdd50e448937`. After Black F6, White can win with A1,
completing A1–B2–C3–D4–E5. The rules engine independently verifies A1 is the
only winning legal action. Its existing candidate already has `wins_now: true`,
and the strategy begins `win immediately`. The model instead chose B5.

Replay identical state, legal candidate ordering, board image, model and four
presentation rotations. Modify only the stated request fields. Every candidate
remains available; there is no application-side action override.

| Arm | Change | Selected | A1 probability | B5 probability | Input tokens |
| --- | --- | --- | --- | --- | --- |
| Baseline | Original request | B5 | 6.90% | 27.02% | 2,898 |
| Explicit priority | Append `If any candidate has wins_now=true, choose one of those candidates.` | B5 | 5.98% | 33.55% | 2,912 |
| Prefix priority | Put that sentence first | B5 | 10.09% | 25.70% | 2,912 |
| Named winning actions | Add a winning-action list and instruction below | A1 | 95.36% | 0.14% | 2,933 |
| Plain winning result | Add a natural-language winning result to A1, plus explicit priority | B5 | 17.90% | 19.48% | 2,935 |

The successful request addition is:

```json
"immediate_White_win_actions": ["place_A1"]
```

Append this to the question instructions:

> If immediate_White_win_actions is nonempty, choose one action from that list before considering any other action.

When the list is empty, existing strategy and candidate facts continue to apply.
For a winning placement requiring capture, the list identifies the placement;
the subsequent capture request must still identify a winning capture. The
experiment includes both stages as controls.

The exact proposed boolean-priority sentence failed despite fitting the input
limit. Naming the winning actions succeeds on this fixture. The comparison
demonstrates sensitivity to evidence representation, not a diagnosis of the
model's internal reasoning or a guarantee of future instruction compliance.

## Controls and artifacts

The explicit-priority arm also passed six control decisions: E5 immediate
block, C4 capture-trap defence, F4 counter-placement, E4 counter-capture,
F5 win requiring mandatory capture, and a winning capture choice. Its failure
on the actual missed A1 win is therefore material despite these passes.

The named-winning-actions arm passed **all seven decisions**, including the
actual A1 win and all six controls above. Requests used 2,345–3,497 input
tokens, below the 4,096-token service limit.

Commands (an existing service is required):

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_win_priority --controls
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_win_priority \
  --arms prefix_win_priority named_wins plain_win_result \
  --output logs/boku-win-priority-screen-2.json
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_win_priority \
  --controls --arms named_wins \
  --output logs/boku-win-priority-named-controls.json
```

Each output saves requests, raw replies, independently verified winning
actions, chosen actions, pass/fail and latency. The script starts or stops no
process and changes no saved game. Source sessions and raw outputs remain
local under ignored directories. These cases are a small development set,
not a general accuracy or playing-strength measurement.

## Integration

Prompt version `boku-v4-win-priority` derives `immediate_White_win_actions`
from existing computed candidate facts in both Boku quoted modes, with no
additional search. Nonwinning positions receive an empty list. All candidates
and per-capture evidence remain available, and retries retain the list and
instruction. The model still selects the action. Original mode is unchanged.

Regression tests cover the A1 winning line, mandatory-capture placement and
capture choices, nonwinning positions, retries and Original-mode isolation.
The experiment script removes this addition before constructing its historical
baseline, so the comparison remains reproducible after integration.

Verification: **283 tests passed**, and both Boku and tic-tac-toe offscreen GUI
smoke checks passed. The debug app resumed the real revision-53 position after
Black F6 with the new prompt. Imajev chose A1 (95.36% raw choice probability)
in 8.72 seconds and the rules engine confirmed a White win. The replay record
is `sessions/4cca755c-7a1f-44d5-ada7-47a7ca1f356a/session.json`; the source game
was preserved.
