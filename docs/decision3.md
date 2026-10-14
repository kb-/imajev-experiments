# Decision 3.0 d3-mini Q8

Decision3 d3-mini Q8 is available as a replacement for the Decision2 Nox opponent through the existing `llama_cpp` System One backend. It is integrated and GPU-verified on the RTX 3070 Laptop GPU (8 GB). Its [strategic evaluation](evaluation/decision3-mini-q8.md) still marks it **unsuited for unassisted strategic play in the tested setup**: it improved on Nox's position scores but lost 11 of 12 Tic-tac-toe games against minimax. Integration and playing strength are separate results.

## Install and launch

Run from the repository root under Linux/WSL2 x86_64:

```sh
uv run python scripts/install_decision3.py
uv run python scripts/install_decision3.py --verify
uv run imajev-game --config configs/decision3-mini-4b-q8.yaml --debug-input
```

Close the app before verification. The installer downloads a pinned official llama.cpp CUDA binary bundle, its matching CUDA libraries, d3-mini Q8_0 weights (5.16 GB), and the matching Q8 vision projector (0.37 GB). A CUDA compiler or system toolkit installation is not required. It does not install Linux NVIDIA drivers, run sudo, or alter Python dependencies. WSL uses the existing Windows NVIDIA driver.

Assets are isolated from the working Liquid and Decision2 installations:

- Runtime: `runtimes/decision3/install/llama-server`.
- Weights: `models/decision3-mini-4b/vllm-d3-mini-Q8_0.gguf`.
- Projector: `models/decision3-mini-4b/mmproj-vllm-d3-mini-Q8_0.gguf`.
- Installation manifest: `runtimes/decision3/manifest.json`.

The runtime is official llama.cpp `b11606`, commit `1b70350c92563449576bcbbf552994b1d1ed847d`, using the Ubuntu x64 CUDA 13.4 bundles. The model repository is `ggml-org/vllm-d3-mini-GGUF`, revision `c03ac9c3bdc5ae949111acb945338933f396e4d7`. Downloads are checked against pinned sizes and SHA-256 hashes before atomic publication. The manifest records the runtime's installed file hashes and model identities. Verified installations are reused; conflicting files are rejected. `--download-only` installs weights and projector without a runtime. The original Decision2 fork and profile remain reproducible baselines.

The main profile preserves Liquid drawing recognition, ImaJEV fallback, and the configured Ollama Qwen3.6 coach, while selecting Decision3 as the text opponent. Its Quoted prompt, zero move temperature, and disabled tactical guard match the former Nox profile's experiment settings. Choosing Coached quoted still requires that coach to be available; the Decision3 scorer itself does not generate revised rules.

## Optional shared recognition and opponent

Decision3 is multimodal. To experiment with one resident model for both roles:

```sh
uv run imajev-game --config configs/decision3-mini-4b-shared.yaml --debug-input
```

This profile retains the projector in the runtime even for text moves, so recognition and opponent activation reuse the same resource identity. The recognition threshold of 0.8 is provisional. Synthetic controls passed 14/15: all three clear Tic-tac-toe crosses passed, as did the tested Boku F5 and K5 circles and A2 capture. A valid Boku A1 circle was rejected because its symbol probability was 0.746. All eight invalid controls were rejected. These controls do not establish accuracy on held-out human drawings. ImaJEV remains the fallback after the configured rejection limit.

## GPU verification and limits

`--verify` refuses to run while this checkout's application-owned inference is active. It uses `ServiceManager` to check native model identity, real choice distributions and legal proposals, and runtime health. It exercises Tic-tac-toe requests, an early White Boku position with all 79 legal options, Decision3 → Liquid recognition → Decision3, and shared Decision3 vision → text reuse. It confirms full GPU layer offloading and process cleanup, and records synthetic recognition results separately from integration success.

The checked-in [integration evidence](evaluation/decision3-integration.json) records the measured checks. The full local report, raw responses, sampled VRAM and native runtime output are saved to `logs/decision3-verification.json`. All 33 language-model layers were offloaded; sampled whole-device peak memory was 6951 MiB across the test, including vision and switches. This is an observed sampled device peak, not an exact allocation measurement. Boku's deeper positions and larger images may need more memory than these controls.

Profiles request context, batch and microbatch limits of 8192 because Decision3 evaluates a question's complete input together. Requests that exceed capacity fail explicitly. No prompt truncation, legal-choice filtering, CPU fallback or Q4 substitution is automatic. The tested Boku request passed with 79 choices; this does not establish every request length or the entire configured 255-choice budget.

## Compare strategic decisions

The replay runner uses the archived Decision2 complete-context requests, including symbol definitions, rules, general tactics, board and history. The model receives all legal choices without per-position tactical analysis. Minimax is used only by the evaluator; sampling and guard correction are disabled.

```sh
uv run python scripts/evaluate_decision_context.py \
  --config configs/decision3-mini-4b-q8.yaml \
  --output logs/decision3-full-context-benchmark.json
```

The full run repeats 102 distinct positions in three option orders and 12 games against minimax. For a small pilot, use `--limit 10 --games-per-starter 0`; the limit reduces tested positions, never their legal options. The runner can also use another text-opponent profile. Source prompts are frozen in the checked-in baseline archive, and learning files are not updated.

Official references: [d3-mini model card](https://huggingface.co/vllm-sr/d3-mini), [GGUF files](https://huggingface.co/ggml-org/vllm-d3-mini-GGUF), [native llama.cpp integration](https://github.com/ggml-org/llama.cpp/pull/30383), [pinned runtime release](https://github.com/ggml-org/llama.cpp/releases/tag/b11606).
