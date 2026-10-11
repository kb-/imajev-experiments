# Independent recognition and opponents

Repository, application, Python package and CLI names are unchanged. Existing YAML files still select the same ImaJEV recognition and opponent, prompts and tactical policy. ImaJEV recognition retains its probability, abstention and geometry checks.

Select an opponent independently:

```yaml
game:
  default: boku
recognition:
  backend: imajev
  model: imajev-4b-nf4
opponent:
  backend: ollama
  protocol: chat
  model: qwen3:8b
  input_mode: text
  prompt_variant: quoted
  generation_temperature: 0
  move_temperature: 0
  tactical_guard: true
```

Run `uv run imajev-game --config your-config.yaml`. The UI shows both model identities. Selecting a multimodal opponent does not change recognition: configure that role separately. `input_mode: text_image` sends a rendered board as well as the same symbolic state and choices. Text-only opponents receive no image. A local vision runtime also needs `projector_path` pointing to its installed projector.

The `OpponentBackend.choose_move(request, image)` boundary returns `MoveResult`: proposed choice, backend/model identity and original response, with optional distribution, confidence, abstention and unknown probability. Chat models need only return the offered choice. Missing uncertainty fields remain `None`; no probability or abstention values are invented. Games validate legal actions directly. Sampling with `move_temperature > 0` requires a real distribution; chat uses its separate `generation_temperature`. These temperatures have different meanings.

Chat profiles can set `generation_max_tokens` (default 128) to budget reasoning and answer tokens. Truncated responses are rejected, including responses with empty answer content. The installed Qwen3-VL model continued reasoning despite `think: false`; its [profile](../configs/qwen3-vl.yaml) uses 512 tokens, which completed the tested opening position. Non-default budgets are recorded in session metadata.

Only the ImaJEV adapters read the legacy `Reply`. Games and evaluation decision callers consume `MoveResult`; drawing callers consume `RecognitionResult`. Existing saved sessions without role metadata remain readable, and previous ImaJEV role metadata remains compatible. Native recognition sessions record complete role settings and their threshold policy, and reject restoration with a different configuration.

## Native vision recognition

Recognition supports ImaJEV and image-capable native System One models through llama.cpp or Ollama. Vision chat recognition is deferred. Text-only models cannot recognize drawings. The complete recognition question set must fit the runtime's choice limit: Boku's cell question exceeds Ollama System One's 26-choice limit. No cells are removed to fit a model.

The existing Liquid profile keeps ImaJEV recognition. To run Liquid d1 for **both** roles, install its projector and select the shared profile:

```sh
uv run python -m scripts.install_liquid_d1
uv run imajev-game --config configs/liquid-d1-3b-shared.yaml --debug-input
```

The installer downloads only the pinned publisher Q8 projector, verifies its SHA-256, and leaves existing `models/liquid-d1-3b/model.gguf` weights intact. The existing CUDA llama.cpp installation serves both roles with GPU offloading; no additional Python inference package or local compilation is required. Gameplay uses local assets and never downloads them.

`recognition` accepts the native runtime fields supported by `opponent`, with `input_mode: text_image`. Set `recognition.min_choice_probability` for native recognition (default 0.85). ImaJEV continues to use `recognition.min_effective_probability` (default 0.85), including its unknown-probability adjustment. The threshold keys are not interchangeable. Native confidence is reported separately and never substitutes for a missing probability distribution. Missing abstention/unknown fields remain null. Model probabilities are not a measured drawing-accuracy guarantee.

The shared Liquid profile also configures `recognition_fallback` with ImaJEV-4B NF4. After three rejected Liquid recognition attempts at the same board revision, the app automatically retries the pending drawing with ImaJEV using its own `min_effective_probability: 0.65`. The model manager releases Liquid before loading ImaJEV, then releases ImaJEV before the next Liquid opponent move. Each request records the backend and threshold actually used. Undo and Clear reset the rejection count; a saved session preserves it across restart.

Both games retain their symbol, geometry, turn and action-legality checks. Rejected drawings remain editable. Startup warms the image path on a blank observation and validates the response contract without requiring blank ink to be accepted as a move.

To share a runtime, configure both roles with identical backend, service origin, model identity, executable, weights, projector, context and launch arguments. YAML anchors in the shared Liquid profile keep these aligned. Recognition sends the ink image; the opponent uses symbolic state. Changing role, input mode, prompts or threshold does not reload the model. Changing loaded assets or launch settings requires an ordered release and activation. Diagnostics record reuse, process ID, activation time and inference time.

Other native vision models, including d1-omni and Clef, can use the same adapter when the installed runtime advertises image and decision support and their matching projector is configured. Their recognition quality and GPU residency require separate live validation. Clef remains excluded from default 8 GB checks.

With non-ImaJEV recognition, `coached_quoted` requires `learning.coach_backend: ollama` and an installed coach model. The legacy shared coach uses ImaJEV's coaching endpoint. After Ollama coaching finishes and unload is confirmed, the configured recognizer is restored.

The shared Liquid profile does not select a coach. Its selector keeps **Coached quoted** visible but disabled, with a configuration hint. To enable it, add the following to that profile (the existing Ollama coaching profile uses `qwen3.6:latest`; install the selected model separately):

```yaml
learning:
  coach_backend: ollama
  model: qwen3.6:latest
```

The coach generates strategy text after games; it is separate from Liquid's System One decision endpoint. Coaching still requires switching GPU runtimes, while recognition and opponent moves continue to share Liquid.

Run local controls and a shared-turn check with:

```sh
uv run python -m scripts.validate_recognition
```

The report at `logs/liquid-recognition-validation.json` records exact requests, drawings, raw responses, rejections, role settings, asset/runtime provenance, process reuse, accuracy/coverage and sampled device-wide peak memory. Warm-up is excluded from inference latency. Small synthetic controls validate integration; use the recognition evaluation/replay scripts and identical saved human drawings to compare recognition quality. Evaluation and replay now support managed inference by default and `--external-inference` for an existing service. The Boku prompt-variant script retains its explicit external-service behavior.

## Runtimes and profiles

[configs](../configs) contains editable examples for all requested families:

| Profiles | Runtime | Notes |
| --- | --- | --- |
| ImaJEV 2B / 4B NF4 | Existing guarded PyTorch service | Legacy image behavior retained |
| Qwen3 8B | Ollama `/api/chat` | JSON choice with explicit generation temperature |
| Tev1 4B / Nimble 9B / Clef Flash | Ollama `/v1/systemone` | Native decisions, 2–26 options |
| Liquid d1 3B / d1-omni 600M | Current llama.cpp `/v1/systemone` | Text by default; optional installed vision projector |
| Kev 4B / lev 4B | Current llama.cpp | Conservative 52-option profiles |
| Laya / Julia-1 | Current llama.cpp | NVIDIA GGUF runtime; no MLX dependency |
| Decision 2.0 Nox 4B / Sol 2B | Decision 2.0 llama.cpp branch | Use its trained decision-head runtime |
| Mapika Decider 4B | Local `decider-ai` GGUF bridge | Publisher prompt builder and scoring, not chat generation |

Profiles are configuration examples, not performance claims or downloaded models. Local paths resolve relative to the YAML file. Edit model paths, executable paths, context sizes and choice limits to match installed assets and runtime capabilities. Clef Flash is optional and excluded from default 8 GB validation: its default distribution exceeds that GPU budget. It has not been used in the archived experiments.

Install tools and weights explicitly before gameplay. For Ollama, install version 0.35+ and pull the exact tags you intend to test, for example:

```sh
ollama pull qwen3:8b
ollama pull tev1:4b-q4_K_M
ollama pull nimble:9b-q4_K_M
```

Install the pinned official llama.cpp binary bundle with `scripts/install_llama_cpp.sh`. It downloads the prebuilt Ubuntu x64 CUDA 13.4 bundle by default, so no local compiler or CUDA toolkit is needed; use `--backend cpu` for the CPU bundle. The script records the release and backend in `runtimes/llama_cpp/manifest.json`. The profiles point to its installed `llama-server`. Then place the chosen GGUF under `models/<profile>/model.gguf`, or edit the profile. Managed launch uses local paths and `--offline`; it never uses automatic `-hf` downloads. Vision models need their matching local projector. Decision 2.0 profiles expect `runtimes/decision2/llama-server`, built from the [publisher's runtime branch](https://github.com/Xunzhuo/llama.cpp/tree/decision2-gguf), with embeddings and pooling flags specified in the profile.

For Mapika, create the separate environment in `runtimes/decider` using its pyproject (`decider-ai[gguf]==1.9.0`). Install a CUDA-enabled `llama-cpp-python` build there following the publisher's GGUF instructions. Keep the GGUF, `tokenizer.json`, `tokenizer_config.json` and `decider_config.json` together locally. The service runs through that environment's Python with `-m app.inference.decider_service` from the repository root. It hashes those assets, reports runtime versions, rejects oversized complete prompts and serializes GPU requests. The pinned publisher prompt-builder boundary is covered by a bridge contract test; install-time integration with actual weights remains necessary.

## GPU ownership

Managed inference uses one exclusive GPU gate for activation and requests. Matching recognition/opponent runtimes stay resident across turns, with health and model identity verified before reuse. A different runtime or coaching releases the active model first. Switching backends stops owned local process groups or unloads only the selected Ollama model and confirms its disappearance from `/api/ps`. An unconfirmed unload blocks another activation. Occupied ports and preloaded Ollama models are reported; unrelated services are not terminated. Ollama startup includes the existing WSL/Windows relay. Downloads are never initiated by gameplay.

Process creation alone never establishes readiness. Every managed ImaJEV request verifies `/v1/models` reports the expected loaded PyTorch model before checking its busy status and submitting inference. Local llama.cpp and Decider activation waits for `/health` and verifies the model identity from `/v1/models`. Ollama activation explicitly loads chat models using an empty chat request; native decision models use a separate small readiness decision. It then verifies the resident name and installed digest through `/api/ps` before submitting a game request. A failed load, identity mismatch, or failed release blocks the next request. Polling intervals only control how often readiness is checked; elapsed time is never evidence of readiness.

`--external-inference` leaves services under your control and disables managed switching. Arrange GPU residency yourself. Following an HTTP timeout, an external opponent is blocked because disconnecting does not prove GPU work stopped; restart its service and the application before retrying. Managed opponents confirm process exit or unload before allowing a retry.

A forced single legal move bypasses inference. A native runtime's option limit applies to the **complete** offered set. Boku moves are never pruned, grouped or silently sent to another backend. An unsupported position pauses gameplay with an error and is counted as unsupported in comparisons.

## Reproducible comparisons

```sh
uv run python -m scripts.compare_models \
  --config configs/imajev-4b-nf4.yaml \
  --config configs/qwen3-8b.yaml \
  --game tic_tac_toe --limit 32 --seed 42 \
  --output logs/model-comparison --prepare-only
# Remove --prepare-only and add --resume to execute the frozen experiment.
```

`--config` repeats; `--limit 0` includes every source position. Tic-tac-toe uses reachable ongoing O-turn states and an exact minimax reference. Boku uses deduplicated computer-turn states from committed session fixtures and **bounded tactical evidence**, not a perfect-play oracle. Each position has canonical, reversed and seeded shuffled options. Prompt variants, opening suggestions, observation size and tactical guard must agree across configurations. Coached prompts freeze their initial strategy and never read or write learning history.

The runner saves a versioned protocol, source hashes, config snapshots, local weight/projector hashes, exact requests, PNGs and their hashes. Resume verifies these, case/checkpoint integrity, and recorded runtime provenance after a separate warm-up. Use a fresh directory if code, assets or protocol change. Warm-up is excluded from trial scores. Calls retain original proposals/responses before validation or tactical assistance. Summary reports coverage, validity with explicit denominators, errors/refusals, raw and assisted quality, and option-order sensitivity. Forced moves have a separate count.

Managed HTTP runtimes report activation and inference durations separately. Total call latency is always recorded; missing timings remain null. Memory readings include device-wide snapshots and a sampled device-wide peak from `nvidia-smi`, not per-model allocations or a reliable Windows-host RAM measurement through WSL. Measure host RAM and peak VRAM externally for hardware claims. Ollama provenance includes installed model digest, runtime version and capabilities; local runtimes record command, model hash and readiness identity. Archived evaluation files are unchanged.

Existing decision scripts use the opponent factory. Recognition evaluation, replay and validation scripts use the recognition factory and record real scores and their score policy. Older research scripts that explicitly use external services still expect you to start those services; `evaluate_prompting` now accepts `--config` and manages the chosen opponent. Its experiment protocol version changed to prevent mixing old decoder results into resumed trials.

Protocol references: [Ollama System One](https://docs.ollama.com/api/systemone), [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), [Mapika Decider](https://github.com/Mapika/decider), [Decision 2.0 GGUF](https://huggingface.co/vllm-sr/Decision-2.0-Nox-4B-GGUF).
