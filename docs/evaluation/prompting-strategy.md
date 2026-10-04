# Context and output strategy evaluation

This experiment tests the non-thinking proposals in `imajev_prompting_strategy.md` on
the pinned Qwen3.5-4B NF4 installation. It does not use native thinking, coaching,
retained learning, retries, or tactical corrections for raw decisions. The model,
adapter, trained 256-code readout, calibration, four option rotations, and 768-pixel
observations remain fixed. Minimax supplies scoring and the perfect X opponent only.

## Prompt variants

| Variant | Context and output |
| --- | --- |
| `legacy` | Current gameplay request, including explicit tactical and opening recommendations |
| `raw` | Exact empty-strategy learning request from `feature/strategy-learning` |
| `context` | Raw plus `you`, `opponent`, `board_rows` with `.` empties, and coordinate matrix |
| `strategy` | Raw plus eight ordered strategy priorities |
| `ids` | Raw with bare cell IDs instead of `place_<cell>` |
| `consequences` | Raw with structured per-candidate position and immediate tactical facts |
| `A` | Changed context, rules, legal cells, and bare IDs |
| `B` | A plus eight ordered strategy priorities |
| `C` | B plus structured position, win, block, and immediate-loss facts |
| `expanded` | C plus fork creation/prevention, opponent fork exposure, opposite corner, and winning-cell counts |
| `text` | Expanded with descriptions flattened to equivalent plain text |
| `quoted` | The supplied concrete JSON shape, exact instruction, five priorities, and four structured candidate fields |

The quoted arm uses a matrix under `board`, `player_to_move`, and `opponent`. Other
row-oriented arms retain the canonical dictionary and add `board_rows`. The fixture
`. . . / . X O / . . X` checks every candidate fact from the supplied example and
measures whether each arm selects A1. No experimental arm includes opening advice,
named recommended actions, minimax values, or optimal-action labels.

The pinned API flattens structured descriptions to semicolon-separated text.
`expanded` and `text` therefore produce identical model-visible option descriptions;
their repeated calls check that the wire representation has no separate advantage.

## Tactical facts

Facts use bounded legal simulations, never minimax. A block eliminates **all**
immediately winning X replies; occupying one of several threatened cells does not
qualify. Winning or terminal O moves have no subsequent opponent replies. Forks
count distinct next-turn winning cells, rather than winning lines that share a cell.

`creates_fork` means two or more O winning cells after its move; immediate X exposure
is recorded separately. An opponent fork is dangerous only when its creation does
not allow O an immediate win. Fork prevention means a dangerous X fork existed
before the candidate move and none exists afterward. This includes moves that
force X to defend. Counts describe opportunities on the after-O board, not a claim
that those opportunities survive every X response.

## Sampling, selection, and confirmation

The frozen diagnostic set contains the 21 prior decomposition positions, the quoted
fixture if absent, and 64 fresh boards. Historical drawing strokes are preserved.
The 256-board holdout is disjoint. Fresh sampling is round-robin across both starters
and win, block, fork creation, fork prevention, quiet, and forced-loss strata.
One-option boards are excluded from position samples; forced moves in games bypass
inference and are separately marked.

All twelve arms play all nine X openings plus three O-start games. Select one
candidate by accepted-optimal decisions, wins, blocks, abstentions, nonloss games,
game aborts, and median latency, in that order. Freeze the candidate before holdout
calls. Compare it with both baselines on holdout boards and on reversed/shuffled
option orders for 64 holdout boards. Confirmation games use fresh fixed X tie
orders. Assisted games use the same confirmation setup with the existing tactical
guard and retain raw proposals and corrections.

Abstentions count as failed accepted decisions and abort games; ranked-optimal
accuracy remains separate. Forced-loss accuracy is separately recorded because
preserving an unavoidable loss is not evidence of safe play.

Adoption requires a positive lower bound for the paired-bootstrap 95% interval
of accepted-optimal improvement over current gameplay (10,000 resamples), no
regression in wins, blocks, abstentions, confirmation game outcomes, or aggregate
option-order accuracy, no illegal replies, agreement at least as high as the raw
baseline, and median latency at most twice current gameplay. Failure leaves the
gameplay default unchanged. This is sampled validation, not an exhaustive benchmark.

## Run and resume

Run from the repository root with the prepared inference environment and a free
port 8765:

```bash
uv run --offline python -m scripts.evaluate_prompting --prepare-only
uv run --offline python -m scripts.evaluate_prompting --resume
```

Alternatively, omit both flags for a fresh complete trial. Use `--output` for a new
artifact directory; existing artifacts are never silently overwritten. The default
source is `logs/decomposition-experiment/results.json`; `--previous` selects another
compatible prior-position file.

The harness owns its offline service and stops only that child process on completion
or failure. An occupied port is reported without attaching to its owner. Resume
verifies code, configuration, runtime manifest, prior dataset, frozen dataset,
candidate selection, and exact request/image hashes, including candidate order.
Already recorded calls are replayed from the checkpoint without inference.

Artifacts include `dataset.json`, `protocol.json`, `selection.json`, `calls.jsonl`,
exact observation PNGs, game histories, resource readings, `summary.json`, and
`report.md`. GPU readings are sampled whole-device usage, not per-process allocations.
Protected hashes verify that existing NF4 configuration edits, exported game files,
and learning JSON records remain unchanged. Raw artifacts stay local under `logs/`.
The completed comparison is recorded in [prompting-strategy-results.md](prompting-strategy-results.md).
