#!/bin/bash
# Points the release builds at a newer python-build-standalone release.
#
#     ./tools/update_python.sh <release> <python version>     e.g.  ./tools/update_python.sh 20261013 3.12.15
#
# Downloads that release's SHA256SUMS and rewrites tools/python-runtimes.sha256 with the same targets
# as now. Releases are at https://github.com/astral-sh/python-build-standalone/releases; pick one
# that's been out a couple of weeks, then rebuild and test every target (see BUILDING.md).
set -euo pipefail
[ $# -eq 2 ] || { sed -n '2,9p' "$0"; exit 2; }
rel=$1 ver=$2
pins="$(cd "$(dirname "$0")" && pwd)/python-runtimes.sha256"
sums=$(curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/$rel/SHA256SUMS")
new=""
for triple in $(sed -n 's/.*cpython-[^-]*-\(.*\)-install_only_stripped\.tar\.gz$/\1/p' "$pins"); do
  line=$(printf '%s\n' "$sums" | grep -E "  cpython-$ver\+$rel-$triple-install_only_stripped\.tar\.gz$") \
    || { echo "Python $ver isn't in release $rel for $triple"; exit 1; }
  new="$new$line"$'\n'
done
printf '%s' "$new" > "$pins"
cat "$pins"
echo "Updated $pins. Remember the Docker base image in Dockerfile (BUILDING.md)."
