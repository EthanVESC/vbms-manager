"""The API reference (docs/API.md) as a web page, served at /docs/api.

A small Markdown converter for the subset that file uses: headings, paragraphs, lists (two levels),
tables, fenced code, inline code, bold, italic and links. Everything is escaped, the page has no
scripts and no token, so it's safe to show to any client, including ones from the internet.
"""

import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
API_MD = os.path.join(HERE, "docs", "API.md")
PATH = "/docs/api"

_cache = {"mtime": None, "page": None}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


def _inline(text):
    """Escape, then apply `code`, **bold**, *italic* and [links](url). Code spans are protected first."""
    out, codes = [], []

    def keep(m):
        codes.append("<code>%s</code>" % html.escape(m.group(1)))
        return "\x00%d\x00" % (len(codes) - 1)

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)

    def link(m):
        label, url = m.group(1), html.unescape(m.group(2))
        if not re.match(r"^(https?://|/|#|mailto:)", url):
            return m.group(0)
        ext = url.startswith("http")
        return '<a href="%s"%s>%s</a>' % (html.escape(url), ' target="_blank" rel="noopener"' if ext else "", label)

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    for i, c in enumerate(codes):
        text = text.replace("\x00%d\x00" % i, c)
    out.append(text)
    return "".join(out)


def _row(line):
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def render_markdown(md):
    """Markdown -> (body html, [(level, text, id)] headings)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out, heads, para = [], [], []
    i = 0

    def flush():
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)))
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s.startswith("```"):
            flush()
            lang = s[3:].strip()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            out.append('<pre class="code"%s><code>%s</code></pre>' % (
                ' data-lang="%s"' % html.escape(lang) if lang else "", html.escape("\n".join(body))))
            i += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            flush()
            level, text = len(m.group(1)), m.group(2)
            hid = _slug(re.sub(r"[`*]", "", text))
            heads.append((level, re.sub(r"[`*]", "", text), hid))
            out.append('<h%d id="%s">%s</h%d>' % (level, hid, _inline(text), level))
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|?\s*:?-{2,}", lines[i + 1].strip()):
            flush()
            head = _row(s)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_row(lines[i]))
                i += 1
            out.append('<div class="tablewrap"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
                "".join("<th>%s</th>" % _inline(c) for c in head),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r) for r in rows)))
            continue
        if re.match(r"^(\d+\.|-)\s+", s) and not line.startswith("  "):
            flush()
            ordered = bool(re.match(r"^\d+\.", s))
            items = []
            while i < len(lines):
                ln = lines[i]
                st = ln.strip()
                top = re.match(r"^(\d+\.|-)\s+(.*)$", st) if not ln.startswith("  ") else None
                if top:
                    items.append([top.group(2), []])
                elif ln.startswith("  ") and re.match(r"^-\s+", st) and items:
                    items[-1][1].append([st[2:].strip()])
                elif ln.startswith("  ") and st and items:
                    tgt = items[-1][1][-1] if items[-1][1] else items[-1]
                    tgt[0] += " " + st
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            out.append("<%s>%s</%s>" % (tag, "".join(
                "<li>%s%s</li>" % (_inline(t), "<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(x[0]) for x in sub)
                                   if sub else "") for t, sub in items), tag))
            continue
        if not s:
            flush()
        else:
            para.append(s)
        i += 1
    flush()
    return "\n".join(out), heads


CSS = """
:root{--bg:#f3f4f7;--panel:#fff;--panel2:#f0f2f5;--text:#171a20;--muted:#626a77;--line:#dde1e7;--accent:#2563eb;
--code:#eef1f6;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0d0f13;--panel:#161920;--panel2:#1e222b;--text:#e7e9ee;--muted:#9aa2af;
--line:#2a2f3a;--accent:#4f8cff;--code:#1e222b;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line);
padding:10px max(16px,env(safe-area-inset-left));display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header b{font-size:16px}
header a{color:var(--accent);text-decoration:none;font-size:14px}
header .sp{flex:1}
.wrap{max-width:980px;margin:0 auto;padding:16px;display:grid;grid-template-columns:200px minmax(0,1fr);gap:24px}
nav.toc{position:sticky;top:64px;align-self:start;font-size:13px}
nav.toc a{display:block;color:var(--muted);text-decoration:none;padding:4px 0}
nav.toc a:hover{color:var(--accent)}
main{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:8px 24px 24px;min-width:0}
h1{font-size:24px;margin:16px 0 4px}h2{font-size:19px;margin:28px 0 8px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:16px;margin:20px 0 6px}
a{color:var(--accent)}
code{font:13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:5px;
overflow-wrap:anywhere}
pre.code{background:var(--code);padding:12px 14px;border-radius:10px;overflow-x:auto;line-height:1.45}
pre.code code{background:none;padding:0;overflow-wrap:normal;white-space:pre}
.tablewrap{overflow-x:auto;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line)}
th{background:var(--panel2);font-weight:600}
td:first-child code{white-space:nowrap}
li{margin:3px 0}
.note{color:var(--muted);font-size:13px}
@media (max-width:760px){.wrap{grid-template-columns:1fr;padding:12px}nav.toc{position:static;display:flex;flex-wrap:wrap;gap:4px 14px}
main{padding:4px 14px 18px}}
"""


def page(version=""):
    """The whole HTML page. Re-read when docs/API.md changes."""
    try:
        mtime = os.path.getmtime(API_MD)
    except OSError:
        mtime = None
    if _cache["page"] is not None and _cache["mtime"] == mtime and _cache.get("version") == version:
        return _cache["page"]
    try:
        with open(API_MD, encoding="utf-8") as f:
            md = f.read()
    except OSError:
        md = "# VBMS Manager API (v1)\n\nThe API reference (docs/API.md) is missing from this install."
    body, heads = render_markdown(md)
    toc = "".join('<a href="#%s">%s</a>' % (hid, html.escape(t)) for lvl, t, hid in heads if lvl == 2)
    out = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
           "<title>VBMS Manager API</title><style>%s</style></head><body>"
           "<header><a href=\"/\">&larr; VBMS Manager</a><span class=\"sp\"></span><b>API reference</b>"
           "<span class=\"note\">%s</span></header>"
           "<div class=\"wrap\"><nav class=\"toc\" aria-label=\"Contents\">%s</nav><main>%s</main></div>"
           "</body></html>") % (CSS, html.escape("v" + version if version else ""), toc, body)
    out = out.encode("utf-8")
    _cache.update(mtime=mtime, page=out, version=version)
    return out
