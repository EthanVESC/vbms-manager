"""Static check of the LispBM script running on the BMS.

The charge logic (how vc_charge_start / vc_charge_end are used) lives in the
script, not in the firmware. Before the tool will change those values it
confirms the script uses them in the known vbms32 pattern:

  main loop : charge-ok needs (< c-max (if is-charging vc_charge_end vc_charge_start))
              -> end is the cutoff while charging, start is the re-enable threshold
  wake path : (< c-max (bms-get-param 'vc_charge_end))   [no hysteresis on wake]
  full reset: (>= c-max (bms-get-param 'vc_charge_start)) [coulomb counter reset]

Any other reference to either parameter means the script does something we
haven't reviewed, so writes stay locked until the user acknowledges it.
"""

import hashlib
import re

_P = r"\(bms-get-param\s+'%s\)"
END, START = _P % "vc_charge_end", _P % "vc_charge_start"

HYSTERESIS = re.compile(r"\(<\s*c-max\s*\(if\s+is-charging\s*" + END + r"\s*" + START + r"\s*\)\s*\)")
WAKE_PATH = re.compile(r"\(<\s*c-max\s*" + END + r"\s*\)")
FULL_RESET = re.compile(r"\(>=\s*c-max\s*" + START + r"\s*\)")
ANY_REF = re.compile(r"vc_charge_(start|end)")
STICKY = re.compile(r"sticky", re.I)


def _strip_comments(code: str) -> str:
    # Lisp comments run from ';' to end of line; blank them but keep offsets/lines.
    return re.sub(r";[^\n]*", lambda m: " " * len(m.group(0)), code)


def analyze(code_bytes: bytes) -> dict:
    code = code_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
    sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    body = _strip_comments(code)

    accounted = []

    def find(rx):
        ms = list(rx.finditer(body))
        for m in ms:
            accounted.append((m.start(), m.end()))
        return ms

    hyst = find(HYSTERESIS)
    wake = find(WAKE_PATH)
    full = find(FULL_RESET)

    refs, unexpected = [], []
    for m in ANY_REF.finditer(body):
        line_no = body.count("\n", 0, m.start()) + 1
        line = code.split("\n")[line_no - 1].strip()
        ok = any(a <= m.start() < b for a, b in accounted)
        refs.append({"line": line_no, "text": line, "expected": ok})
        if not ok:
            unexpected.append({"line": line_no, "text": line})

    def excerpt(ms):
        out = []
        for m in ms:
            ln = body.count("\n", 0, m.start()) + 1
            lines = code.split("\n")
            out.append({"line": ln, "text": "\n".join(lines[max(0, ln - 4):ln + 6])})
        return out

    problems = []
    if len(hyst) != 1:
        problems.append("expected exactly one start/end hysteresis check in the charge logic, found %d" % len(hyst))
    if unexpected:
        problems.append("%d unreviewed reference(s) to vc_charge_start/end" % len(unexpected))
    if not code.strip():
        problems.append("no script found on the board")

    return {
        "sha256": sha,
        "length": len(code),
        "lines": code.count("\n") + 1,
        "verified": not problems,
        "problems": problems,
        "hysteresis_found": len(hyst) == 1,
        "wake_path_uses_end": len(wake) > 0,
        "full_reset_uses_start": len(full) > 0,
        "sticky_charger_latch": bool(STICKY.search(code)),
        "references": refs,
        "unexpected": unexpected,
        "excerpts": excerpt(hyst) + excerpt(wake) + excerpt(full),
    }
