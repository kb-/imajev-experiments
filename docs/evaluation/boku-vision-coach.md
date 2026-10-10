# Boku loss diagnosis with rendered board images

Tested on 2026-10-10 with Ollama `qwen3.8:latest` (Qwen3.8 27.3B Q4_K_M,
vision capability reported by Ollama), using completed game
`d6950a35-60f2-462d-9b44-2482ec8c6059`. The experiment did not update the
strategy ledger or apply any move.

## Method

The request used the app's Boku diagnosis prompt, accepted action history, game
rules, axial geometry and previous strategy. It attached application-rendered,
cell-labeled board images after actions 39, 42, 43 and 44. Those positions show
the forced capture sequence and terminal board. Qwen received the full accepted
history as well as the images; this was a multimodal request, not an image-only
ablation, so the correct winner cannot be attributed to visual input alone.

The first request used the original PNGs. Their combined base64 request exceeded
the Windows Ollama relay's 1 MB body limit and was rejected with HTTP 502 before
inference. The loopback relay limit is now 8 MiB, still bounded. The successful
retry used the same four rendered positions as 768-pixel JPEGs (quality 78),
with a combined base64 image payload of 349,076 bytes. No model download was
performed.

Ollama reported 5,516 prompt tokens and 133 generated tokens. Model generation
took about 104 seconds; total time including model loading and the managed
Imajev/Ollama handoff was 107 seconds. The coach call used the configured
non-thinking API option, temperature 0 and 512-token output ceiling.

## Result

Qwen correctly named Black as the winner, but identified the winning line as
F5–G5–H5–I5–J5. The actual five-stone line was D3–E4–F5–G5–H5. It blamed White's
action 40 (`place_G5`) and suggested blocking H5 or I5 after Black's H4. This
diagnosis did not correctly track the mandatory Black capture at action 42 or
offer a rules-checked alternative. A bounded tactical check had already found
that, after action 39, all 53 legal White replies permitted a forced-loss setup
within its horizon; this check does not prove the complete game-theoretic result,
but it does not support claiming that action 40 was an avoidable mistake.

Earlier text-only Qwen3.8 coaching on this game also misread the terminal
position. The image-assisted response got the winner right, but its exact line,
turn sequence and proposed remedy remained wrong. Since the successful request
also included text history, it is unclear whether the winner identification came
from the images or the text. The experiment shows that Qwen accepts the rendered
boards and that board images alone do not make its diagnosis dependable.

The exact successful request metadata and response are in the local, ignored
attempt record `learning/coached-quoted/boku/attempts/229e5b46bfb64bf5b59a2babc1269236.json`.
The failed original-size request is recorded in
`learning/coached-quoted/boku/attempts/5fcd89c1e34d432f85067b612a5a0ad3.json`.
Those local records are not committed with this report. The focused relay suite
passed 24 tests after the 8 MiB limit change.
