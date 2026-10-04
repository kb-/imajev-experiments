# Context and output prompting experiment

Live trial completed on 4 October 2026 on `feature/prompting-strategy-evaluation`.
The gameplay default remains unchanged. See the [protocol and variant definitions](prompting-strategy.md).

Candidate consequences are useful, but the highest board-score variant is a worse
game opponent than the existing prompt. On fresh holdout boards, expanded C improves
optimal-move agreement from 77.3% to 85.2%, while missing 20 immediate wins and 11
necessary blocks. Its confirmation games produce two draws and ten losses, versus
eight draws and four losses for the current prompt. The existing tactical guard
raises the candidate to five draws, still below the current prompt.

The quoted matrix-board prompt is the most promising simpler alternative on the
diagnostic sample: it matches the current prompt at 71/86 optimal moves, takes all
16 immediate wins and all 18 required blocks, and produces ten draws in twelve
games. It supplies no named recommended move. It was not selected for holdout
testing, so these diagnostic results do not establish an independently validated
replacement. Context, bare IDs, and strategy prose alone do not improve overall
diagnostic agreement over the unassisted baseline.

Candidate: **text**. Adoption gate: **failed**.

Pinned Qwen3.5-4B NF4, ImaJEV adapter and 256-code readout, existing calibration, four rotations, no thinking. No coaching, retained strategy, retries or tactical corrections in position comparisons. Local consequences are supplied only in the designated arms; minimax is used only for scoring and perfect X.

## Position comparisons

| Phase | Arm | Accepted optimal | Ranked optimal | Missed wins | Missed blocks | Abstentions | p50 / p95 seconds |
| --- | --- | --- | --- | --- | --- | --- | --- |
| diagnostic | legacy | 71/86 | 71/86 | 0/16 | 0/18 | 0 | 2.98 / 3.19 |
| diagnostic | raw | 59/86 | 59/86 | 6/16 | 11/18 | 0 | 2.83 / 3.03 |
| diagnostic | context | 59/86 | 59/86 | 5/16 | 11/18 | 0 | 3.00 / 3.20 |
| diagnostic | strategy | 54/86 | 54/86 | 6/16 | 13/18 | 0 | 3.01 / 3.22 |
| diagnostic | ids | 55/86 | 55/86 | 8/16 | 12/18 | 0 | 2.83 / 3.02 |
| diagnostic | consequences | 67/86 | 67/86 | 2/16 | 2/18 | 0 | 3.05 / 3.24 |
| diagnostic | A | 58/86 | 58/86 | 6/16 | 12/18 | 0 | 2.99 / 3.21 |
| diagnostic | B | 53/86 | 53/86 | 7/16 | 13/18 | 0 | 3.10 / 3.32 |
| diagnostic | C | 68/86 | 68/86 | 5/16 | 0/18 | 0 | 3.37 / 3.63 |
| diagnostic | expanded | 80/86 | 80/86 | 7/16 | 1/18 | 0 | 3.77 / 4.15 |
| diagnostic | text | 80/86 | 80/86 | 7/16 | 1/18 | 0 | 3.74 / 4.16 |
| diagnostic | quoted | 71/86 | 71/86 | 0/16 | 0/18 | 0 | 3.01 / 3.22 |
| holdout | legacy | 198/256 | 198/256 | 0/42 | 0/43 | 0 | 3.01 / 3.20 |
| holdout | raw | 169/256 | 169/256 | 15/42 | 26/43 | 0 | 2.85 / 3.01 |
| holdout | text | 218/256 | 218/256 | 20/42 | 11/43 | 0 | 3.72 / 4.25 |
| robustness | legacy | 87/128 | 87/128 | 0/20 | 0/24 | 0 | 2.98 / 3.13 |
| robustness | raw | 68/128 | 68/128 | 8/20 | 18/24 | 0 | 2.82 / 2.93 |
| robustness | text | 101/128 | 101/128 | 10/20 | 5/24 | 0 | 3.71 / 4.18 |

## Games against perfect X

| Phase | Arm | Draws | O wins | Losses | Aborts |
| --- | --- | --- | --- | --- | --- |
| diagnostic | legacy | 11 | 0 | 1 | 0 |
| diagnostic | raw | 0 | 0 | 9 | 3 |
| diagnostic | context | 0 | 0 | 9 | 3 |
| diagnostic | strategy | 0 | 0 | 12 | 0 |
| diagnostic | ids | 0 | 0 | 9 | 3 |
| diagnostic | consequences | 7 | 0 | 5 | 0 |
| diagnostic | A | 0 | 0 | 9 | 3 |
| diagnostic | B | 1 | 0 | 11 | 0 |
| diagnostic | C | 5 | 0 | 7 | 0 |
| diagnostic | expanded | 5 | 0 | 7 | 0 |
| diagnostic | text | 5 | 0 | 7 | 0 |
| diagnostic | quoted | 10 | 0 | 2 | 0 |
| confirmation | legacy | 8 | 0 | 4 | 0 |
| confirmation | raw | 0 | 0 | 9 | 3 |
| confirmation | text | 2 | 0 | 10 | 0 |
| assisted | legacy | 8 | 0 | 4 | 0 |
| assisted | raw | 2 | 0 | 7 | 3 |
| assisted | text | 5 | 0 | 7 | 0 |

## Quoted example

Board: `. . . / . X O / . . X`. A1 is the required diagonal block.

This board is already a forced loss against perfect X, even after A1. Minimax
agreement alone therefore counts every legal move as optimal; the block metric
is what distinguishes successful handling of the quoted example.

| Arm | Ranked choice | Abstained | Block taken |
| --- | --- | --- | --- |
| legacy | place_A1 | False | True |
| raw | place_B3 | False | False |
| context | place_B3 | False | False |
| strategy | place_A3 | False | False |
| ids | B3 | False | False |
| consequences | place_A1 | False | True |
| A | B3 | False | False |
| B | C1 | False | False |
| C | A1 | False | True |
| expanded | A1 | False | True |
| text | A1 | False | True |
| quoted | A1 | False | True |

## Adoption decision

Holdout accepted-optimal difference versus current gameplay: **+7.8 percentage
points**; paired-bootstrap 95% interval **[+2.0, +13.7] points** (10,000 resamples).
This passes the improvement check, but immediate wins, required blocks, and
confirmation-game outcomes regress. No gameplay integration or default change was made.

- paired_improvement: pass.
- not_below_raw_baseline: pass.
- no_win_regression: fail.
- no_block_regression: fail.
- no_abstention_regression: pass.
- no_illegal_replies: pass.
- no_game_regression: fail.
- no_order_regression: pass.
- latency_within_2x: pass.

## Protocol and limitations

Diagnostic positions: 86; holdout: 256. Both starters are represented. Historical probes retain their recorded drawings. The candidate was frozen before holdout calls. Each arm plays all nine X openings and three O-start games; confirmation uses fresh fixed tie orders. Order robustness uses 64 holdout boards with reversed and seeded shuffled options.

Abstentions count as failed accepted decisions and abort raw games. Forced one-option turns bypass inference. Forced-loss boards are reported separately in summary.json; optimality on them does not mean a safe move. The sample is stratified, not a natural game distribution or exhaustive model validation. Diagnostic prompt selection uses no holdout results.

Facts are bounded tactical simulations, not a minimax answer. A fork creates at least two distinct next-turn winning cells. Opponent fork prevention includes forcing replies: forks allowing an immediate O win are not dangerous. Immediate-win and terminal states do not claim subsequent opponent replies. Structured and text descriptions flatten to identical strings in the pinned API; their separate calls serve as a representation control.

Measured requests: 2838. Peak sampled whole-device GPU use: 5258 MiB. Protected local-file hashes preserved: True.

The trial took 8,920.6 seconds (2 hours 28 minutes), including service startup and
shutdown. The owned service released port 8765; whole-device GPU use afterward was
117 MiB. No model weights, adapters, calibration, configuration, learning records,
or exported games were changed.

All three finalists are optimal on all 47 forced-loss holdout boards. Excluding
those boards gives expanded C 171/209 (81.8%), current gameplay 151/209 (72.2%),
and raw 122/209 (58.4%). These remain agreement measurements, not full-game success.

Verification: 92 automated tests and the offscreen Qt smoke check passed. An
independent replay audit checked all 2,838 requests, replies, scores, and served
image hashes against their reconstructed states, plus all 216 game histories and
perfect X replies. There were 473 distinct observation PNGs, 22 forced moves, and
19 assisted tactical corrections. All requests used one verified runtime and
omitted thinking. Code hashes and protected-file hashes remained unchanged.

## Reproduction

```bash
uv run --offline python -m scripts.evaluate_prompting
uv run --offline python -m scripts.evaluate_prompting --resume
```

Raw artifacts: `/home/olivier/imajev-experiments/logs/prompting-strategy-experiment`. The directory contains the frozen dataset, protocol, selection checkpoint, calls.jsonl, exact PNGs, game histories, summary, resource readings and owned-service log. Resume validates code, configuration, runtime assets, dataset and request/image hashes. Fresh trials require a fresh output directory.
