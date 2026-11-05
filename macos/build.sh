#!/bin/bash
# Builds "VBMS Manager.app" and a DMG for it.
#
#     ./macos/build.sh
#
# Output: ../dist/VBMS-Manager-<version>-macos.dmg (next to this source folder).
# Bundles CPython from python-build-standalone (pinned in tools/python-runtimes.sha256) for
# Apple Silicon and Intel, so the app runs on a Mac with no Python installed.
#
# Signing: ad-hoc by default. For a Developer ID build set
#   SIGN_ID="Developer ID Application: ..."  and optionally
#   NOTARY_PROFILE=<notarytool keychain profile>  to notarize and staple the DMG.
set -euo pipefail
source "$(dirname "$0")/../tools/release_common.sh"
PYTHON=/usr/bin/python3
SIGN_ID="${SIGN_ID:--}"

SHORT_VERSION=${VERSION%%-*}
BUILD_NUM=$(echo "$VERSION" | sed -n 's/.*[a-z]\([0-9][0-9]*\)$/\1/p'); BUILD_NUM=${BUILD_NUM:-1}
APP="$BUILD/stage/VBMS Manager.app"
DMG="$DIST/VBMS-Manager-$VERSION-macos.dmg"

rm -rf "$BUILD/stage" "$BUILD/obj"
mkdir -p "$BUILD/obj" "$DIST"

# --- 1. Python runtimes -----------------------------------------------------
fetch_python aarch64-apple-darwin "$BUILD/obj/python-arm64"
fetch_python x86_64-apple-darwin "$BUILD/obj/python-x86_64"

# --- 2. App code (allowlist: nothing personal, no tests or fixtures) ---------
step "Staging app $VERSION"
mkdir -p "$APP/Contents/MacOS"
res="$APP/Contents/Resources"
stage_app "$res/app"
for arch in arm64 x86_64; do ditto "$BUILD/obj/python-$arch" "$res/python-$arch"; done
sed -e "s/@SHORT_VERSION@/$SHORT_VERSION/" -e "s/@BUILD@/$BUILD_NUM/" -e "s/@VERSION@/$VERSION/" \
    "$SRC/macos/Info.plist" > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

# --- 3. Tests under the bundled interpreters --------------------------------
for arch in arm64 x86_64; do
  py="$res/python-$arch/bin/python3"
  if [ $arch = x86_64 ] && [ "$(uname -m)" = arm64 ] && ! arch -x86_64 /usr/bin/true 2>/dev/null; then
    echo "(skipping x86_64 tests: Rosetta isn't installed)"; continue
  fi
  step "Unit tests with bundled Python ($arch)"
  (cd "$SRC" && arch -$arch "$py" -B -E -s -m unittest discover -s tests -t . 2>&1 | tail -3)
  (cd "$SRC" && arch -$arch "$py" -B -E -s -m unittest discover -s tests -t . >/dev/null 2>&1) \
    || { echo "tests failed under bundled Python ($arch)"; exit 1; }
done

# --- 4. Launcher and icon ---------------------------------------------------
step "Compiling launcher"
for arch in arm64 x86_64; do
  swiftc -O -target $arch-apple-macos13.0 -file-prefix-map "$SRC=." \
    -o "$BUILD/obj/launcher-$arch" "$SRC/macos/VBMSManagerApp.swift" "$SRC/macos/BLEBridge.swift"
done
lipo -create -output "$APP/Contents/MacOS/VBMS Manager" "$BUILD/obj/launcher-arm64" "$BUILD/obj/launcher-x86_64"

step "Drawing icon"
swift "$SRC/macos/make_icon.swift" "$BUILD/obj/icon.png"
iconset="$BUILD/obj/AppIcon.iconset"; mkdir -p "$iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$BUILD/obj/icon.png" --out "$iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/obj/icon.png" --out "$iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns -o "$res/AppIcon.icns" "$iconset"

# --- 5. DMG contents and leak check -----------------------------------------
dmgdir="$BUILD/stage/dmg"
mkdir -p "$dmgdir"
mv "$APP" "$dmgdir/"
APP="$dmgdir/VBMS Manager.app"
ln -s /Applications "$dmgdir/Applications"
stage_docs "$dmgdir" macos
xattr -cr "$dmgdir"
leak_check "$dmgdir"

# --- 6. Sign ----------------------------------------------------------------
step "Signing ($SIGN_ID)"
opts=(--force --sign "$SIGN_ID")
[ "$SIGN_ID" != "-" ] && opts+=(--options runtime --timestamp)
# Inner code first (every Mach-O in the runtimes), then the bundle.
find "$APP/Contents/Resources" -type f \( -perm -u+x -o -name '*.so' -o -name '*.dylib' \) -print0 |
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q 'Mach-O'; then codesign "${opts[@]}" "$f"; fi
  done
codesign "${opts[@]}" "$APP"
codesign --verify --deep --strict "$APP"
lipo -info "$APP/Contents/MacOS/VBMS Manager"

# --- 7. DMG -----------------------------------------------------------------
step "Making $DMG"
rm -f "$DMG"
hdiutil create -quiet -volname "VBMS Manager $VERSION" -srcfolder "$dmgdir" -ov -format UDZO "$DMG"
if [ "$SIGN_ID" != "-" ]; then
  codesign --force --sign "$SIGN_ID" "$DMG"
  if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG"
  fi
fi
du -h "$DMG"
echo "Done: $DMG"
