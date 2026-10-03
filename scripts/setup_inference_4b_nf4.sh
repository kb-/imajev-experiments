#!/usr/bin/env bash
# Explicit online setup for Imajev-4B with runtime NF4 quantization. Gameplay is offline.
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
runtime_root="$project_root/.inference"
upstream_root="$runtime_root/upstream"
upstream_commit=ccf586d43d2a580319b6535c893668904d909eb9
adapter_revision=f8d8234cebc6c99065c07731e59716dc0a6e27ab
bitsandbytes_version=0.50.2

mkdir -p "$runtime_root"
if [ ! -d "$upstream_root/.git" ]; then
    git clone https://github.com/mohit67890/imajev "$upstream_root"
fi
git -C "$upstream_root" checkout --detach "$upstream_commit"

uv sync --project "$project_root/inference" --locked
uv pip install --python "$project_root/inference/.venv/bin/python" "bitsandbytes==$bitsandbytes_version"

cd "$upstream_root"
uv run --project "$project_root/inference" --no-sync python scripts/download_model.py --model 4b
uv run --project "$project_root/inference" --no-sync hf download mohit67890/imajev-4b \
    --revision "$adapter_revision" --local-dir adapters/imajev-4b

uv run --project "$project_root/inference" --no-sync python "$project_root/scripts/runtime_assets.py" \
    --upstream "$upstream_root" --model 4b-nf4 --record

printf 'Imajev-4B NF4 assets prepared. Launch with: bash %s/scripts/launch_inference_4b_nf4.sh\n' "$project_root"
