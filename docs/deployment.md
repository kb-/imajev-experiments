# Local service deployment

The GUI and inference environments are separate. The supplied launcher uses the [upstream PyTorch image server](https://github.com/mohit67890/imajev/tree/ccf586d43d2a580319b6535c893668904d909eb9). It adds a local busy gate and provenance endpoint; NF4 base-model loading is supplied by the local wrapper; prompts, adapter, calibration, and HTTP inference handling use the pinned upstream implementation.

For the complete first-time setup and everyday managed startup sequence, see [Run the complete app](../README.md#run-the-complete-app). This guide supplies deployment details and alternate profiles. The standard setup and launch scripts select NF4; `uv run imajev-game` manages a pinned service child by default. The explicit service commands below use `--external-inference` for the GUI.

Pinned assets:

| Asset | Revision |
|---|---|
| Upstream source | `ccf586d43d2a580319b6535c893668904d909eb9` |
| Qwen/Qwen3.5-4B base (default) | `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a` |
| mohit67890/imajev-4b adapter (default) | `f8d8234cebc6c99065c07731e59716dc0a6e27ab` |
| Qwen/Qwen3.5-2B base (optional) | `15852e8c16360a2fea060d615a32b45270f8a8fc` |
| mohit67890/imajev-2b adapter (optional) | `0426f7b1c73804b64fab5802e04f401420ec774c` |
| Calibration/readout | Files from the same pinned adapter snapshot; SHA256 recorded during setup |

## WSL2 / NVIDIA setup

On the target machine, first confirm `nvidia-smi` works inside WSL2. Install Git and uv there. Clone this app into the WSL filesystem, then explicitly prepare dependencies and model files while online:

```sh
bash scripts/setup_inference.sh
```

This installs the locked inference environment under `inference/.venv`, downloads the pinned 4B base model and adapter, installs bitsandbytes 0.50.2, and records `.inference/runtime-manifest-4b-nf4.json`. Weights can consume several GB of disk.

Launch offline:

```sh
bash scripts/launch_inference.sh
```

The launcher checks source revision, all required local model/processor/tokenizer/adapter/calibration files and their hashes, and requires CUDA. Missing or modified assets cause an error before service startup. Hugging Face and Transformers offline modes prohibit gameplay downloads. The service binds to `127.0.0.1:8765`, uses `--backend torch`, the pinned 4B bundle with NF4 quantization, the official 4B adapter and trained 256-code readout, `calibration-rot4.json`, and four option rotations. It does not switch to CPU or another model.

## Default Imajev-4B NF4 profile

The standard setup and launch scripts delegate to `setup_inference_4b_nf4.sh` and `launch_inference_4b_nf4.sh`. The GUI defaults to `imajev-4b-nf4` with a 45-second request timeout and 300-second startup budget. `config.4b-nf4.yaml` remains available as an explicit profile; it is no longer needed to select NF4.

The profile uses CUDA-only bitsandbytes NF4 quantization of the pinned Qwen3.5-4B base at load time. The official Imajev-4B PEFT adapter, trained 256-code decision readout, and four-rotation calibration remain unquantized. It uses `artifacts/model-qwen4b.json` and reports `model: imajev-4b-nf4`. The launcher does not enable upstream `--fast` CUDA graphs or `--merge-lora`.

The user reports that this profile runs successfully, and it is now the default. This operational report does not establish benchmark results or equivalent calibration between the 2B and NF4 profiles. Earlier 2B evaluation records retain their original model identifiers.

## Optional Imajev-2B profile

The original 2B assets and manifest remain separate from NF4. To prepare, launch, and select 2B explicitly:

```sh
bash scripts/setup_inference_2b.sh
bash scripts/launch_inference_2b.sh
uv run imajev-game --external-inference --config config.2b.yaml
```

Run setup once while online, then keep the launcher running in one terminal and start the GUI in another. These scripts use `.inference/runtime-manifest.json`, the pinned 2B base and adapter, and the original calibration. Both profiles bind to port 8765; run one inference service at a time and use the matching GUI configuration.

## GUI setup (Linux or Windows)

The GUI runs separately from the inference service. Keep the service running in its own terminal and keep the endpoint in `config.yaml` at `http://127.0.0.1:8765/v1/systemone`.

### Linux

With a desktop display available, install uv and copy/clone this app. From the app directory, install the locked GUI dependencies and launch the game:

```sh
uv sync --locked
uv run imajev-game --external-inference
```

Use the same app checkout as the inference service when both run on the same Linux machine; the GUI environment is separate from `inference/.venv`. For a GUI running inside WSL2, a working graphical display is also required.

Verify access from the Linux environment running the GUI with:

```sh
curl --fail http://127.0.0.1:8765/v1/models
curl --fail http://127.0.0.1:8765/v1/status
```

### Windows

Run the inference service inside WSL2 and the GUI on Windows. On Windows, install uv, copy/clone this app, and run `uv sync --locked` then `uv run imajev-game --external-inference` from the app directory. Verify access from Windows with:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/v1/models
Invoke-RestMethod http://127.0.0.1:8765/v1/status
```

A valid models response reports `loaded: true`, `backend: torch`, and `model: imajev-4b-nf4`. Status reports `busy` and the local runtime asset manifest. If localhost forwarding does not work, fix WSL localhost forwarding rather than binding the model to a public interface. The GUI performs a real image warm-up before enabling drawing.

## Retry safety and contract check

`/v1/status` plus the server busy gate prevents sending overlapping retries after a client timeout. While the GPU request continues, Retry shows an actionable busy message and does not send another image. The upstream server alone is also supported for ordinary calls; after its request times out, the GUI cannot know whether that server is still busy and requires the guarded launcher for safe retry. Resetting the board invalidates old responses but does not cancel GPU work.

Run `uv run python scripts/evaluate.py contract` in the GUI environment to validate the exact installation with image requests. Session diagnostics include returned calibration fields and, with the supplied launcher, actual runtime revision/hash provenance.

For a clean offline check, prepare both environments and assets, disable external network access, launch the service, and complete a game in the Linux or Windows GUI. Record memory and latency on the actual RTX 3070. None of these hardware checks have been completed in the current environment.
