#!/bin/sh
# Installs VBMS Manager for the current user (no root needed).
#
#   ./install.sh              copy to ~/.local/share/vbms-manager, add the `vbms-manager` command
#                             and an app-menu entry
#   ./install.sh --service    also run it in the background now and at every boot (systemd user
#                             service), for an always-on computer such as a Raspberry Pi
#   ./install.sh --service --lan   the same, reachable from phones and other computers on your network
#   ./install.sh --uninstall  remove all of that (your data in ~/.vbms_manager is kept)
set -eu

service=; lan=; uninstall=
for a in "$@"; do
  case "$a" in
    --service) service=1 ;;
    --lan) lan=" --lan" ;;
    --uninstall) uninstall=1 ;;
    *) echo "unknown option $a (--service, --lan, --uninstall)"; exit 2 ;;
  esac
done

here=$(dirname "$(readlink -f "$0")")
dest="${XDG_DATA_HOME:-$HOME/.local/share}/vbms-manager"
bin="$HOME/.local/bin"
desktop="${XDG_DATA_HOME:-$HOME/.local/share}/applications/vbms-manager.desktop"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/vbms-manager.service"

have_systemd() { command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }

if [ -n "$uninstall" ]; then
  if [ -f "$unit" ]; then
    have_systemd && systemctl --user disable --now vbms-manager.service || true
    rm -f "$unit"
    have_systemd && systemctl --user daemon-reload || true
  fi
  rm -rf "$dest" "$desktop"
  [ -L "$bin/vbms-manager" ] && rm -f "$bin/vbms-manager"
  echo "VBMS Manager removed. Your data is still in ~/.vbms_manager (delete it if you don't want it)."
  exit 0
fi

[ "$(readlink -f "$here")" = "$(readlink -f "$dest" 2>/dev/null || echo -)" ] || {
  if [ -f "$unit" ] && have_systemd; then systemctl --user stop vbms-manager.service || true; fi
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  cp -R "$here" "$dest"
}
mkdir -p "$bin" "$(dirname "$desktop")"
ln -sf "$dest/vbms-manager" "$bin/vbms-manager"
cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=VBMS Manager
Comment=Monitor VESC VBMS32 boards and set charge limits
Exec="$dest/vbms-manager"
Icon=battery
Terminal=true
Categories=Utility;
EOF
echo "Installed to $dest"
case ":$PATH:" in *":$bin:"*) ;; *) echo "Add $bin to your PATH to run \`vbms-manager\` from anywhere." ;; esac

if [ -n "$service" ]; then
  have_systemd || { echo "systemd (user services) isn't available here; run vbms-manager yourself instead."; exit 1; }
  mkdir -p "$unit_dir"
  cat > "$unit" <<EOF
[Unit]
Description=VBMS Manager
After=network-online.target
Wants=network-online.target

[Service]
ExecStart="$dest/vbms-manager" --no-browser$lan
# Ctrl+C-style stop, so boards are disconnected cleanly
KillSignal=SIGINT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now vbms-manager.service
  echo "Running as a background service. Logs (including the pairing code): journalctl --user -u vbms-manager"
  if command -v loginctl >/dev/null 2>&1 && [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
    echo "To keep it running when you're logged out and start it at boot, run once:  sudo loginctl enable-linger $USER"
  fi
fi
