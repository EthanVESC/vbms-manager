# Shared by the release builds (release.sh, macos/build.sh, windows/build.sh, linux/build.sh).
# Source it from bash; it sets SRC, ROOT, BUILD, DIST and VERSION and defines the helpers below.
# Works with macOS's bash 3.2 and on Linux.

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$SRC/.." && pwd)"
BUILD="${BUILD_DIR:-$ROOT/build}"
DIST="${DIST_DIR:-$ROOT/dist}"
PYTHON="${PYTHON:-python3}"   # any Python 3.8+ on the build machine, for the leak check and packing
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$SRC/vbms_manager.py")
[ -n "$VERSION" ] || { echo "VERSION not found in vbms_manager.py"; exit 1; }

# Pinned Python runtimes (python-build-standalone), one line per target in SHA256SUMS format.
# tools/update_python.sh rewrites the file for a newer release.
PY_PINS="$SRC/tools/python-runtimes.sha256"

step() { printf '\n== %s\n' "$*"; }

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}

# fetch_python <target triple> <dest dir>
# Downloads (once, into $BUILD/cache) the pinned runtime for a target, checks its SHA-256,
# unpacks it into <dest> and trims what the app never uses.
fetch_python() {
  local triple=$1 dest=$2 line want tgz rel got
  line=$(grep -E "^[0-9a-f]{64}  cpython-[^ ]*-$triple-install_only_stripped\.tar\.gz$" "$PY_PINS") \
    || { echo "No Python runtime pinned for $triple in tools/python-runtimes.sha256"; return 1; }
  want=${line%% *}; tgz=${line##* }
  rel=${tgz#*+}; rel=${rel%%-*}
  mkdir -p "$BUILD/cache"
  if [ ! -f "$BUILD/cache/$tgz" ]; then
    step "Downloading $tgz"
    curl -fL --retry 3 -o "$BUILD/cache/$tgz.part" \
      "https://github.com/astral-sh/python-build-standalone/releases/download/$rel/${tgz//+/%2B}"
    mv "$BUILD/cache/$tgz.part" "$BUILD/cache/$tgz"
  fi
  got=$(sha256 "$BUILD/cache/$tgz")
  [ "$got" = "$want" ] || { echo "SHA-256 mismatch for $tgz"; rm -f "$BUILD/cache/$tgz"; return 1; }

  step "Unpacking and trimming Python ($triple)"
  rm -rf "$dest"; mkdir -p "$dest"
  tar -xzf "$BUILD/cache/$tgz" -C "$dest" --strip-components 1
  if [ -f "$dest/python.exe" ]; then   # Windows layout
    rm -rf "$dest"/{include,libs,Scripts,tcl} "$dest"/Lib/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} \
           "$dest"/Lib/site-packages/* "$dest"/DLLs/{_tkinter.pyd,tcl86t.dll,tk86t.dll}
  else
    local lib; lib=$(echo "$dest"/lib/python3.*)
    rm -rf "$dest/include" "$dest/share" "$lib"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,site-packages/*} \
           "$lib"/config-* "$lib"/lib-dynload/_tkinter* "$dest"/lib/{tcl,tk,itcl,thread,Tix}* "$dest"/lib/lib{tcl,tk}* \
           "$dest"/lib/pkgconfig "$dest"/bin/{idle3*,pydoc3*,2to3*,pip*,*-config}
  fi
  find "$dest" -name '__pycache__' -path '*/test*' -prune -exec rm -rf {} +
}

# stage_app <dest dir>: the app code only (allowlist: nothing personal, no tests, fixtures or tools).
stage_app() {
  mkdir -p "$1"
  cp "$SRC"/*.py "$1/"
  local d
  for d in vesc cells web lisp docs; do
    rsync -a --exclude '__pycache__' --exclude '.*' --exclude '*.pyc' "$SRC/$d" "$1/"
  done
}

# stage_docs <dest dir> <platform folder>: README, LICENSE and that platform's "Read Me First.txt".
stage_docs() {
  cp "$SRC/README.md" "$SRC/LICENSE" "$1/"
  cp "$SRC/docs/API.md" "$1/API.md"
  sed "s/@VERSION@/$VERSION/" "$SRC/$2/Read Me First.txt" > "$1/Read Me First.txt"
}

# leak_check <dir>: fails if it finds this computer's board MACs, names, WiFi details or home paths.
leak_check() {
  step "Checking for personal data"
  "$PYTHON" -I "$SRC/tools/leak_check.py" "$1"
}

# pack <dir> <out.zip|out.tar.gz>: archive with the folder as its top level, owners and times normalised.
pack() {
  step "Packing $(basename "$2")"
  mkdir -p "$(dirname "$2")"
  "$PYTHON" -I "$SRC/tools/pack.py" "$1" "$2"
  du -h "$2" | cut -f1
}
