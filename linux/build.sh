#!/bin/bash
# Builds the Linux releases: one .tar.gz per CPU type, each holding the app, a bundled Python,
# a launcher and an installer.
#
#     ./linux/build.sh                 # x86_64, arm64 and armv7
#     ./linux/build.sh arm64           # just one
#
# Output: ../dist/VBMS-Manager-<version>-linux-<arch>.tar.gz
# Runs on macOS or Linux. Tests run with each bundled Python when this machine can run it,
# directly or in a Debian container if Docker is available; otherwise they're skipped.
set -euo pipefail
source "$(dirname "$0")/../tools/release_common.sh"

# Debian image the tests run in when this machine can't run the Linux Python itself.
TEST_IMAGE=debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

triple_of() {
  case "$1" in
    x86_64) echo x86_64-unknown-linux-gnu ;;
    arm64)  echo aarch64-unknown-linux-gnu ;;
    armv7)  echo armv7-unknown-linux-gnueabihf ;;
    *) echo "unknown arch $1 (x86_64, arm64, armv7)" >&2; return 1 ;;
  esac
}
docker_platform_of() {
  case "$1" in x86_64) echo linux/amd64 ;; arm64) echo linux/arm64 ;; armv7) echo linux/arm/v7 ;; esac
}

run_tests() {  # <arch> <runtime dir>
  local arch=$1 rt=$2 here
  here=$(uname -m); [ "$here" = aarch64 ] && here=arm64; [ "$here" = armv7l ] && here=armv7
  if [ "$(uname -s)" = Linux ] && [ "$here" = "$arch" ]; then
    step "Unit tests with bundled Python ($arch)"
    (cd "$SRC" && "$rt/bin/python3" -B -E -s -m unittest discover -s tests -t .)
  elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    step "Unit tests with bundled Python ($arch, in Docker)"
    # Drop the copy pulled for another arch: some Docker setups (GitHub's runners) refuse to
    # pull the same pinned digest again for a second platform ("cannot overwrite digest").
    docker image rm "$TEST_IMAGE" >/dev/null 2>&1 || true
    docker run --rm --platform "$(docker_platform_of "$arch")" -v "$SRC:/src:ro" -v "$rt:/rt:ro" -w /src \
      "$TEST_IMAGE" /rt/bin/python3 -B -E -s -m unittest discover -s tests -t .
  else
    echo "(skipping $arch tests: needs Linux on $arch, or Docker)"
  fi
}

ARCHES=("$@"); [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(x86_64 arm64 armv7)
for arch in "${ARCHES[@]}"; do
  triple=$(triple_of "$arch")
  NAME="vbms-manager-$VERSION-linux-$arch"
  STAGE="$BUILD/stage-linux/$NAME"
  rm -rf "$STAGE"
  fetch_python "$triple" "$STAGE/runtime"

  step "Staging app $VERSION ($arch)"
  stage_app "$STAGE/app"
  stage_docs "$STAGE" linux
  cp "$SRC/linux/vbms-manager" "$SRC/linux/install.sh" "$STAGE/"
  chmod 755 "$STAGE/vbms-manager" "$STAGE/install.sh"

  run_tests "$arch" "$STAGE/runtime"
  leak_check "$STAGE"
  pack "$STAGE" "$DIST/VBMS-Manager-$VERSION-linux-$arch.tar.gz"
done
echo "Done: $DIST"
