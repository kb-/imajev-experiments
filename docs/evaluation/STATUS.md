# Acceptance status

Implementation and automated application checks are complete. Broad handwriting and hardware feasibility are **not established**; a live synthetic recognition pilot is now available. Do not approve model/hardware acceptance until the following target-machine evidence exists.

## Verified here

- Immutable rules: all eight winning lines, full-board draw, win-before-draw, occupied-cell rejection, turn order, terminal rejection and serialization invariants across all 5,478 reachable boards.
- Recognition protocol: two questions, unknown-adjusted confidence, X/O correction, invalid/uncertain rejection and independent length-weighted cell geometry.
- Controller: reset during inference, duplicate Submit/Retry, failed recognition retaining ink, failed computer request retaining the accepted human turn, stale results, and a second test-only game with generic state/action/player types.
- Canvas: multiple strokes, normalized coordinates after resize, undo/clear, boundary clipping, capture loss and observations independent of window size.
- Qt: offscreen desktop window startup, warm-up lifecycle and screenshot rendering with an explicit test fake.
- Reproducibility: locked GUI and separate inference environments; source/base/adapter pinned; offline launcher records and verifies asset hashes.

## Blocked target-machine evidence

The initial implementation environment had no running model service and blocked sandboxed NVIDIA access. A separately prepared local 2B service is now running, and live recognition calls have been verified. Cold-start benchmarking, Windows display scaling, Windows-to-WSL access, a complete offline game, peak memory and held-out human handwriting accuracy remain unmeasured. Unit tests and the synthetic pilot do not establish these properties.

## Run the pilot

1. Follow `docs/deployment.md` to prepare the default 4B NF4 image service on the RTX 3070 (or explicitly select the legacy 2B profile).
2. Run `uv run python scripts/evaluate.py --output contract.json contract`. This sends recognition and decision PNGs and validates typed answers against the installation.
3. Gather at least 200 labelled drawings from multiple people, including neat/messy X/O, small/uneven marks, blank, scribble, multiple symbols and cell crossings. Store strokes in the schema shown in `corpus.example.json`. Record `participant`, `split`, `valid_x`, and `cell` for each sample. Keep `pilot` and `heldout` sets separate; do not tune against heldout answers. The example corpus is illustrative, not evaluation evidence.
4. Run `uv run python scripts/evaluate.py --output handwriting.json recognition corpus.json --split heldout`. Report accepted-move precision, correct valid-X coverage and invalid false acceptance separately. Review the proposed ≥99% precision and ≥90% clear-X coverage goals. A report with insufficient samples/participants/splits is explicitly flagged.
5. Run `uv run python scripts/evaluate.py --output opponent.json opponent` for all reachable O-turn states; use `--limit 20` only for a pilot. The oracle measures optimal-action agreement and missed immediate wins/necessary blocks. Failures and abstentions are counted separately. No oracle choices enter gameplay.
6. Record cold-start time, warmed recognition/decision p50/p95, peak VRAM and process RAM on the service host. Observe `nvidia-smi` during image warm-up and throughout games; a weight file's size is not peak VRAM. Measure input-to-paint latency on Windows and test display scaling.
7. Complete a game with external networking disabled. Kill the service, simulate malformed answers and an OOM, and verify recovery with the retained board/ink. Reset during a running job and retry after timeout; the guarded launcher reports busy until that GPU job has ended.

If the handwriting pilot misses the goals, revise rendering, prompts, calibration or model selection explicitly and repeat with a fresh heldout set. Do not add an automatic geometric symbol classifier or minimax fallback.

## Recognition rejection debug, 3 October 2026

The supplied screenshot showed a clear X in B2 being rejected after a successful model call. Reconstructing its visible shape reproduced the failure: the v1 request returned X with effective probability 0.5651 and B2 with 0.8917. The unchanged 0.85 threshold correctly rejected the weak symbol score, but the original generic message hid that cause.

Prompt v2 uses a short handwriting question and describes each cell's position. On the same synthetic B2 cross, X reached 0.9617 and B2 reached 0.9601. The final live pilot passed 18/18 expected acceptance/rejection decisions: ten valid X cases (nine cells plus a small X), three circles, blank, one diagonal, scribble, two X marks in different cells, and a boundary-crossing X. See `recognition-pilot.json` for exact prompts and answer distributions. These cases were used during prompt development and are not a held-out accuracy measurement. No threshold, geometry gate, model, calibration or image rendering was changed.

The app now exposes symbol/cell confidence and abstention in rejection messages, logs model answers/rejection reasons, and saves JSON plus exact PNG observations when `--debug-input` is enabled. `scripts/replay_recognition.py` can compare the saved image against original/current prompts without modifying a live game.

## Opponent tactics, 3 October 2026

A saved O turn had X at B2 and C3 and O at C2. The model's v2 request included the complete symbolic board and a brief objective, but it chose B3 (one recorded game) and C1 (another). Both missed the immediate diagonal block at A1. Three broader prompt variants also missed A1 in a live probe. A direct instruction with A1 named selected it, which motivated explicit tactical context in prompt v3.

Gameplay now offers every legal action to Imajev and records its proposed action. The enabled `opponent.tactical_guard` enforces an immediate O win or the single available cell that blocks an immediate X win, visibly recording any correction. This one-turn guard is intentionally distinct from the evaluation-only minimax oracle. It cannot prevent a loss caused by a prior fork or multiple simultaneous threats. With the guard disabled, model choices are committed as before.

The guard was checked over all 2,097 reachable ongoing O-turn boards and their 7,536 legal action proposals. Every selected correction remained legal and took an immediate O win or unique X block where present. A live request on the recorded B2/C3 position returned A1 under the new prompt. Full model skill and optimal-move agreement remain unmeasured; `scripts/evaluate.py opponent` reports raw proposals and optionally guarded moves with `--with-tactical-guard`.

## Repeated abstention debug, 3 October 2026

A saved game with X at A1 showed four identical O requests and four identical abstentions. The top conditional move was A3 at 17.3%, but the unknown option had more raw mass than A3, so Imajev abstained. The UI message incorrectly called A3 illegal. The updated opening question explicitly asks O to choose the center B2. The local model returned B2 without abstaining at 93.3% on the saved board; revised retry questions returned B2 at 88.1% and 81.4%. The error message now distinguishes abstention from an invalid ID, and Retry changes the request. Session restore permits continuing the exact paused board in a new window. These measurements apply to this board and do not establish abstention rates across other positions.

## Default model update, 3 October 2026

The user reports successful operation of Imajev-4B NF4 and requested its promotion to the default. The default GUI configuration and standard setup/launch scripts now select this profile. The earlier recognition pilot and debug measurements above remain 2B results; no equivalent NF4 accuracy, calibration, or hardware benchmark is claimed. The explicit 2B configuration and launch scripts remain available.
