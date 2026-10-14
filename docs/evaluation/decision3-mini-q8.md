# Decision3 d3-mini Q8: improved scores, unsuited for unassisted strategy

**Status: unsuited for unassisted strategic play in the tested setup.** On 14 October 2026, Decision3 d3-mini Q8 improved on Decision2 Nox Q8's complete-context Tic-tac-toe position scores, but still lost 11 of 12 full games against minimax. Its GPU integration passed; that does not establish strategic reliability.

## Comparable setup

The model was `ggml-org/vllm-d3-mini-GGUF`, revision `c03ac9c3bdc5ae949111acb945338933f396e4d7`, Q8_0 SHA-256 `214d44cc351f084d8cc26dc85ce68af5a3c2e878c86c52e2be6c22312793f93d`. It ran through official llama.cpp `b11606` (`1b70350c92563449576bcbbf552994b1d1ed847d`) on the RTX 3070 Laptop GPU (8 GB), Linux/WSL2. Context/batch/microbatch were 8192, with all language-model layers on GPU. Input was text; the primary profile's Liquid recognizer and Ollama coach were not used by the benchmark.

The [replay runner](../../scripts/evaluate_decision_context.py) reconstructed all 306 inputs from the [Decision2 archive](decision2-nox-q8-results.json). It retained the complete definitions of X, O, empty cells, coordinates, turn order, all winning lines, legal placement, wins, draws, objectives and tactical terms. It supplied general tactical priorities, the board, history and every legal action. No per-position tactical hints, oracle scores, corrections, coaching updates or sampled choices entered the model's decisions. Native highest-probability proposals were used directly.

The same 102 distinct board positions and canonical, reversed and seeded shuffled choices were used. The sample is stratified by depth and minimax outcome, with four positions retained from the original game; it is not a population accuracy estimate. Three decisions on one board are not three independent positions.

## Results

| Measure | Decision2 Nox Q8 | Decision3 d3-mini Q8 |
| --- | ---: | ---: |
| Optimal choices, canonical order | 63 / 102 (61.8%) | 76 / 102 (74.5%) |
| Optimal choices, all orders | 197 / 306 (64.4%) | 223 / 306 (72.9%) |
| Avoidable losing choices where win/draw remained available | 97 / 216 (44.9%) | 70 / 216 (32.4%) |
| Missed immediate wins | 28 / 57 (49.1%) | 13 / 57 (22.8%) |
| Failed immediate defenses, evaluator definition | 54 | 49 |
| Positions whose chosen action changed with option order | 44 / 102 | 18 / 102 |
| Full games: wins / draws / losses | 0 / 1 / 11 | 0 / 1 / 11 |

An optimal choice preserves the best achievable final outcome under perfect subsequent play. Already forced-loss positions can count every losing move as optimal, which inflates this measure relative to strategic competence. Missing an immediate win is counted separately, even if a slower forced win remains. A failed defense requires an immediate X threat, no immediate O win, and a move with worse minimax outcome than the best move. Different choices across orders can be equally good.

Decision3 controlled O in all full games. Minimax controlled X, choosing randomly among equal optimal actions using seeds 0–5, with each side starting six times. X-start games produced six O losses. O-start games produced five losses and one draw. A sole legal action bypassed inference. Perfect play can draw from either initial position; these losses therefore demonstrate mistakes in the tested games. Some games share openings, so the 12 outcomes are not an independent win-rate estimate.

Decision3's lower error counts are an improvement on this sample. Remaining missed wins, failed defenses and full-game losses prevent recommending it as a reliable unassisted strategic opponent. This result applies to this Q8 model, runtime, fixed prompt and sample; it does not assess other Decision3 sizes, Boku strategy, coached rules or every possible prompt.

## Evidence and reproduction

The [results archive](decision3-mini-q8-results.json) stores the frozen prompt template, dynamic positions and histories, exact choice orders, returned probabilities and token counts, minimax scores, game traces and model/runtime provenance. Reconstruct requests using the same archive layout described in the [Decision2 evaluation](decision2-nox-q8.md#evidence). The baseline identity is included for comparison.

```sh
uv run python scripts/evaluate_decision_context.py \
  --config configs/decision3-mini-4b-q8.yaml \
  --output logs/decision3-full-context-benchmark.json
```

The benchmark requires installed assets and exclusive managed inference. It uses the same frozen rules across profiles and does not update learning history. See [installation and GPU/vision validation](../decision3.md) for the separate integration and recognition results.
