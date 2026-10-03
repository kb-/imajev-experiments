#!/usr/bin/env bash
# Explicit online setup. Gameplay and the launch script are offline.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
runtime_root="$project_root/.inference"
upstream_root="$runtime_root/upstream"
upstream_commit=ccf586d43d2a580319b6535c893668904d909eb9
adapter_revision=0426f7b1c73804b64fab5802e04f401420ec774c
mkdir -p "$runtime_root"
if [ ! -d "$upstream_root/.git" ]; then
    git clone https://github.com/mohit67890/imajev "$upstream_root"
fi
git -C "$upstream_root" checkout --detach "$upstream_commit"
uv sync --project "$project_root/inference" --locked
cd "$upstream_root"
uv run --project "$project_root/inference" --no-sync python scripts/download_model.py --model 2b
uv run --project "$project_root/inference" --no-sync hf download mohit67890/imajev-2b --revision "$adapter_revision" --local-dir adapters/imajev-2b
uv run --project "$project_root/inference" --no-sync python "$project_root/scripts/runtime_assets.py" --upstream "$upstream_root" --record
printf 'Inference assets prepared. Launch with: bash %s/scripts/launch_inference.sh\n' "$project_root"
