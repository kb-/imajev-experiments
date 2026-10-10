# Capture-trap fact experiment

Initially tested after commit `df1f43e` on 2026-10-10 outside the
application prompt builder. Subsequently integrated into Boku quoted modes
for the requested replay; the strategy ledger remains unchanged.

## Question and scope

At action revision 25 of session
`015e9e8c-b396-44c2-b431-fc059e97bb85`, White chose D5. Black could then place
B3, capture D3, and win at D3 on its following turn. White could not replace
the captured blocker because D3 was forbidden during White's intervening turn.
Existing immediate-win facts did not see this setup.

The proposed flag is `allows_Black_capture_forced_win`. It is true when every
completion of White's candidate action allows Black to choose a placement and
mandatory capture, after which **every complete White reply** still permits
Black to win on its following turn. White wins, draws and safe capture branches
refute a claimed trap. All actions use the authoritative Boku rules engine.
This checks capture setups only, not all possible longer-term forced wins.

## Rules-engine results

The experiment checked all 63 legal White candidates in 14.90 seconds in one
run, and 14.09 seconds in the run used for model probes. Sixty candidates allow
a verified capture trap; three have no capture trap within the checked scope:

| Candidate | Flag | Defensive continuation |
| --- | --- | --- |
| D5, the actual mistake | true | Black B3 → capture D3 leaves 61 White replies, all losing to Black D3 |
| B3 | false | Occupies the setup pocket before Black can use it |
| C4 | false | After Black B3 → capture D3, White F4 → capture E4 breaks the winning line |
| F4 | false | Symmetric counter-capture: White C4 → capture E4 |

The script records a concrete Black placement/capture witness, the forbidden
pocket and the number of complete White replies checked for each flagged
candidate. Independent regression assertions replay the witness and verify
Black D3 wins after every reply. Counter-capture and immediate White-win controls
are included in five passing tests.

## Model probes

Use the existing `imajev-4b-nf4` service with four presentation rotations and
zero app move temperature. Keep the same board and candidate ordering. Append
the new sparse flag and its definition to the existing quoted candidate facts;
do not select or override the action.

| Prompt | Selected | Raw probability | D5 probability | Input tokens | Inference latency |
| --- | --- | --- | --- | --- | --- |
| Actual game, immediate facts only | D5 | 37.04% | 37.04% | recorded in source session | 7.98 s |
| New capture-trap fact, original two-rule strategy | C4 | 57.64% | 13.51% | 3,476 | 10.18 s |
| New fact plus “otherwise avoid allowing Black to force a win through a capture” | C4 | 63.27% | 8.23% | 3,489 | 9.67 s |

Both experimental requests fit the current 4,096-token input ceiling and neither
abstained. The original strategy alone sufficed with the new evidence on this
fixture; the extra strategy priority strengthened the preference for C4. This
does not establish general playing strength.

A live follow-through probe then played White C4 → Black B3 → Black capture
D3. Using the existing application immediate facts, Imajev chose **F4**, then
**capture E4**. The rules engine confirmed no immediate Black winning reply
remained. This verifies that the model can realize the defensive continuation
on this fixture, rather than merely selecting a candidate labeled safe.
Requests, replies and timings are in `logs/boku-capture-traps-followup.json`.

## Reproduction and artifacts

```sh
.venv/bin/python -m scripts.evaluate_boku_capture_traps \
  --actions place_D5 place_B3 --output logs/boku-capture-traps-screen.json

QT_QPA_PLATFORM=offscreen .venv/bin/python -m scripts.evaluate_boku_capture_traps \
  --probe-model --output logs/boku-capture-traps-model.json

QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

The model probe requires an explicitly running service. It starts/stops no
processes and does not modify saved games. Saved source sessions and JSON/log
artifacts remain local. Full rule results are in
`logs/boku-capture-traps-all.json`; prompt/result pairs are in
`logs/boku-capture-traps-model.json`.

## Integration implications

The initial experimental suite passed 277 tests. The integrated
`boku-v3-capture-traps` prompt enables the fact in Quoted and Coached quoted.
Boku request preparation runs in the existing serialized worker, displaying
“Checking move consequences…” while calculating facts. It captures immutable
game/config/strategy inputs; reset or shutdown discards the stale preparation
before HTTP inference. The prepared request is persisted on the GUI thread.
Already-winning or immediately-unsafe completions skip deeper evaluation;
Black positions with fewer than three stones cannot form the checked trap.
The deeper flag is omitted for those skipped branches, as defined in the prompt.
Additional tests cover background execution, reset cancellation before
inference, prepared-request recording, and the integrated losing-position
flags. Computation still takes about 14 seconds on the replay fixture.
Further optimization can reduce repeated reply evaluation. Further fixtures
should exercise other captures and distinguish absence of a checked capture
trap from general safety.

The integrated suite passes **280 tests**, and both offscreen GUI smoke checks
pass. The replay starts at revision 25 before White's D5 decision.

The integrated debug replay in session `2fce6e24-d68e-407d-b817-cdd50e448937` selected
**C4** instead of D5 (raw probability 50.28%, unknown 2.47%). The recorded
request marks D5 with `allows_Black_capture_forced_win: true` and leaves C4
unflagged. End-to-end preparation/inference took 25.05 seconds;
the service processed 3,466 input tokens. The GUI then returned
control to Black at revision 26.
