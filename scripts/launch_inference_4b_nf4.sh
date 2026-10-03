#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
upstream_root="$project_root/.inference/upstream"

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_DATASETS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export IMAJEV_QUANTIZATION=nf4
export IMAJEV_RUNTIME_MANIFEST=runtime-manifest-4b-nf4.json

uv run --project "$project_root/inference" --no-sync --offline python "$project_root/scripts/runtime_assets.py" \
    --upstream "$upstream_root" --model 4b-nf4

cd "$upstream_root"
export PYTHONPATH="$upstream_root/src:$upstream_root/scripts"
exec uv run --project "$project_root/inference" --no-sync --offline python "$project_root/scripts/serve_local.py" \
    --backend torch --model-bundle artifacts/model-qwen4b.json --adapter adapters/imajev-4b \
    --calibration adapters/imajev-4b/calibration-rot4.json --model-name imajev-4b-nf4 \
    --readout-codes 256 --rotations 4 --host 127.0.0.1 --port 8765
