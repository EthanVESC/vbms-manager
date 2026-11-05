#!/bin/bash
# Builds the Docker image from ../Dockerfile for each CPU type and saves each as a file that
# `docker load` reads, so people can use it without a registry.
#
#     ./docker/build.sh                      # amd64, arm64 and arm/v7 image files
#     ./docker/build.sh arm64                # just one
#     PUSH=ghcr.io/<owner>/vbms-manager ./docker/build.sh
#                                            # instead: push one multi-arch image to a registry
#                                            # (log in first; tags <version> and latest)
#
# Output: ../dist/VBMS-Manager-<version>-docker-<arch>.tar.gz, and the image vbms-manager:<version>
# for this machine's CPU loaded into the local Docker. Needs Docker with buildx (Docker Desktop has
# it); other CPU types build under emulation.
set -euo pipefail
source "$(dirname "$0")/../tools/release_common.sh"

docker info >/dev/null 2>&1 || { echo "Docker isn't running."; exit 1; }
platform_of() {
  case "$1" in
    amd64) echo linux/amd64 ;; arm64) echo linux/arm64 ;; armv7) echo linux/arm/v7 ;;
    *) echo "unknown arch $1 (amd64, arm64, armv7)" >&2; return 1 ;;
  esac
}
ARCHES=("$@"); [ ${#ARCHES[@]} -gt 0 ] || ARCHES=(amd64 arm64 armv7)

if [ -n "${PUSH:-}" ]; then
  plats=""; for a in "${ARCHES[@]}"; do plats="$plats${plats:+,}$(platform_of "$a")"; done
  step "Building and pushing $PUSH:$VERSION ($plats)"
  tags=(-t "$PUSH:$VERSION"); case "$VERSION" in *-*) ;; *) tags+=(-t "$PUSH:latest") ;; esac
  docker buildx build --platform "$plats" --build-arg VERSION="$VERSION" "${tags[@]}" --push "$SRC"
  exit 0
fi

here=$(uname -m); case "$here" in x86_64) here=amd64 ;; aarch64|arm64) here=arm64 ;; armv7l) here=armv7 ;; esac
mkdir -p "$DIST"
for arch in "${ARCHES[@]}"; do
  plat=$(platform_of "$arch")
  out="$DIST/VBMS-Manager-$VERSION-docker-$arch.tar"
  step "Building image for $plat"
  docker buildx build --platform "$plat" --build-arg VERSION="$VERSION" -t "vbms-manager:$VERSION" \
    --output "type=docker,dest=$out.part" "$SRC"
  gzip -9 -n -c "$out.part" > "$out.gz" && rm -f "$out.part"
  du -h "$out.gz" | cut -f1

  if [ "$arch" = "$here" ]; then
    step "Smoke test ($arch)"
    docker load -q -i "$out.gz"
    name="vbms-manager-smoke-$$"
    docker run -d --name "$name" --network none "vbms-manager:$VERSION" >/dev/null
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
    ok=
    for _ in $(seq 1 20); do
      if docker exec "$name" python -I -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8642/', timeout=3)" 2>/dev/null; then
        ok=1; break
      fi
      sleep 1
    done
    docker stop -t 15 "$name" >/dev/null
    logs=$(docker logs "$name" 2>&1)
    echo "$logs" | sed 's/^/  | /'
    [ -n "$ok" ] || { echo "smoke test failed: the UI never answered"; exit 1; }
    echo "$logs" | grep -q "Stopped. Boards disconnected." || { echo "smoke test failed: no clean stop"; exit 1; }
    docker rm "$name" >/dev/null
    echo "smoke test passed"
  fi
done
echo "Done: $DIST"
