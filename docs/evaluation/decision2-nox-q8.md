# Decision2 Nox Q8: unsuited for unassisted strategic play

**Status: unsuited for unassisted strategic game play in the tested setup.** On 12 October 2026, Decision 2.0 Nox-4B Q8_0 frequently missed immediate wins and necessary blocks in Tic-tac-toe despite receiving complete rules, general tactics, and the full position. Keep its profile for research and reproducibility; it is not a recommended strategic opponent.

This finding applies to Nox Q8 with the prompt and runtime below. It does not establish the performance of Nox Q4, other Decision 2.0 sizes, Boku, or every possible prompt. A successful installation or `install_decision2.py --verify` establishes integration, not playing strength.

## Setup and context

The experiment ran on the RTX 3070 Laptop GPU (8 GB), under WSL2/Linux, using the publisher's Q8_0 GGUF and pinned Decision 2.0 llama.cpp fork:

- Model revision: `b179d65af24ff9e28924c8d5c080f8c5bf5b2b07`.
- Model SHA-256: `047979ef73ffd92f2619cc4f4ede1e5996b42dd808d97b5cdc01aa75e0130390`.
- Runtime commit: `3c64a581d4f95bb2de409d383741fa5b18d84345`, with the installer's recorded output-modality metadata fix.
- Context 2048, batch/microbatch 1024, GPU layers 99, embeddings, pooling `none`, one slot, offline operation.
- Highest-probability legal proposal used directly. Move temperature zero; no tactical corrections or coaching updates.

One fixed prompt defined both players and their marks: O is the model's circle, X is the opponent's cross, and `.` is an empty cell. It explained all nine cell coordinates, board orientation, turn order, placement legality, all eight winning lines, terminal wins and draws, the preference for win over draw over loss, and the absence of captures, passes, and hidden information. It defined threats, blocks, forks, center, corners, and sides before giving general tactical priorities.

Each request supplied the complete board, starting player, current player, move history, and every legal placement. Choice descriptions stated only where O would be placed. No per-position winning actions, threat lists, per-action quality labels, oracle scores, or targeted recommendations were sent to the model. Minimax was used only by the evaluator. Input sizes were 851–958 tokens; these requests completed without truncation or capacity errors. This experimental prompt did not replace the application's production prompt modes.

## Position benchmark

The test selected reachable O-to-move states, stratified by move count and minimax outcome with seed `20261012`, and retained the four positions from the motivating game. There were 102 distinct board positions, each tested with canonical, reversed, and seeded shuffled choice order. These are 306 decisions on a stratified sample, not 306 independent positions or a population accuracy estimate. A shuffle can coincide with another order.

| Measure | Result |
| --- | ---: |
| Optimal choices, canonical order | 63 / 102 (61.8%) |
| Optimal choices, all three orders | 197 / 306 (64.4%) |
| Avoidable losing choices where a win or draw remained available | 97 / 216 (44.9%) |
| Missed immediate wins | 28 / 57 (49.1%) |
| Failed immediate defenses, as recorded by the evaluator | 54 |
| Positions whose chosen action changed with option order | 44 / 102 (43.1%) |

An optimal action preserves the best achievable final outcome under perfect subsequent play. In an already forced-loss position, every losing action can therefore count as optimal. Missing an immediate win is recorded separately, even if a slower forced win remains possible. The failed-defense count requires an immediate X threat, no immediate O win, and a selected action with worse minimax outcome than the best action. Different choices across orders can be equally good; order sensitivity alone is not counted as a tactical error.

In both missed-block positions from the motivating game, C1 was the only action preserving a draw. The full-context prompt still chose C2 in the first position and A3 in the second, under all three tested orders.

## Full games

Decision2 always controlled O against a perfect minimax X opponent. Twelve games used six seeds for choosing between equally optimal X actions, with each player starting six times. O used the same complete-context prompt and canonical choices. A sole legal action bypassed inference; there was no tactical correction or oracle fallback for other O moves.

| Starter | O wins | Draws | O losses |
| --- | ---: | ---: | ---: |
| X | 0 | 0 | 6 |
| O | 0 | 1 | 5 |
| Total | 0 | 1 | 11 |

Perfect Tic-tac-toe play can draw from the empty board with either starter. These losses establish failures in the tested games, rather than merely a failure to beat a perfect opponent. Games can share openings and are not independent estimates of a general win rate.

## Why the earlier targeted result is insufficient

A preceding four-position experiment produced 6/12 optimal decisions with Quoted prompts, 6/12 after adding explicit rules, and 12/12 with rules plus concrete per-action consequences. The last variant explicitly told the model which reply would complete a winning line. It tested use of supplied tactical analysis, rather than deriving that analysis from rules and the board. It does not overturn this broader unassisted result.

Enabling a tactical guard can prevent some immediate mistakes, but then the outcome measures an assisted system. It must not be reported as evidence that Nox itself is a sound strategic player.

## Evidence

The checked-in [results archive](decision2-nox-q8-results.json) preserves the fixed prompt template, dynamic positions and histories, choice order, returned probabilities and token counts, minimax scores, per-decision measurements, and all full-game traces. Reconstruct each request by copying `request_template`, adding the referenced `request_positions` entry to its state, and filling its criteria in `option_order` with `choice_description_template`. Runtime/model identity is stored once under `runtime_provenance`.

The original full local report and experiment script remain at `logs/decision2-full-context-benchmark.json` and `logs/decision2-full-context-benchmark.py`. Logs are local artifacts; the checked-in archive does not depend on retaining them.
