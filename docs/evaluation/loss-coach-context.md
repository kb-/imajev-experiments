# Loss coach context experiments

Local tests on 2026-10-05 used the completed coached game `a1fd0e15-37e6-4eac-ab6f-413d6420c52a` and its three preceding draws. Experiments did not update the gameplay ledger. Complete requests, responses and decision replays are saved under the local ignored `logs/coach-rules-*` directories.

The accepted sequence was O B2, X C3, O B3, X B1, O A3, X C1, O C2, X A1. O's A3 allowed X C1 to create two winning threats, A1 and C2. The last O move was already in a forced loss. Scoring confirmed that other legal alternatives to A3 could draw. These solver results were used for evaluation only; the coach received accepted moves and factual board reconstructions.

## Method

Compare basic rules against coach-generated rules on 12 fixed positions: the avoidable mistake from this game and the first 11 positions in `data/evaluation/prompting-strategy-positions.json`. A position passes when the accepted Imajev action preserves the best achievable result according to the evaluation solver. An abstention fails. This small development set includes the triggering game and is not an independent strength benchmark.

Each arm also plays one O-start game against a perfect scripted X opponent, using a fixed tie order. These games stop on the first abstention; they do not exercise the GUI's Retry workflow. A basic-rules opening abstention therefore leaves its game incomplete rather than proving a loss.

Coaching uses greedy output, thinking disabled, and a 512-token output limit. Windows Ollama tests used an 8,192-token context and conservative 7,500-byte message packing, with older games summarized when necessary. The shared NF4 base uses a 3,072-token input limit. The analysis and minimal-change arms sometimes required history summarization, so their differences include context reduction as well as the prompt wording.

## Results

| Coach and context | Positions passed | Scripted game | Coaching seconds |
|---|---:|---|---:|
| Basic two-rule strategy | 8/12 | Opening abstention | — |
| Shared Qwen4B, compact history | 10/12 | Human win | 16.7 |
| Shared Qwen4B, reconstructed loss boards | 10/12 | Opening abstention | 8.5 |
| Shared Qwen4B, diagnosis then rules | 9/12 | Human win | 18.7 |
| Ollama qwen3.5:latest, compact history | 5/12 | Human win | 8.8 |
| Ollama qwen3.5:latest, reconstructed loss boards | 8/12 | Draw | 9.3 |
| Ollama qwen3.5:latest, diagnosis then rules | 7/12 | Draw | 27.7 |
| Ollama qwen3.5:latest, lines and minimal revision request | 7/12 | Draw | 17.9 |
| Ollama qwen3.6:latest, lines, diagnosis, minimal revision request | 8/12 | Draw | 135.4 |

Every arm still chose A3 when replaying the original avoidable mistake. Draws came from different earlier moves, including choosing A1 instead of B3 on O's second turn. One draw per arm does not establish improved strength.

## Diagnosis and interpretation

Qwen4B and Qwen3.5 made factual errors in their loss explanations, including nonexistent lines and treating the final forced-loss turn as preventable. Explicit boards alone did not eliminate these mistakes. Qwen3.5 also ignored the request to preserve unaffected rules and make at most one change.

The installed Windows alias `qwen3.6:latest` is a 36B Q4_K_M `qwen35moe` model, about 23 GB. During the test Ollama reported 68% CPU / 32% GPU on the 8 GB RTX 3070 Laptop GPU. It correctly identified A3, the subsequent C1 fork, and the A1/C2 threats. Its explanation still contained inaccurate intermediate claims and one incorrect rendered board, so it should not be treated as fully reliable.

Its resulting strategy was:

1. win immediately
2. otherwise stop X winning next turn
3. otherwise prevent X from creating a fork on their next turn

That is a relevant general lesson in the intended priority order. Imajev nevertheless repeated A3 on the recorded position. A better loss diagnosis is therefore insufficient by itself: the decision model must recognize and apply the lesson.

Production context now labels each result from O's perspective and supplies the triggering loss's accepted boards, legal cells, next X moves and winning lines. It does not supply solver decisions or automatically insert a fork rule. At the time of these first experiments, the separate diagnosis call and one-change restriction were experimental. The later update described below enables diagnosis before revision; the one-change restriction remains experimental. `config.coached-quoted.ollama.yaml` selects Qwen3.6 for further local play.

After the experiments, the debug GUI resumed the saved terminal loss and successfully performed normal Qwen3.6 coaching. The update and service recovery took 105.8 seconds. Revision 1 covered all four completed games; the resumed game's original revision-0 snapshot was preserved. This normal request returned six rules with generic center/corner advice and a redundant block rule, rather than the focused fork-prevention lesson above. It also said to create a fork "instead of blocking," despite retaining block as the higher-priority rule. This confirms the distinction between a successful lifecycle and a useful strategy: the separate diagnosis experiment produced a better lesson than the normal one-pass update. No semantic quality filter or manual correction was applied to the saved strategy.

The lifecycle worked: stop owned Imajev, coach through Windows Ollama, unload the selected model, confirm `/api/ps` is empty, restart and warm up Imajev, then replay. `ollama ps` only shows the temporary coaching model; it does not show the Python Imajev service. The Windows daemon is left running.

## Opposite-corner loss and diagnosis display

After revision 1, the user won with X A1, O B2, X C3, O A3, X C1, O C2, X B1. The normal update produced revision 2 but retained a misleading opposite-corner rule. A later symmetric loss, `dc5f4832-1bbb-4694-99a2-9bf4e8e54ddd`, followed X A1, O B2, X C3, O C1, X A3, O B3, X A2. Revision 3 still recommended taking an opposite corner when X held two corners.

Re-reviewing the second loss used its original two-game history window and prior revision-2 rules. No test committed a new strategy revision. Qwen3.6 ran with `think: false` in the API; current prompts contain no thinking-control wording.

| Test | Diagnosis | Suggested rule quality |
|---|---|---|
| Four labeled lines | Blamed final B3 block; invented C2 defense and a D2 coordinate | Retained the bad corner preference |
| Explicit move numbers and boards after X replies | Correctly identified move 4 C1 and a safe B1 alternative; invented X A2 reply and an X row containing O | Vague "block their potential line intersection" |
| Additional factual winning-threat cells | Correctly identified C1, X A3 and threats A2/B3; A2 is a safe alternative, but its claimed immediate blocking rationale was inaccurate | Retained the bad corner preference |

The third test annotated before/after threat cells from the existing game-rule functions. It supplied no suggested alternative, optimal action or strategy rule. These annotations remain an optional experiment in `scripts.evaluate_loss_diagnosis.py` (`--threat-facts`), and are not injected into gameplay decision prompts or normal coach context.

The normal coaching flow now requests diagnosis before revision, records both calls, and presents the explanation under **COACH DIAGNOSIS** in inline Diagnostics while revision continues. A failed or truncated diagnosis retains the previous strategy. Explanations survive revision failures and resume. Status labels follow the actual coaching stage. Visibility does not prove diagnosis correctness; these tests show that even factual context does not guarantee a useful learned rule.

The first re-review completed both generations but hit a two-second Windows-relay readiness timeout during reload. Startup checks now allow ten seconds for PowerShell transport, and a slow existing listener is not treated as permission to start a duplicate daemon.

## Reproduce

Close the app and its owned service first. With the same local history and models installed:

```bash
QT_QPA_PLATFORM=offscreen uv run --offline python -m scripts.evaluate_coach_rules \
  --backend ollama --model qwen3.6:latest --diagnose-first \
  --game-id a1fd0e15-37e6-4eac-ab6f-413d6420c52a \
  --output logs/new-qwen36-comparison
```

Omit `--diagnose-first` for the three main context variants, or use `--minimal-only` for the minimal revision arm. Output directories must be new. Current prompt code includes the later clarification about O's result and checking supplied winning lines, so exact responses may differ from these recorded runs.
