# Loss acknowledgement experiment: dropped from gameplay

Tested on 2026-10-10 with `imajev-4b-nf4`, four rotations and zero app move
temperature. The initial comparison changed only experimental requests.
The option was briefly integrated in commit `af950d7`, then dropped after
inconsistent play and user reports of false acknowledgements. The retained
gameplay prompt is `boku-v5-forced-win-defence`: legal placements/captures plus
the service's implicit unknown answer. Unknown pauses the turn for Retry;
it does not declare a loss or trigger loss coaching.

The later shared [retry limit](../retry-forfeit.md) ends a session after three
rejected retries. That deterministic application rule records a forfeit, not
a model judgement or a rules-engine board victory. A single abstention still
does not end the game.

## Why it was dropped

- A separate loss question answered **no** with 94.09% raw choice probability
  in a position where every White action allowed a Black win next turn.
  A confident answer was not a reliable diagnosis.
- Adding an acknowledgement choice produced an acknowledgement in that same
  position, despite the separate question's contrary answer. These different
  prompts did not demonstrate a stable understanding of defeat.
- Twelve requests over six development positions passed, but later GUI play
  produced user reports of false acknowledgements. Exact board/request fixtures
  for those reports were not isolated, so this report cannot say whether they
  were rejected model proposals or engine-accepted resignations, or establish
  that the engine's proof was incorrect. The small controls did not establish
  reliable behavior across games.
- Adding the option and its instructions also changed ordinary move choices.
  A [full replay with v6](fixtures/boku/boku-acknowledgement-opening-replay.json)
  answered E9 to Black F10, versus the recorded F1;
  the next recorded Black E9 was then illegal. This comparison changes the
  prompt, not just the disposition at the final position. Temperature was zero.
- Abstention itself does not establish that Imajev knows it has lost. On the
  exact v5 position, rephrasing the question with the existing Retry instruction
  was enough to obtain a legal board action without resignation.

The decision is to keep bounded tactical evidence and normal retries while
removing acknowledgement from production candidates, outcome handling, saved
game state and coaching. The experimental script and results below remain
available for research; they do not enable resignation in the app.

## Position and meaning

At revision 32 of [session f04c80d5](fixtures/boku/abstained-loss-session.json),
Black H7 → capture G8 leaves G8 forbidden for White's next placement.
Every one of White's 53 legal actions allows Black to win at G8 next turn.
The board is still ongoing, but the engine proves a forced loss. Imajev's
original response abstained, reporting 35.59% unknown probability.

The experimental `has_lost` means every legal White action permits an immediate
Black win or a verified Black setup after which every complete White reply
permits a Black win. False means the bounded check has not proved defeat;
it does not prove the position is generally safe. Ground truth is recomputed
from rules-engine facts, never inferred from an empty defensive list alone.

## Comparison on the exact abstained position

| Arm | Answer disposition | Unknown probability |
| --- | --- | --- |
| Saved baseline | Unknown | 35.59% |
| Ask it to keep playing despite defeat | Unknown | 15.52% |
| Supply engine `has_lost: true` | Unknown | 29.51% |
| Engine fact plus keep-playing instruction | Legal G7 | 8.96% |
| Separate `has_lost` question in the same request | Move still unknown | 35.59% |
| Feed its preceding evaluation into the next move request | Legal G7 | 9.24% |
| Add `acknowledge_loss` beside all legal actions | **acknowledge_loss** | **0.38%** |
| Add that option plus engine `has_lost: true` | **acknowledge_loss** | **0.54%** |

The acknowledgement option's raw choice probability was 47.56%, or 44.19%
with the additional engine fact. All legal candidates remained available and
the service's implicit unknown answer was retained. No candidate was applied
to the GUI or saved game. An acknowledgement needs no subsequent board move.

The separate evaluation question incorrectly answered **no** with 94.09% raw
choice probability. The multi-turn arm fed that incorrect answer back as
`has_lost: false`; getting a playable move from it is not evidence of correct
loss recognition. Batched questions are scored independently, so asking an
extra question does not automatically pass its answer into the move question.

The acknowledgement arm adds this candidate:

```json
"acknowledge_loss": "Acknowledge unavoidable White defeat: every legal White action permits an immediate Black win or a verified forced-win setup."
```

Its question prioritizes immediate White wins and available defences, then
asks for acknowledgement when all board actions lose. Unknown remains for
insufficient evidence. This avoids demanding a strongest board move when
every board move loses; it does not establish the model's internal reason
for the original abstention.

## Reproduction and artifacts

With the configured inference service already running:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_loss_acknowledgement
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_loss_acknowledgement \
  --controls --arms loss_option loss_option_with_fact \
  --output logs/boku-loss-acknowledgement-controls.json
```

The script starts/stops no process and applies no moves. It saves exact
requests, raw replies, dispositions, engine ground truth, timings and separate
evaluation accuracy. `passed` measures acknowledgement on lost positions for
the acknowledgement arms, continued legal play for the keep-playing arms,
and a winning/defensive legal move on the surviving controls. It does not
measure the accuracy of the separate evaluation head; that is recorded as
`loss_evaluation_correct`.

The [recorded comparison](fixtures/boku/boku-loss-acknowledgement.json)
preserves the measurements above. The controls cover the forbidden-G8 loss,
a forced capture loss, a double-ended four, the only-C5 defence, an A1 win
and an E5 block. These are development fixtures, not a broad resignation
accuracy benchmark. Any app integration should validate acknowledgement
against the engine's proved loss before accepting it as a resignation.

Both acknowledgement variants passed **all six positions (12/12 requests)**:
they acknowledged the three verified losses and selected C5, A1 and E5 in the
three surviving positions. There were no false acknowledgements in these
controls. Requests used at most 3,568 input tokens, below the 4,096-token limit.
The [recorded controls](fixtures/boku/boku-loss-acknowledgement-controls.json)
preserve both variants' exact requests and replies. Adding `has_lost` to the
state was unnecessary for acknowledgement on this small set.

## Historical integration (not retained)

Commit `af950d7` (`feature/boku`) offered the tested acknowledgement option in
Quoted and Coached quoted, including retries and capture choices. Original
mode is unchanged. The engine validates a non-abstained top acknowledgement
in the background before ending the game. False acknowledgements produce a
decision error and retain the ongoing game. Unknown retains its existing retry
behavior; no probability threshold is weakened.

A valid acknowledgement awards Black a win without changing board stones or
reserves. History records White's `acknowledge_loss` action and a persisted
resignation marker. Replay recomputes the proof; coaching receives the accepted
action, the resignation reason and the exact final board, and seeks the earlier
avoidable mistake. A resumed successful coaching update is not repeated.
Sampling excludes acknowledgement from alternative moves, and an accepted top
acknowledgement is never sampled into a different action.

The integrated suite passed **319 tests**, including false acknowledgements,
abstention, retry, worker-thread validation, reset cancellation, unchanged board
state, resume validation, sampling and loss coaching without duplicate updates.

The live debug app then resumed the originally abstained position and accepted
`acknowledge_loss`, with 47.56% raw choice probability and 0.38% unknown.
The engine confirmed a Black win with unchanged stones and reserves, and the
GUI displayed “Imajev acknowledges defeat. You won!” The
[archived resigned replay](fixtures/boku/retired-resigned-replay.json) preserves the
request, response, terminal decision and accepted history.

The 319-test result and successful live acknowledgement above describe the
discarded integration. They do not establish general resignation accuracy or
describe the retained implementation. Resignation saves from that experiment
are not supported by the retained rules engine; use the pre-acknowledgement
board fixture to replay the position.

## Exact refusal and Retry reproduction

An initial rollback to parent commit `2855fef` was too broad: it restored the
v4 capture-only facts as well as removing acknowledgement. On the same board,
that prompt played G7 directly (7.87% unknown). This was a changed-input test,
not evidence of random move sampling or an exact reproduction of the refusal.

Restoring the v5 broader facts and named defensive actions reproduced the
original request exactly after JSON serialization. On the board after Black
H7 → capture G8, the live results were:

| Request | Top board choice | Abstained | Unknown probability |
| --- | --- | --- | --- |
| Original v5 | G7 | Yes | 35.59% |
| Retry 1 | G7 | No | 11.87% |
| Retry 2 | G7 | No | 7.83% |

The top board choice was G7 throughout; abstention prevented it from being
applied initially. Retry changed the instruction and the model's uncertainty.
It did not fix the forced loss: Black can still win at G8. The board stays
ongoing until a legal move actually ends it. No engine override chooses G7.

The GUI reproduced the refusal and was left waiting for Retry. Separate
requests tested the retries without changing its board. The
[recorded requests and replies](fixtures/boku/boku-refusal-retries.json)
preserve this comparison. Reproduce it against an already running service:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_refusal_retries
```

Move temperature zero disables application move sampling. It is not a promise
of identical scores across model versions, hardware or execution environments;
the changed prompt explains the observed v4/v5 discrepancy here.
