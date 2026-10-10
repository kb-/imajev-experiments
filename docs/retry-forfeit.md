# Forfeit after repeated rejected decisions

Every game and prompt mode allows an initial computer decision plus three
retries. If all three retries return abstentions or answers rejected as illegal
moves, Imajev forfeits and the human wins. The GUI shows failed retries out of
three while Retry is available. The third failure ends the session and hides
the move Retry button. Retry remains manual; the app does not loop through
requests automatically.

Timeouts, service outages, malformed responses and decision-preparation errors
do not count and do not reset progress. Startup, drawing recognition and
coaching failures are outside this limit. Any accepted computer action resets
progress, including forced actions and Boku captures. New game and game
switching also reset it. Cancelled, stale and duplicate completions cannot
consume retries.

This is an application rule, not model-driven loss acknowledgement. The board,
reserves, current player and accepted move history remain exactly as they were
before forfeiture. No winning line or synthetic move is added. The session
result is a human win; the underlying rules engine can still report an ongoing
board. New game follows the existing coaching and service-readiness gates.

## Persistence and resume

Session exports retain version 1 and add optional session-level fields:

- `decision_retries`: version 1, current `state_revision`, and `failed` retry count.
- `termination`: kind `forfeit`, reason `decision_retry_limit`, `loser`, `winner`,
  `state_revision`, and `failed_retries` (three).

The controller checks these fields against decision events and the computer
turn when restoring. An inconsistent saved counter or forfeit is rejected.
Older records reconstruct progress from decision events, without requiring
these fields. A legacy refusal loop already at the limit is adjudicated on
restore. A resumed pending turn keeps its progress; the automatic continuation
counts as a retry if it returns another rejected answer. Interrupted requests
without a recorded result do not consume the limit.

Restoring a terminal forfeit does not request a computer move. The ordinary
service readiness/warm-up step still runs. Coached games retain their logical
game ID, preventing a second successful strategy update for the same forfeit.

## Coaching

In Coached quoted mode, a forfeit is a completed computer loss and triggers the
existing diagnosis and strategy revision pipeline. Both games supply the
accepted moves, final board, termination reason and compact evidence of the
rejected decisions. The coach is told explicitly that this is not a board
victory or proof of a forced tactical loss. It must not invent a winning line
or blame an accepted move without supporting evidence; retaining the previous
strategy is appropriate when no tactical lesson is established.

Forfeit metadata survives history summaries, including repeated summary
reduction. The triggering game's final position and rejection evidence remain
exact. Coaching failures keep the existing Retry coaching / Continue with
previous strategy workflow, and neither action changes the completed result.

`tests/test_retry_forfeit.py` covers both games and all modes, counting and
resets, excluded errors, capture/forced phases, GUI controls, replay validation,
legacy restore, stale responses, coaching recovery and summary preservation.
