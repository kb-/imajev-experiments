#!/usr/bin/env bash
# Default profile: Imajev-4B NF4. Use setup_inference_2b.sh for the legacy 2B profile.
set -euo pipefail
script_root="$(cd "$(dirname "$0")" && pwd)"
exec bash "$script_root/setup_inference_4b_nf4.sh" "$@"
