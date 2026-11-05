# Building VBMS Manager

How the code is laid out, how to test it, and how every download is built. Everything here works from the source zip (`VBMS-Manager-<version>-source.zip`) or a checkout of this folder. VBMS Manager is GPL-3.0: you can change it and share your builds, as long as you share your source too.

## What a release contains

| File | For | What it is |
|---|---|---|
| `VBMS-Manager-<v>-macos.dmg` | macOS 13+, Apple Silicon and Intel | Menu-bar app with Bluetooth. Bundles Python. |
| `VBMS-Manager-<v>-windows-x64.zip` | Windows 10/11, x64 | Folder with `VBMS Manager.cmd` and a bundled Python. |
| `VBMS-Manager-<v>-linux-x86_64.tar.gz` | Linux PCs | Folder with the `vbms-manager` launcher, `install.sh` and a bundled Python. |
| `VBMS-Manager-<v>-linux-arm64.tar.gz` | 64-bit Raspberry Pi OS, other ARM64 | Same. |
| `VBMS-Manager-<v>-linux-armv7.tar.gz` | 32-bit Raspberry Pi OS | Same. |
| `VBMS-Manager-<v>-docker-<arch>.tar.gz` | Docker on amd64, arm64, arm/v7 | Image file for `docker load`. |
| `VBMS-Manager-<v>-source.zip` | Anyone | This folder: code, tests, tools and these build scripts. |
| `SHA256SUMS` | | Checksums of all of the above. |

Every build carries the same app code. Each platform adds only a launcher and a Python runtime. Bluetooth is the exception: it's done by the Mac app's Swift code (`macos/BLEBridge.swift`), so the other builds are WiFi only.

## Code layout

The app is Python (standard library only, 3.8+) serving a browser UI (plain ES modules, no build step).

| Path | What's in it |
|---|---|
| `vbms_manager.py` | Entry point and command-line options. `VERSION` lives here. |
| `server.py` | Local web server: JSON API and the static UI. |
| `access.py` | Who's asking: this computer, a paired device or a view-only LAN device. |
| `board.py` | One connected board: polling, cached state, history and events. |
| `vesc/` | VESC protocol: packet framing, buffers, the TCP client, config params, BMS values. |
| `ble.py` | Bluetooth link, through the Mac app's bridge. |
| `discovery.py` | Finding boards on the network. |
| `safe_write.py` | The **only** code path that writes config or a Lisp script to a BMS. Read it before changing anything that writes. |
| `config_fields.py` | Which BMS settings the UI may edit, their limits and risks. |
| `charge_limit.py` | Charge-limit percentage to Charge Start/End voltages. |
| `cells/` | Cell profiles and SOC curves (`profiles.json`; method in `tools/cell_curves/`). |
| `schedule.py` | Charge schedules and one-time timers. |
| `datalog.py`, `sessions.py`, `analysis.py` | Recording, charge sessions and history analysis. |
| `script_check.py`, `addons.py`, `lisp/` | Checking the board's Lisp script; the optional charger-latch add-on. |
| `store.py` | Everything saved under `~/.vbms_manager`. |
| `web/` | The UI: `index.html`, `style.css`, `js/` (one module per area, see the comment at the top of each). |
| `tests/` | Unit tests, a mock BMS that speaks the VESC protocol, a mock Bluetooth bridge, captured fixtures. |
| `tools/` | Board capture, synthetic history, and the release helpers below. |
| `macos/`, `windows/`, `linux/`, `docker/`, `Dockerfile`, `compose.yaml` | Packaging for each platform. |

## Running and testing from source

```bash
python3 vbms_manager.py
python3 -m unittest discover -s tests -t .
```

To try changes without a real board, run the mock BMS (`python3 -m tests.mock_bms vbms32_32s 65199 --sim`) and add `127.0.0.1:65199` in the UI. Use a throwaway data folder so you don't touch your own boards' data: `VBMS_MANAGER_HOME=/tmp/vbms-test python3 vbms_manager.py --port 8650`. `tools/seed_history.py` fills such a folder with weeks of synthetic history.

When you change the safety warning text in substance, bump `RISK_VERSION` in `safe_write.py` so every board asks again.

## Building the downloads

```bash
./release.sh                       # everything this machine can build, into ../dist/<version>/
./release.sh source linux docker   # or just some
```

Each target is also its own script: `macos/build.sh`, `windows/build.sh`, `linux/build.sh [x86_64|arm64|armv7]`, `docker/build.sh [amd64|arm64|armv7]`. On their own they write to `../dist/`.

| Target | Builds on | Needs |
|---|---|---|
| `source` | macOS, Linux | `rsync`, `python3` |
| `macos` | macOS | Xcode command-line tools |
| `windows` | macOS, Linux | `curl`, `rsync`, `python3` |
| `linux` | macOS, Linux | `curl`, `rsync`, `python3`; Docker to run the tests on other CPU types |
| `docker` | anywhere with Docker | Docker with buildx (Docker Desktop has it) |

What every build does:

1. **Python runtime.** Downloads the [python-build-standalone](https://github.com/astral-sh/python-build-standalone) build for each target (cached in `../build/cache/`), checks it against the SHA-256 pinned in `tools/python-runtimes.sha256`, and trims parts the app never uses (tests, Tk, pip).
2. **App code by allowlist.** Copies only the `*.py` files and `vesc/`, `cells/`, `web/` and `lisp/`, never tests, fixtures or anything from your data folder (`tools/release_common.sh`, `stage_app`).
3. **Tests** with the bundled Python, where this machine can run it. Linux builds run them in a Debian container for each CPU type when Docker is available. The Windows build's tests run in CI on Windows (below), and the Mac's Intel half only with Rosetta.
4. **Leak check** (`tools/leak_check.py`). Fails the build if the output contains the board MACs, nicknames, addresses, WiFi names and keys, or BLE names and PINs saved on *this* computer in `~/.vbms_manager*`, or a home-folder path. You can add your own patterns (your name, your LAN's IPs), one regex per line, in `~/.vbms_manager-leak-patterns.txt`. They stay out of the source.
5. **Pack** (`tools/pack.py`): archives are written with root as owner, so they don't carry your user name.

The Docker build also starts the image for this machine's CPU, checks the UI answers and that `docker stop` shuts it down cleanly.

### Signing

- **Mac:** ad-hoc signed by default, so testers click *Open Anyway* once. With an Apple Developer ID, set `SIGN_ID="Developer ID Application: …"` and `NOTARY_PROFILE=<notarytool keychain profile>` to sign, notarize and staple.
- **Windows:** unsigned, so SmartScreen asks once. With a code-signing certificate, sign `runtime/python.exe` and `pythonw.exe` with `signtool` in `windows/build.sh` before packing.

## Continuous integration (GitHub Actions)

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

- the tests on Linux, Windows and macOS with Python 3.12, and on Python 3.8, the oldest supported;
- every download built on GitHub's machines (source, Windows, Linux and Docker on Linux; the DMG on macOS), with the same tests and leak check as a local build. They're attached to the run as artifacts;
- the Windows zip unpacked on Windows, the tests run with its bundled Python, and `VBMS Manager.cmd` started to check the page loads.

## Making a new version

1. Change `VERSION` in `vbms_manager.py` (e.g. `0.9.0-beta2`, `1.0.0`). Pre-release versions (anything with a `-`) are marked as pre-releases and never get the Docker `latest` tag.
2. Commit, then tag and push: `git tag v0.9.0-beta2 && git push origin main v0.9.0-beta2`. The tag must be `v` + `VERSION`, or CI stops.
3. CI builds and checks everything and creates a **draft** GitHub release with all the downloads, the source zip and `SHA256SUMS`. Review it and click *Publish*.
4. To also publish the Docker image on tags, set the repository variable `PUBLISH_DOCKER` to `true` (Settings → Secrets and variables → Actions → Variables). It's pushed to `ghcr.io/<owner>/vbms-manager`.

Without GitHub, `./release.sh` builds the same files locally into `../dist/<version>/`.

### Updating the bundled Python

```bash
./tools/update_python.sh <release> <python version>     # e.g. 20261013 3.12.15
```

rewrites `tools/python-runtimes.sha256` from that release's own `SHA256SUMS`, for the same targets. Pick a release that has been out for a couple of weeks. Then update the two Docker images to match:

- `Dockerfile`: the `FROM python:<version>-slim-<debian>@sha256:…` line. Get the digest with `docker buildx imagetools inspect python:<tag>` (the top-level `Digest:`).
- `linux/build.sh`: `TEST_IMAGE`, the Debian image the Linux tests run in.

Rebuild every target and check the tests pass.

### Adding a platform or CPU type

The pattern is the same for any target python-build-standalone supports (Windows ARM64, Linux musl, RISC-V, …):

1. Add its line from the release's `SHA256SUMS` to `tools/python-runtimes.sha256` (the `install_only_stripped` archive).
2. Add it to the build script for that OS (`triple_of` in `linux/build.sh`), or copy `windows/build.sh` as a starting point for a new script. The helpers in `tools/release_common.sh` (`fetch_python`, `stage_app`, `stage_docs`, `leak_check`, `pack`) do most of the work.
3. Add it to `release.sh` and to the table at the top of this file.

The app itself needs only a Python 3.8+ standard library. Anywhere Python runs, `python3 vbms_manager.py` from the source zip works too, with no build at all.

### Publishing Docker images to a registry

`docker/build.sh` writes image files for `docker load`. To publish one multi-arch image instead, log in to your registry and run:

```bash
PUSH=ghcr.io/<owner>/vbms-manager ./docker/build.sh
```

That pushes the tags `<version>` and, for non-pre-release versions, `latest`. If Docker says the builder doesn't support multi-platform images, run `docker buildx create --use` once. Then change `image:` in `compose.yaml` to that name.
