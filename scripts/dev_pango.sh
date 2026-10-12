#!/usr/bin/env bash
# Install Pango (needed by WeasyPrint) into .devlibs/pango on macOS, using a private
# micromamba in .devlibs/micromamba. Idempotent: anything already present is skipped.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVLIBS="$REPO/.devlibs"
MM_DIR="$DEVLIBS/micromamba"
MM="$MM_DIR/bin/micromamba"
PREFIX="$DEVLIBS/pango"

case "$(uname -s)" in
  Linux)
    echo "Linux: use your system packages (apt install libpango-1.0-0 libpangoft2-1.0-0)"
    exit 0
    ;;
  Darwin) ;;
  *)
    echo "Unsupported platform: $(uname -s)" >&2
    exit 1
    ;;
esac

case "$(uname -m)" in
  arm64) PLATFORM="osx-arm64" ;;
  x86_64) PLATFORM="osx-64" ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

mkdir -p "$DEVLIBS"

if [ -x "$MM" ]; then
  echo "micromamba already present: $MM"
else
  echo "Downloading micromamba ($PLATFORM)..."
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://micro.mamba.pm/api/micromamba/$PLATFORM/latest" -o "$TMP/mm.tar.bz2"
  mkdir -p "$MM_DIR"
  tar -xjf "$TMP/mm.tar.bz2" -C "$MM_DIR" bin/micromamba
  echo "Installed micromamba: $MM"
fi

if ls "$PREFIX"/lib/libpango-1.0*.dylib >/dev/null 2>&1; then
  echo "Pango already present: $PREFIX"
else
  echo "Installing Pango into $PREFIX (this takes a few minutes)..."
  MAMBA_ROOT_PREFIX="$MM_DIR/root" "$MM" create -y -q -p "$PREFIX" -c conda-forge pango
  echo "Installed Pango: $PREFIX"
fi

echo "Done. Run commands through scripts/with-pango, e.g. scripts/with-pango .venv/bin/pytest"
