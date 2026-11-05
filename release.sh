#!/bin/bash
# Builds a release: every download for the version in vbms_manager.py, plus SHA256SUMS.
#
#     ./release.sh                       # everything this machine can build
#     ./release.sh source linux docker   # just those
#
# Targets: source, macos (needs a Mac), windows, linux, docker (needs Docker).
# Output: ../dist/<version>/ (or $RELEASE_DIR)   See BUILDING.md.
set -euo pipefail
source "$(dirname "$0")/tools/release_common.sh"
export DIST_DIR="${RELEASE_DIR:-$ROOT/dist/$VERSION}"
DIST="$DIST_DIR"

build_source() {
  # Everything needed to change, test and build the app, minus personal and generated files.
  local name="vbms-manager-$VERSION" stage="$BUILD/stage-source"
  step "Staging source $VERSION"
  rm -rf "$stage"; mkdir -p "$stage/$name"
  rsync -a --exclude '.git/' --exclude '.DS_Store' --exclude '__pycache__/' --exclude '*.pyc' \
    --exclude '/build/' --exclude '/dist/' --exclude '/tests/fixtures/private/' "$SRC/" "$stage/$name/"
  leak_check "$stage/$name"
  pack "$stage/$name" "$DIST/VBMS-Manager-$VERSION-source.zip"
}

if [ $# -eq 0 ]; then
  set -- source
  [ "$(uname -s)" = Darwin ] && set -- "$@" macos
  set -- "$@" windows linux
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    set -- "$@" docker
  else
    echo "(Docker isn't running: skipping the Docker images)"
  fi
fi

mkdir -p "$DIST"
for target in "$@"; do
  case "$target" in
    source) build_source ;;
    macos|windows|linux|docker) "$SRC/$target/build.sh" ;;
    *) echo "unknown target $target (source, macos, windows, linux, docker)"; exit 2 ;;
  esac
done

step "Checksums"
(cd "$DIST" && rm -f SHA256SUMS && for f in VBMS-Manager-*; do printf '%s  %s\n' "$(sha256 "$f")" "$f"; done > SHA256SUMS)
cat "$DIST/SHA256SUMS"
echo "Release $VERSION is in $DIST"
