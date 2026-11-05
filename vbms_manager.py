#!/usr/bin/env python3
"""VBMS Manager: monitor VESC VBMS boards over WiFi and set charge limits.

    python3 vbms_manager.py                 # opens http://127.0.0.1:8642
    python3 vbms_manager.py --lan           # also reachable from phones etc. on your network (remembered)
    python3 vbms_manager.py --local-only    # back to this computer only
    python3 vbms_manager.py --add 192.168.1.50 --add 192.168.1.51
    python3 vbms_manager.py --port 9000 --no-browser

The key-authenticated API is under /api/v1 (turn it on in Preferences; see docs/API.md).
"""

import _thread
import argparse
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if sys.version_info < (3, 8):
    sys.exit("VBMS Manager needs Python 3.8 or newer.")

import access  # noqa: E402
import server  # noqa: E402

VERSION = "1.0.0"


def exit_with_parent():
    """Under VBMS Manager.app: stop (cleanly, like Ctrl+C) if the app goes away."""
    parent = os.getppid()

    def watch():
        while os.getppid() == parent:
            time.sleep(2)
        _thread.interrupt_main()
    threading.Thread(target=watch, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="VBMS Manager")
    ap.add_argument("--host", default=None,
                    help="address to serve the UI on (overrides --lan / --local-only)")
    lan = ap.add_mutually_exclusive_group()
    lan.add_argument("--lan", action="store_true",
                     help="serve on your local network too; other devices are view-only until paired (remembered)")
    lan.add_argument("--local-only", action="store_true", help="serve on this computer only (remembered)")
    ap.add_argument("--port", type=int, default=8642)
    ap.add_argument("--add", action="append", default=[], metavar="IP", help="add a board by IP")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-discovery", action="store_true", help="don't listen for board broadcasts")
    args = ap.parse_args()
    if os.environ.get("VBMS_MANAGER_APP"):
        exit_with_parent()

    app = server.App(start_discovery=not args.no_discovery)
    app.version = VERSION
    for h in args.add:
        app.add_host({"host": h})
    if args.lan or args.local_only:
        app.store.update_settings(lan=bool(args.lan))
    host = args.host or ("0.0.0.0" if app.store.settings().get("lan") else "127.0.0.1")
    try:
        httpd = server.serve(app, host, args.port)
    except OSError as e:
        sys.exit("Could not start on port %d (%s). Try --port." % (args.port, e))
    url = "http://127.0.0.1:%d/" % args.port
    print("VBMS Manager %s running at %s  (Ctrl+C to quit)" % (VERSION, url))
    if app.lan:
        ips = access.lan_ips()
        print("On your network:      %s" % ("  ".join("http://%s:%d/" % (ip, args.port) for ip in ips)
                                            or "(no LAN address found)"))
        print("Pairing code:         %s   (other devices can watch; enter this code on one to let it make changes)"
              % app.pairing.code)
        print("If other devices can't connect, allow incoming connections for Python in your firewall.")
        print("From the internet (a forwarded port) only the key-authenticated API answers, and only once it's "
              "turned on in Preferences.")
    else:
        print("Only this computer can open it. Run with --lan to use it from your phone.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.scheduler.stop()
        for b in list(app.boards.values()):
            b.stop()
        print("\nStopped. Boards disconnected.")


if __name__ == "__main__":
    main()
