# Strategy learning acceptance — 4 October 2026

Implemented on `feature/strategy-learning`. The existing NF4 configuration edits and exported games were preserved.

The live command is repeatable after preparing assets and freeing port 8765:

```sh
QT_QPA_PLATFORM=offscreen uv run python -m scripts.verify_learning
```

It starts and shuts down an owned service using the pinned NF4 launcher. Two synthetic, legal human-winning move sequences trigger coaching; a following game uses real Imajev O decisions and scripted symbolic X moves. Results and durable revision/request artifacts are saved under `logs/learning-validation/`. This checks the learning lifecycle and adapter restoration; it does not measure handwriting recognition or playing-strength improvements.

| Live check | Result |
|---|---|
| Model | Installed Qwen3.5-4B NF4 with official Imajev adapter and trained readout |
| GPU | NVIDIA GeForce RTX 3070 Laptop GPU, 8 GiB |
| First successful revision | 9.24 s |
| Second successful revision | 9.27 s; previous strategy and both completed losses included |
| Following real decision game | 9 accepted moves, O win, 0 retries |
| Peak sampled total GPU memory | 4,711 MiB, sampled every 200 ms |
| Ownership | Managed launch, readiness, warmup and shutdown completed |
| Ollama live | Not run: executable/model unavailable |

Two preliminary requests asking for up to 300 words hit the 512-token output limit. Both were rejected and left revision 0 intact. The final prompt requests six short rules within 120 words, retaining the same 512-token generation ceiling. No backend fallback occurred. The two successful revisions returned identical strategy text; persistence and revision advancement succeeded, but stronger or different lessons are not guaranteed. One earlier successful cycle took 8.38 s and also completed a following real decision game.

Automated coverage includes loss/draw triggers, forced endings, no coaching after wins/abandonment/rejected moves, stable IDs on restore, duplicate prevention, learning-only compact history, context bounds, revision retention and revision changes, initial/retry strategy injection, adapter restoration after exceptions and interruption, both endpoint busy gates, port conflict reporting, external mode, child exit, owned shutdown, mocked Ollama stop/coaching/unload/reload, restart blocking until unload confirmation, and inline Retry/Continue controls. Qt offscreen startup/render smoke verification also passed.

Playing strength remains experimental. The generated rules contain imprecise tactical advice; a single win against scripted first-legal X moves is not evidence of improvement.
