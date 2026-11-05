#!/bin/bash
# Builds the Windows release: a zip holding the app, a bundled Python and a launcher.
#
#     ./windows/build.sh
#
# Output: ../dist/VBMS-Manager-<version>-windows-x64.zip
# Runs on macOS or Linux. The tests can't run there; CI (.github/workflows/ci.yml) runs them, and the
# launcher, on Windows from the finished zip.
# The user unzips it anywhere and double-clicks "VBMS Manager.cmd"; no Python install needed.
set -euo pipefail
source "$(dirname "$0")/../tools/release_common.sh"

NAME="VBMS Manager"
STAGE="$BUILD/stage-windows/$NAME"
OUT="$DIST/VBMS-Manager-$VERSION-windows-x64.zip"

rm -rf "$BUILD/stage-windows"
fetch_python x86_64-pc-windows-msvc "$STAGE/runtime"

step "Staging app $VERSION"
stage_app "$STAGE/app"
stage_docs "$STAGE" windows
cp "$SRC/windows/VBMS Manager.cmd" "$STAGE/"

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    step "Unit tests with bundled Python"
    (cd "$SRC" && "$STAGE/runtime/python.exe" -B -E -s -m unittest discover -s tests -t .) ;;
  *) echo "(skipping tests: the Windows Python only runs on Windows)" ;;
esac

leak_check "$STAGE"
pack "$STAGE" "$OUT"
echo "Done: $OUT"
