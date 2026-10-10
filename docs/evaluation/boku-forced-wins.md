# Boku ordinary forced-win setups

Tested on 2026-10-10 using `imajev-4b-nf4`, four presentation rotations and
zero app move temperature. The starting code was `2855fef`.

## Loss and missing evidence

In [session 2f51b231](fixtures/boku/open-four-session.json), Black F8 at action
25 formed D6–E7–F8. White D5 left both ends open. Black C5 then formed an
open-ended four, threatening B4 and G8. Every legal White reply lost; White D4
was followed by Black G8 and a five-stone win.

The old capture-only setup fact did not flag D5 because the forcing Black C5
move required no capture. This was a limitation of the tactical horizon, not
a recognition, transport or input truncation failure.

## Comparison on the same position

Keep board image, candidate order, model and sampling identical. Model choice
remains authoritative for selecting among legal actions; the application supplies
evidence rather than overriding the choice.

| Prompt | Selected | Raw choice probability | Input tokens | Inference latency |
| --- | --- | --- | --- | --- |
| Saved baseline | D5, losing | 20.84% | 2,869 | 8.25 s |
| Broader sparse forced-win facts | E1, counter-threat | 29.60% | 3,370 | 10.04 s |
| Broader facts plus named defensive actions | C1, counter-threat | 45.79% | 3,431 | 9.70 s |

The broader check finds exactly three actions avoiding the checked losses:
`place_C1`, `place_C5`, `place_E1`. C5 interrupts the open-four setup. C1/E1
creates a White winning threat; if Black continues with C5, White wins at the
other square. G8 prevents this particular open four but allows a separate
capture trap, so it is excluded.

The production prompt supplies `White_defensive_actions` only when at least
one candidate is dangerous, and prioritizes an immediate White win before that
list. An empty list means no candidate is flagged or all candidates lose; it
does not promise safety. All legal actions remain candidates.

## Controls and implementation

The named-defence arm passes the new loss plus all seven existing controls:
A1 win, E5 block, capture-trap prevention (now choosing B3), F4 counter-placement,
E4 capture, F5 win requiring capture and a winning capture choice.
The follow-through probe also plays White C1 → Black C5 and asks Imajev for the
next move, verifying it chooses the winning E1. These are a small development
set, not a general playing-strength measurement.

`boku-v5-forced-win-defence` uses `allows_Black_forced_win` in place of the
capture-only flag. It checks a completed Black setup, every completed White
reply, and the existence of a winning Black continuation. Mandatory captures,
forbidden pockets, White counter-wins and terminal draws are included. A White
placement with several capture choices is dangerous only when every completion
loses within the checked horizon. The old capture-only evaluator remains
available for historical experiments.

Immediate Black winning placements are shortlisted using five-cell windows
with four Black stones and one empty cell, then checked by the game engine.
This is complete for a placement win because capturing only removes enemy
stones. Regression tests compare it against exhaustive legal-move evaluation.
Preparation took 1.26 seconds on this loss and 1.06 seconds on the prior
capture-trap control; the older capture-only implementation took about 14
seconds on the latter fixture. These are individual local timings, not a
controlled performance benchmark.

Both quoted modes and retries receive the new evidence; Original mode and
tic-tac-toe prompts are unchanged. Request preparation remains in the existing
serialized background worker. All live requests fit the 4,096-token service
limit. The full suite passed **303 tests**, including validation of the archived
results, and both game GUI smoke checks passed.

## Reproduction

With the configured service already running, from the repository root:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_forced_wins --controls
```

The script starts/stops no processes and writes fresh results to ignored
`logs/boku-forced-wins.json`. The committed [recorded results](fixtures/boku/boku-forced-wins.json)
retain exact requests, replies, timings and the follow-through result.
