# Local service deployment

The GUI and inference environments are separate. The supplied launcher uses the [upstream PyTorch image server](https://github.com/mohit67890/imajev/tree/ccf586d43d2a580319b6535c893668904d909eb9). It adds a local busy gate and provenance endpoint; model loading, prompts, calibration and inference remain upstream.

Pinned assets:

| Asset | Revision |
|---|---|
| Upstream source | `ccf586d43d2a580319b6535c893668904d909eb9` |
| Qwen/Qwen3.5-2B base | `15852e8c16360a2fea060d615a32b45270f8a8fc` |
| mohit67890/imajev-2b adapter | `0426f7b1c73804b64fab5802e04f401420ec774c` |
| Calibration/readout | Files from the same pinned adapter snapshot; SHA256 recorded during setup |

## WSL2 / NVIDIA setup

On the target machine, first confirm `nvidia-smi` works inside WSL2. Install Git and uv there. Clone this app into the WSL filesystem, then explicitly prepare dependencies and model files while online:

```sh
bash scripts/setup_inference.sh
```

This installs the locked inference environment under `inference/.venv`, downloads the pinned base model and adapter, and records `.inference/runtime-manifest.json`. Weights can consume several GB of disk. Setup does not establish whether the complete image pipeline fits 8 GB VRAM.

Launch offline:

```sh
bash scripts/launch_inference.sh
```

The launcher checks source revision, all required local model/processor/tokenizer/adapter/calibration files and their hashes, and requires CUDA. Missing or modified assets cause an error before service startup. Hugging Face and Transformers offline modes prohibit gameplay downloads. The service binds to `127.0.0.1:8765`, uses `--backend torch`, the pinned 2B bundle, the pinned adapter and its calibration, and four option rotations. It does not switch to CPU or another model.

## GUI setup (Linux or Windows)

The GUI runs separately from the inference service. Keep the service running in its own terminal and keep the endpoint in `config.yaml` at `http://127.0.0.1:8765/v1/systemone`.

### Linux

With a desktop display available, install uv and copy/clone this app. From the app directory, install the locked GUI dependencies and launch the game:

```sh
uv sync --locked
uv run imajev-game
```

Use the same app checkout as the inference service when both run on the same Linux machine; the GUI environment is separate from `inference/.venv`. For a GUI running inside WSL2, a working graphical display is also required.

Verify access from the Linux environment running the GUI with:

```sh
curl --fail http://127.0.0.1:8765/v1/models
curl --fail http://127.0.0.1:8765/v1/status
```

### Windows

Run the inference service inside WSL2 and the GUI on Windows. On Windows, install uv, copy/clone this app, and run `uv sync --locked` then `uv run imajev-game` from the app directory. Verify access from Windows with:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/v1/models
Invoke-RestMethod http://127.0.0.1:8765/v1/status
```

A valid models response reports `loaded: true`, `backend: torch`, and `model: imajev-2b`. Status reports `busy` and the local runtime asset manifest. If localhost forwarding does not work, fix WSL localhost forwarding rather than binding the model to a public interface. The GUI performs a real image warm-up before enabling drawing.

## Retry safety and contract check

`/v1/status` plus the server busy gate prevents sending overlapping retries after a client timeout. While the GPU request continues, Retry shows an actionable busy message and does not send another image. The upstream server alone is also supported for ordinary calls; after its request times out, the GUI cannot know whether that server is still busy and requires the guarded launcher for safe retry. Resetting the board invalidates old responses but does not cancel GPU work.

Run `uv run python scripts/evaluate.py contract` in the GUI environment to validate the exact installation with image requests. Session diagnostics include returned calibration fields and, with the supplied launcher, actual runtime revision/hash provenance.

For a clean offline check, prepare both environments and assets, disable external network access, launch the service, and complete a game in the Linux or Windows GUI. Record memory and latency on the actual RTX 3070. None of these hardware checks have been completed in the current environment.
