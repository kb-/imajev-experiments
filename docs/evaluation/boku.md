# Boku validation

Validated on 2026-10-10 with the pinned `imajev-4b-nf4` service on an NVIDIA GeForce RTX 3070 Laptop GPU. GUI and rules tests use fake inference; live probes below use the actual quantized model. Synthetic ink is not a measurement of human handwriting accuracy or Boku playing strength.

## Automated checks

The complete suite passes 272 tests, including Boku gameplay, tactical facts and game-separation/coaching cases. Coverage includes the 80-space geometry, three winning axes and overlines, captures in all directions, double sandwiches, prohibition expiry, reserve accounting, result ordering, replay validation, pending-capture resume, ink rejection, conversion to stones, computer capture sequencing, drawing through the actual canvas, resizing, disabled input during inference, game switching and coaching isolation. Offscreen GUI smoke checks pass for both games.

## Recognition comparison

All probes retained the 0.85 effective-probability threshold and independent location validation. The location question retained all 80 spaces plus invalid; framing never selected a move for the model.

| Recognition view/prompt | Circle at F5 | Capture X at A2 |
| --- | --- | --- |
| Full shaded board, labels below pockets | Read E5 instead; rejected | Not attempted |
| Full shaded board, labels inside pockets | Accepted F5 | Invalid location; rejected |
| Magnified shaded pockets | Symbol confidence too low; rejected | Not attempted |
| Magnified plain grid, initial verbose prompt | Symbol/location confidence below threshold; rejected | Not attempted |
| Magnified plain grid, short visual prompt (selected) | Accepted | Accepted |

The selected prompt asks “What is the handwritten symbol?” and “Read the ID of the cell containing the handwritten symbol.” Context only describes the image. Board legality remains in the rules engine, and decision requests still include the full authoritative position and rules. Recognition renders all pending ink in a magnified plain hexagonal grid; IDs sit above the writing area, committed stones are omitted, and display/decision views retain realistic stones.

In the comparison run, the selected prompt produced:

| Sample | Symbol effective probability | Location effective probability | Request latency |
| --- | --- | --- | --- |
| Circle at F5 | 93.97% | 99.80% | 7.22 s |
| X at A2 | 99.02% | 98.84% | 7.16 s |

These are two deterministic synthetic examples, not an accuracy estimate. The production threshold remains 85%; uncertain answers preserve editable ink rather than committing an inferred fallback move. Runtime latency varies with system load.

## Reproduction

```sh
QT_QPA_PLATFORM=offscreen uv run --locked --offline python -m scripts.validate_boku
```

The probe starts an owned service, warms it, recognizes a center circle and a corner circle with White replies, loads a valid capture position, recognizes its X, requests White's next move, exercises a separate White capture, and verifies replay. It writes answers, timings and failures to `/tmp/imajev-boku-validation.json` and shuts down only its owned service. `--external-inference` uses an explicitly separately managed service.

## Final live turn sequence

The final integrated probe passed with no abstentions or rejected moves. State export/replay checks passed, and the app-owned service exited afterward.

| Step | Accepted action | Latency |
| --- | --- | --- |
| recognition | `place_F5` | 34.99 s |
| decision | `place_E4` | 42.75 s |
| recognition | `place_K5` | 35.06 s |
| decision | `place_D3` | 42.33 s |
| recognition | `capture_A2` | 34.72 s |
| decision | `place_A5` | 42.74 s |

Recognition scores matched the earlier comparison at F5 and A2; the additional K5 circle scored 95.54% for symbol and 98.02% for location. Requests in this run took 34.7–42.8 seconds, substantially slower than the comparison run. The Boku launch profile therefore provides a 90-second request timeout. The Qt GUI remains responsive while requests run; an accepted human move waits for the computer reply before drawing resumes.

A separate live White-capture request also passed: from the legal sequence Black A2 → White A1 → Black A3 → White K1 → Black K2 → White A4, Imajev selected `capture_A2` without abstaining. The model request took 25.76 seconds (68.10 seconds including service launch/readiness). It returned control to Black with A2 forbidden, replay validation passed, and its owned service exited.


## Coaching refactor checks

Both GUI smoke checks pass with game-specific prompt choices and Move variety. Automated tests cover Boku rules and geometry in all coaching stages, accepted capture replay, strategy snapshots, loss-only triggers, per-game ledger isolation, legacy saves, responsive failure recovery and complete history reduction for a long reserve-exhaustion game. HTTP tests verify protocol 2 exchanges prepared chat messages and returns raw text for application-side validation.

The new live coaching probe is `python -m scripts.validate_boku_coaching`. Its replayed legal loss is explicitly scripted; it does not count as an Imajev playing-strength result. At the refactor checkpoint, live validation is pending because the open Boku GUI owns the previous service on port 8765. No competing model was loaded and the active application was left running.

A CPU-only check with the installed Qwen tokenizer and `enable_thinking=False` measured the replayed-loss fixture at 1,403 diagnosis tokens, 1,371 summary tokens and 1,416 revision tokens. The complete Boku rules and 80-cell geometry are included in each count. This verifies context packing, not model generation. The shared input ceiling is now 4,096 tokens.

## Human drawing threshold adjustment

The last debug run rejected two correctly recognized E3 circles at effective symbol scores 0.6922 and 0.7115, despite location scores 0.9899 and 0.9905. Replaying the four saved recognition requests at threshold 0.65 accepts those circles and still rejects the abstained angular loop and the X drawn during placement. Visual inspection during the subsequent experiments established that the angular loop was a valid drawing, despite the model rejecting it. `config.boku.yaml` now uses 0.65; the earlier synthetic probes above used 0.85. Independent geometry, symbol/phase, abstention and legality checks still apply. Four requests are insufficient to estimate general recognition accuracy.


## Follow-up drawing experiments

[Saved handwriting and negative-control experiments](boku-drawing.md) identify both symbol wording and obscured labels as causes. The selected structural prompt and protected recognition labels accept all eight saved human loops at the unchanged 0.65 threshold. Fresh handwriting validation remains necessary, and the report records how the same-pocket multiple-loop false accept was found and addressed.


## Boku quoted candidate facts

The `boku-v2-tactical-facts` prompt adds game-owned, capture-aware immediate
win/block/danger evidence in Quoted and Coached quoted modes. Original remains
unchanged. False flags are declared once and omitted per candidate to fit the
4,096-token service ceiling. The model still selects among all legal actions;
there is no tactical correction after its answer.

A live replay of the latest missed-block position used:
Black E3 → White D2 → Black E2 → White D1 → Black E1 → White D3 → Black D4 →
White D5 → Black E4. Previously Imajev chose D6, allowing Black E5 and a win.
With the facts it selected E5 (raw probability 0.9149, unknown 0.000623), the
only action preventing an immediate Black win. This is a regression fixture,
not a general playing-strength estimate.

| Live fixture | Action | Input tokens | Total latency | Fact calculation |
| --- | --- | --- | --- | --- |
| Recorded missed E5 block | `place_E5` | 3,416 | 10.14 s | 0.275 s |
| Opening with 79 candidates | `place_E4` | 2,705 | 7.95 s | 0.005 s |
| Mandatory White capture | `capture_A3` | 2,303 | 4.77 s | <0.001 s |

The existing NF4 service used four presentation rotations. None of these
probes abstained. Evidence is saved in `logs/boku-tactical-facts.json`;
reproduce with `QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.validate_boku_tactics`
while an explicit external service is running. Probes do not alter the GUI game.

Tests cover the actual losing sequence, identical retry evidence, unchanged
state and legal choices, safe versus unsafe mandatory capture branches, wins
resolved after capture and before exhausted-reserve draws, forbidden-space
handling, Black winning replies requiring capture, and unchanged Original mode.
All 272 tests and the Boku offscreen GUI smoke check passed. The running GUI
needs a restart to load the new prompt builder.
