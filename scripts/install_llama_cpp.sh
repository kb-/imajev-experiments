#!/usr/bin/env bash
set -euo pipefail

# Pinned official llama.cpp CUDA bundle. It includes llama-server and its
# matching ggml/CUDA libraries, so a local CUDA toolkit/compiler is not needed.
readonly RELEASE='b11552'
readonly CUDA_ASSET="llama-${RELEASE}-bin-ubuntu-cuda-13.4-x64.tar.gz"
readonly CPU_ASSET="llama-${RELEASE}-bin-ubuntu-x64.tar.gz"
readonly CUDA_LIBS_ASSET="cudart-llama-${RELEASE}-bin-ubuntu-cuda-13.4-x64.tar.gz"
readonly CUDA_SHA256='cd3397bc9bed31776d6c9b0a59437b476e563f611740ba6eabcbcb3319ae6c9d'
readonly CUDA_LIBS_SHA256='2da7d7611d8086ab0fdcc8c362b400202a9cc714e59b0293b91bf24d8d3086bf'
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly RUNTIME="$ROOT/runtimes/llama_cpp"
readonly PREFIX="$RUNTIME/install"
readonly STAGE="$RUNTIME/install-staging"

backend='cuda'
usage() {
    cat <<'EOF'
Install the pinned official llama.cpp prebuilt runtime.

Usage: scripts/install_llama_cpp.sh [--backend cuda|cpu]

  cuda  Download the Ubuntu x64 CUDA 13.4 bundle (default; no compiler needed).
  cpu   Download the Ubuntu x64 CPU-only bundle.
EOF
}
while (($#)); do
    case "$1" in
        --backend)
            (($# >= 2)) || { usage >&2; exit 2; }
            backend="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ "$backend" == cuda || "$backend" == cpu ]] || { usage >&2; exit 2; }
command -v curl >/dev/null || { echo 'curl is required.' >&2; exit 1; }
command -v tar >/dev/null || { echo 'tar is required.' >&2; exit 1; }

mkdir -p "$RUNTIME"
rm -rf "$STAGE"
mkdir -p "$STAGE"
if [[ "$backend" == cuda ]]; then
    asset="$CUDA_ASSET"
else
    asset="$CPU_ASSET"
fi
archive="$RUNTIME/$asset"
url="https://github.com/ggml-org/llama.cpp/releases/download/$RELEASE/$asset"
curl --fail --location --retry 3 "$url" --output "$archive"
if [[ "$backend" == cuda ]]; then
    actual_sha256="$(sha256sum "$archive" | cut -d ' ' -f 1)"
    [[ "$actual_sha256" == "$CUDA_SHA256" ]] || {
        echo "CUDA bundle checksum mismatch: $actual_sha256" >&2
        exit 1
    }
    libs_archive="$RUNTIME/$CUDA_LIBS_ASSET"
    curl --fail --location --retry 3 \
        "https://github.com/ggml-org/llama.cpp/releases/download/$RELEASE/$CUDA_LIBS_ASSET" \
        --output "$libs_archive"
    actual_sha256="$(sha256sum "$libs_archive" | cut -d ' ' -f 1)"
    [[ "$actual_sha256" == "$CUDA_LIBS_SHA256" ]] || {
        echo "CUDA libraries checksum mismatch: $actual_sha256" >&2
        exit 1
    }
    tar -xzf "$libs_archive" --directory "$STAGE" --strip-components=1
fi
tar -xzf "$archive" --directory "$STAGE" --strip-components=1
[[ -x "$STAGE/llama-server" ]] || { echo 'Bundle does not contain llama-server.' >&2; exit 1; }
"$STAGE/llama-server" --version
rm -rf "$PREFIX.previous"
if [[ -d "$PREFIX" ]]; then mv "$PREFIX" "$PREFIX.previous"; fi
mv "$STAGE" "$PREFIX"
rm -rf "$PREFIX.previous"
rm -f "$archive" "${libs_archive:-}"
cat > "$RUNTIME/manifest.json" <<EOF
{
  "repository": "https://github.com/ggml-org/llama.cpp",
  "release": "$RELEASE",
  "asset": "$asset",
  "backend": "$backend",
  "binary": "$PREFIX/llama-server"
}
EOF
printf '\nInstalled llama-server (%s) at %s\n' "$backend" "$PREFIX/llama-server"
