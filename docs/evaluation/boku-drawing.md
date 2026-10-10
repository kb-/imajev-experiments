# Boku handwriting experiments

Tested on 2026-10-10 against the running `imajev-4b-nf4` service. These are
recognition experiments, with no training, coaching or move recommendation.

## Method

Replay eight saved human loops and one placement-phase X from sessions
`51b4b238-2021-4a96-bef0-e7d90e1e7631` and
`d1c199a5-ecf8-4f3d-a526-6057cd4865b7`. Inspect drawings before assigning
expected symbols. The first, narrow angular outline is a valid loop; the older
report's description of it as a scribble was incorrect.

Keep the effective-probability threshold at 0.65 for both questions. Keep all
80 location choices plus invalid, independent stroke geometry, abstention,
phase and action-legality checks. Crop around all ink, without selecting a cell.
Use synthetic valid and invalid marks as additional controls.

The script saves each fixture, rendered PNG, request, raw response, accepted
action and latency. It uses an explicitly external service and never starts or
stops a model. The saved examples are a small development set reused while
designing the prompt, so results are not a general accuracy estimate.

## Findings

| Experiment | Result |
| --- | --- |
| Original image/prompt, saved replies evaluated at 0.65 | 3/8 human loops accepted |
| Half-width ink | Restores obscured E2 location, but reduces confidence on other loops |
| Labels farther above pockets | Worsens location association on several examples |
| Separate tight ink-only image for symbol question | Rejects all three difficult loops tested |
| Closed-loop wording plus protected labels, original width | 7/8 human loops accepted |
| Structural outline wording plus protected labels, original width | 8/8 human loops accepted, but accepts two loops in one pocket |
| Exactly-one-loop wording plus protected labels, original width | 8/8 human loops accepted; rejects that multiple-loop case |

Two independent problems explain most failures: imperfect/angular circles did
not confidently match “One handwritten circle”, and thick ink obscured printed
cell IDs. Thinning all drawings is less effective than describing their
structure and preserving readable IDs at their original locations.

The selected symbol question is:

> Identify the structure of the handwritten ink, ignoring the printed grid and labels.

Its criteria are:

- O: Exactly one closed handwritten loop enclosing exactly one empty area. Uneven or angular loops count.
- X: Exactly two handwritten diagonal lines crossing once.
- invalid: Blank, a dot, an open line, scribble, or multiple loops or marks. Two circles are invalid even in the same cell.

The renderer paints recognition labels after ink, with a small background patch.
Boku enables this shared rendering option only for recognition. Stroke width,
the displayed board and tic-tac-toe recognition retain their existing settings.
The application produces pixel-identical images to the outline experiment for
all nine saved drawings.

The eight human loops have symbol effective probabilities 0.8516–0.9599 and
location probabilities 0.9331–0.9948. The placement-phase X is still rejected.
Center/corner circles, a synthetic angular loop and a valid capture X are
accepted. Blank, zigzag, dot, line, open arc, dense scribble and two circles in
different pockets are rejected.

The intermediate outline prompt accepted an adversarial case: two small loops
in the same pocket, as O (0.7751), with correct location (0.9977). The final
exactly-one-loop prompt correctly calls this invalid (0.8746). Separate-pocket
loops also got O with the intermediate prompt, but their invalid location
prevented a move; the final prompt calls their symbol invalid (0.8121). This demonstrates why
symbol confidence alone is insufficient and why the remaining validation stays
in place. The stricter criteria address the observed false accept without lowering
thresholds or bypassing model abstention. Dense scribble still gets O as the
most likely symbol at 0.6085, and is rejected by the unchanged 0.65 threshold.

## Reproduction

With the normal app/service already running:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_drawing \
  --session sessions/51b4b238-2021-4a96-bef0-e7d90e1e7631/session.json \
  --session sessions/d1c199a5-ecf8-4f3d-a526-6057cd4865b7/session.json \
  --controls --variants baseline selected \
  --output logs/boku-drawing-recheck
```

The output directory must be new. `baseline` freezes the original prompt and
unprotected labels; `selected` uses current application rendering/prompt.
`outline` reproduces the intermediate structural experiment; `strict` reproduces
the final exactly-one-loop variant. Saved sessions and
raw experiment outputs remain local, under ignored directories.

Runs: `logs/boku-drawing-screen-{1,2,3,4}` and
`logs/boku-drawing-validation-{1,2,3}`. The first screens compare three difficult
human examples. Validation 1 checks closed-loop wording; validation 2 checks
structural wording with the expanded controls. Screen 4 tests stricter wording
against the same-pocket false accept and difficult valid cases. Validation 3
checks that stricter variant on the entire expanded set.

## Final verification

The strict variant produced the expected outcome in all 21 cases: eight saved
human loops, one placement-phase X and twelve synthetic controls. Application
requests and rendered images exactly match every live strict experiment. Median
request latency was 7.79 seconds. The automated suite passed 266 tests;
the 56 Boku/separation tests passed again after the final wording change. Both
games passed offscreen GUI smoke checks. The already-running GUI retains its
loaded code and needs a restart to use this change.

## Next fresh-input experiment

Collect fresh drawings across center and edge pockets, with small/large loops,
rounded/angular loops and light/heavy overlap with labels. Include capture Xs
and deliberate invalid marks. Count accepted correct moves, rejected valid
drawings and accepted invalid drawings separately. A fresh game is needed to
check the changed application; replay success alone does not establish that
drawing now works reliably for new input.
