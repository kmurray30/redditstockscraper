"""Password-gated browser for the graded threads: a bull/bear x substantive grid, lists, posts.

Runs as its own small Railway service (`thread-ui`), like `gold_server.py`: the worker keeps
no public domain. At start it reads ``data/ui/threads.json.zst`` (written by
``thread_export.py --push``) from the bucket, or the file named by ``THREADS_FILE``.

Routes (all behind HTTP Basic auth, any user name, password ``GOLD_PASSWORD``; ``/healthz``
is open and carries no data):

    /?measure=             the grid: five rows for the model's stance levels, ten columns
                           for thread quality in bins of ten; ``measure`` picks the quality
                           formula (``q`` current, ``q_gated`` corroboration-gated, when the
                           export carries it); every count links to its list
    /list?stance=&qbin=    the titles in one cell, best, newest or most voted first, paged
    /post/<post_id>_<cik>  the thread as the model saw it, with its grades and the
                           company's 5-year outcome from the thread's first cutoff
    /api/grid?q=           the counts as JSON

The pages are rendered here from the exported text; nothing links to Reddit.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

KEY = "data/ui/threads.json.zst"
PAGE_SIZE = 150
# the model's five stance levels (the rubric's levels sit at -1, -0.5, 0, 0.5, 1; the decoded
# value is probability-weighted, so the bins are the midpoints between levels)
STANCES = (("strong_bull", "Strong bull", 0.75), ("bull", "Bull", 0.25), ("neutral", "Neutral", -0.25),
           ("bear", "Bear", -0.75), ("strong_bear", "Strong bear", None))
STANCE_LABEL = {k: v for k, v, _ in STANCES}
QBINS = 10               # quality 0-100 in ten bins of ten
MEASURES = (("q", "Q, current formula"), ("q_gated", "Q, corroboration-gated"))


def stance_class(stance) -> str:
    s = float(stance or 0)
    if s >= 0.75:
        return "strong_bull"
    if s >= 0.25:
        return "bull"
    if s > -0.25:
        return "neutral"
    if s > -0.75:
        return "bear"
    return "strong_bear"


def qbin(q) -> int:
    return max(0, min(QBINS - 1, int(float(q or 0) // (100 / QBINS))))


def qbin_label(b: int) -> str:
    return f"{b * 10}-{(b + 1) * 10}"


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def day(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return ""


class Store:
    def __init__(self, records: list[dict]):
        self.records = records
        self.by_key = {r["key"]: r for r in records}
        self.measures = [m for m, _ in MEASURES if any(r.get(m) is not None for r in records)] or ["q"]
        for r in records:
            r["_stance"] = stance_class(r.get("stance"))
            r["_q"] = float(r.get("q") or 0)

    def measure(self, name: str) -> str:
        return name if name in self.measures else self.measures[0]

    def value(self, r: dict, measure: str) -> float:
        v = r.get(measure)
        return float(v if v is not None else r.get("q") or 0)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Store":
        import zstandard as zstd
        return cls(json.loads(zstd.ZstdDecompressor().decompress(data, max_output_size=2_000_000_000)))

    @classmethod
    def from_file(cls, path: Path) -> "Store":
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_bucket(cls, bucket, key: str = KEY) -> "Store":
        return cls.from_bytes(bucket.get_bytes(key))

    def cell(self, stance: str, b: int, measure: str) -> list[dict]:
        return [r for r in self.records if r["_stance"] == stance and qbin(self.value(r, measure)) == b]

    def grid(self, measure: str) -> dict:
        counts = {s: [0] * QBINS for s, _, _ in STANCES}
        for r in self.records:
            counts[r["_stance"]][qbin(self.value(r, measure))] += 1
        return {"measure": measure, "bins": [qbin_label(b) for b in range(QBINS)],
                "total": len(self.records), "counts": counts}


# ------------------------------------------------------------------ pages

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#17202a;--muted:#6b7280;--line:#e5e7eb;--bull:#15803d;--bear:#b91c1c;
--neutral:#6b7280;--accent:#2563eb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:960px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:24px 0 8px}
.sub{color:var(--muted);margin:0 0 18px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
margin:0 0 14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.gridwrap{overflow-x:auto;padding-bottom:6px}
.grid{display:grid;grid-template-columns:110px repeat(10,minmax(58px,1fr));gap:4px;align-items:stretch;min-width:720px}
.grid .head{font-weight:600;color:var(--muted);font-size:12px;text-align:center;align-self:end;padding:0 2px 4px}
.grid .corner{font-size:12px;color:var(--muted);align-self:end;padding-bottom:4px}
.grid .row{font-weight:600;display:flex;align-items:center;padding-left:4px;font-size:14px}
.grid .tot{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
.cell{border-radius:8px;padding:10px 4px;text-align:center;border:1px solid var(--line);background:var(--card)}
.cell a{display:block;font-size:17px;font-weight:700;line-height:1.2;color:var(--ink)}
.cell.z a{color:var(--muted);font-weight:400}
.bull,.strong_bull{color:var(--bull)}.bear,.strong_bear{color:var(--bear)}.neutral{color:var(--neutral)}
.axis{color:var(--muted);font-size:13px;margin:6px 0 0 114px}
form.ctl{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 16px}form.ctl input[type=number]{width:80px;padding:6px 8px;
font-size:16px;border:1px solid var(--line);border-radius:8px}form.ctl button{padding:7px 14px;border:0;border-radius:8px;background:var(--accent);
color:#fff;font-size:15px}
.item{padding:12px 0;border-bottom:1px solid var(--line)}.item:last-child{border-bottom:0}.item a.t{font-size:17px;font-weight:600;color:var(--ink)}
.meta{color:var(--muted);font-size:13px;margin-top:3px}.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;
border:1px solid currentColor;margin-right:6px}
.pager{display:flex;gap:14px;margin:14px 0;color:var(--muted)}
.body{white-space:pre-wrap;line-height:1.6}.comment{border-left:3px solid var(--line);padding:4px 0 4px 12px;margin:10px 0}
.comment.d1{margin-left:22px}.comment.d2{margin-left:44px}.comment .who{font-weight:600;font-size:13px;color:var(--muted)}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px 14px}.kv div{font-size:14px}.kv b{display:block;font-size:12px;
color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:3px}.bar i{display:block;height:100%;background:var(--accent)}
.crumbs{font-size:14px;color:var(--muted);margin-bottom:10px}
@media(max-width:600px){.grid{grid-template-columns:90px 1fr 1fr;gap:6px}.cell a{font-size:26px}.cell{padding:12px 8px}}
"""


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'><meta name=robots content='noindex,nofollow'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class=wrap>{body}</div></body></html>").encode()


def fmt_q(q) -> str:
    try:
        return f"{float(q):.0f}"
    except (TypeError, ValueError):
        return "?"


def stance_tag(r: dict) -> str:
    s = r["_stance"]
    return f"<span class='tag {s}'>{STANCE_LABEL[s].lower()} {float(r.get('stance') or 0):+.2f}</span>"


def q_tags(store: Store, r: dict) -> str:
    out = []
    for m, label in MEASURES:
        if m in store.measures:
            out.append(f"<span class=tag title='{esc(label)}'>{'Q' if m == 'q' else 'Qg'} {fmt_q(store.value(r, m))}</span>")
    return "".join(out)


def grid_html(store: Store, measure: str) -> bytes:
    g = store.grid(measure)
    peak = max((n for row in g["counts"].values() for n in row), default=1) or 1
    cells = ["<div class=gridwrap><div class=grid><div class=corner>stance &darr; &nbsp; quality &rarr;</div>"]
    for b in range(QBINS):
        cells.append(f"<div class=head>{qbin_label(b)}</div>")
    for s, label, _ in STANCES:
        row = g["counts"][s]
        cells.append(f"<div class='row {s}'>{label}<span class=tot>{sum(row):,}</span></div>")
        for b, n in enumerate(row):
            alpha = 0.08 + 0.72 * (n / peak) ** 0.5 if n else 0
            style = f" style='background:rgba(37,99,235,{alpha:.2f})'" if n else ""
            cells.append(f"<div class='cell{'' if n else ' z'}'{style}>"
                         f"<a href='/list?stance={s}&qbin={b}&measure={measure}'>{n:,}</a></div>")
    cells.append("</div></div>")
    measure_label = dict(MEASURES)[measure]
    switch = " &middot; ".join(f"<b>{lbl}</b>" if m == measure else f"<a href='/?measure={m}'>{lbl}</a>"
                               for m, lbl in MEASURES if m in store.measures)
    body = (f"<h1>Graded threads</h1><p class=sub>{g['total']:,} threads graded by the model, identified variant. "
            f"Rows are the model's stance in its five levels (the decoded value runs from &minus;1 to +1, cut at "
            f"&plusmn;0.25 and &plusmn;0.75); columns are thread quality in ten bins of ten. Quality shown: "
            f"{measure_label}. Click a count to see those threads.</p>"
            f"<p class=sub>Quality measure: {switch}</p>" + "".join(cells)
            + "<p class=axis>Q 0 is worthless, 100 is a fully argued, sourced, level thread with strong independent discussion.</p>")
    return page("Graded threads", body)


def list_html(store: Store, stance: str, b: int, measure: str, sort: str, pg: int) -> bytes:
    rows = store.cell(stance, b, measure)
    if sort == "date":
        rows.sort(key=lambda r: -(r.get("created_utc") or 0))
    elif sort == "votes":
        rows.sort(key=lambda r: -(r.get("votes") or 0))
    else:
        rows.sort(key=lambda r: (-store.value(r, measure), -(r.get("votes") or 0)))
    n = len(rows)
    pages = max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)
    pg = min(max(pg, 1), pages)
    chunk = rows[(pg - 1) * PAGE_SIZE: pg * PAGE_SIZE]
    label = STANCE_LABEL[stance]
    base = f"/list?stance={stance}&qbin={b}&measure={measure}"
    items = []
    for r in chunk:
        subs = esc(", r/".join(r.get("subreddits") or [r.get("subreddit") or ""]))
        items.append(
            f"<div class=item><a class=t href='/post/{quote(r['key'])}?measure={measure}'>{esc(r.get('title') or '(no title)')}</a>"
            f"<div class=meta>{stance_tag(r)}{q_tags(store, r)}"
            f"<b>{esc(r.get('ticker'))}</b> {esc(r.get('name'))} &middot; r/{subs} &middot; "
            f"{day(r.get('created_utc'))} &middot; {int(r.get('votes') or 0)} votes &middot; "
            f"{int(r.get('n_comments') or 0)} comments"
            + (f" &middot; 5y max x{float(r['outcome']['max_mult']):.1f}" if r.get("outcome") and r["outcome"].get("max_mult") is not None else "")
            + "</div></div>")
    sorts = " &middot; ".join(f"<a href='{base}&sort={k}'>{k}</a>" if k != sort else f"<b>{k}</b>" for k in ("q", "date", "votes"))
    pager = (f"<div class=pager>" + (f"<a href='{base}&sort={sort}&page={pg - 1}'>&larr; previous page</a>" if pg > 1 else "")
             + f"<span>page {pg} of {pages}</span>"
             + (f"<a href='{base}&sort={sort}&page={pg + 1}'>next page &rarr;</a>" if pg < pages else "") + "</div>")
    body = (f"<div class=crumbs><a href='/?measure={measure}'>&larr; grid</a></div>"
            f"<h1><span class='{stance}'>{label}</span>, quality {qbin_label(b)}</h1>"
            f"<p class=sub>{n:,} threads with the model's stance {label.lower()} and {dict(MEASURES)[measure]} "
            f"in {qbin_label(b)}. Sort: {sorts}</p>"
            f"<div class=card>{''.join(items) or '<p class=sub>Nothing here.</p>'}</div>{pager}")
    return page(f"{label}, quality {qbin_label(b)}", body)


def kv(label: str, value, bar: float | None = None) -> str:
    inner = f"<b>{esc(label)}</b>{esc(value)}"
    if bar is not None:
        inner += f"<div class=bar><i style='width:{max(0.0, min(100.0, bar)):.0f}%'></i></div>"
    return f"<div>{inner}</div>"


def post_html(store: Store, r: dict, measure: str) -> bytes:
    g = r.get("grades") or {}
    o = r.get("outcome")
    comments = []
    for c in r.get("comments") or []:
        d = min(int(c.get("depth") or 0), 2)
        votes = f" &middot; {c['votes']} votes" if c.get("votes") is not None else ""
        comments.append(f"<div class='comment d{d}'><div class=who>{esc(c.get('label'))}{votes}</div>"
                        f"<div class=body>{esc(c.get('text'))}</div></div>")
    outcome = ""
    if o:
        outcome = ("<h2>What happened next</h2><div class=card><div class=kv>"
                   + kv("from cutoff", r.get("first_month"))
                   + kv("status", o.get("status"))
                   + kv("5y max multiple", f"x{float(o['max_mult']):.1f}" if o.get("max_mult") is not None else "?")
                   + kv("5y vs market", f"{float(o['excess']):+.0%}" if o.get("excess") is not None else "?")
                   + kv("permanent loss", "yes" if o.get("perm_loss") else "no")
                   + "</div><p class=sub style='margin:8px 0 0'>Measured from the first monthly cutoff after the post, "
                     "entering at the next open. Not shown to the model.</p></div>")
    late = []
    if r.get("late_shown"):
        late.append(f"{r['late_shown']} of the comments shown were written after the first cutoff")
    if r.get("edited_late"):
        late.append("the post was edited after the first cutoff")
    thesis = "yes" if r.get("is_thesis") else "no"
    if r.get("is_thesis_p") is not None:
        thesis += f" ({float(r['is_thesis_p']):.0%})"
    grades = ("<h2>The model's grades</h2><div class=card><div class=kv>"
              + kv("stance", f"{float(r.get('stance') or 0):+.2f} ({STANCE_LABEL[r['_stance']].lower()})")
              + "".join(kv(label, fmt_q(store.value(r, m)), store.value(r, m)) for m, label in MEASURES if m in store.measures)
              + kv("is a thesis", thesis)
              + kv("horizon", r.get("horizon") or "?")
              + kv("confidence", f"{float(r.get('confidence') or 0):.2f}")
              + "".join(kv(k.replace("_", " "), fmt_q(g.get(k)), float(g.get(k) or 0)) for k in
                        ("argument_quality", "evidence_quality", "specificity", "tone_stability", "falsifiability",
                         "balance", "hype_risk", "independent_support", "independent_opposition",
                         "strongest_objection_severity"))
              + kv("independent voices", g.get("n_independent_voices"))
              + "</div>" + (f"<p class=sub style='margin:8px 0 0'>Note: {'; '.join(late)}.</p>" if late else "") + "</div>")
    header = (f"<div class=crumbs><a href='/?measure={measure}'>&larr; grid</a> &middot; "
              f"<a href='/list?stance={r['_stance']}&qbin={qbin(store.value(r, measure))}&measure={measure}'>back to the list</a></div>"
              f"<h1>{esc(r.get('title') or '(no title)')}</h1>"
              f"<p class=sub>{stance_tag(r)}{q_tags(store, r)}"
              f"<b>{esc(r.get('ticker'))}</b> {esc(r.get('name'))} &middot; r/{esc(', r/'.join(r.get('subreddits') or [r.get('subreddit') or '']))}"
              + (f" &middot; {esc(r.get('flair'))}" if r.get("flair") else "")
              + f" &middot; posted {day(r.get('created_utc'))} &middot; {int(r.get('votes') or 0)} votes</p>")
    text = (f"<div class=card><div class=body>{esc(r.get('body') or '(no text)')}</div>"
            + (f"<p class=sub><a href='{esc(r['link'])}' rel='noreferrer noopener'>linked page</a></p>" if r.get("link") else "")
            + "</div>")
    if r.get("text_missing"):
        text = "<div class=card><p class=sub>The model's text for this thread is not in the export.</p></div>"
    cm = (f"<h2>Comments as the model saw them</h2><p class=sub>{esc(r.get('comments_note') or '')}</p>"
          f"<div class=card>{''.join(comments) or '<p class=sub>None shown.</p>'}</div>")
    return page(r.get("title") or "thread", header + text + cm + grades + outcome)


# ------------------------------------------------------------------ server

def make_handler(store: Store, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "thread-ui"

        def _authed(self) -> bool:
            h = self.headers.get("Authorization", "")
            if not h.startswith("Basic "):
                return False
            try:
                _, _, given = base64.b64decode(h[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given.encode(), password.encode())

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlsplit(self.path)
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            if u.path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if not self._authed():
                return self._send(401, b"password required", "text/plain",
                                  {"WWW-Authenticate": 'Basic realm="graded threads", charset="UTF-8"'})
            measure = store.measure(qs.get("measure", store.measures[-1]))
            if u.path == "/":
                return self._send(200, grid_html(store, measure))
            if u.path == "/api/grid":
                return self._send(200, json.dumps(store.grid(measure)).encode(), "application/json; charset=utf-8")
            if u.path == "/list":
                stance = qs.get("stance", "bull")
                try:
                    b, pg = int(qs.get("qbin", 9)), int(qs.get("page", 1))
                except ValueError:
                    return self._send(404, b"no such cell", "text/plain")
                if stance not in STANCE_LABEL or not 0 <= b < QBINS:
                    return self._send(404, b"no such cell", "text/plain")
                return self._send(200, list_html(store, stance, b, measure, qs.get("sort", "q"), pg))
            if u.path.startswith("/post/"):
                r = store.by_key.get(u.path[len("/post/"):])
                if r is None:
                    return self._send(404, b"no such thread", "text/plain")
                return self._send(200, post_html(store, r, measure))
            self._send(404, b"not found", "text/plain")

        def log_message(self, fmt, *args):  # no auth headers, no bodies
            sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))
            sys.stdout.flush()

    return Handler


def serve(store: Store, password: str, port: int):
    return ThreadingHTTPServer(("", port), make_handler(store, password))


def main() -> int:
    password = os.environ.get("GOLD_PASSWORD")
    if not password:
        raise SystemExit("GOLD_PASSWORD is not set")
    path = os.environ.get("THREADS_FILE")
    if path:
        store = Store.from_file(Path(path))
    else:
        import time
        import bucket_sync as bs
        bucket = bs.Bucket(bs.load_creds(), workers=2)
        while True:   # the export may not be in the bucket yet (export-threads still running)
            try:
                store = Store.from_bucket(bucket)
                break
            except Exception as e:
                print(f"[thread-ui] waiting for {KEY} in the bucket ({type(e).__name__}); retry in 30 s", flush=True)
                time.sleep(30)
    port = int(os.environ.get("PORT") or 8080)
    print(f"[thread-ui] {len(store.records):,} threads, port {port}", flush=True)
    serve(store, password, port).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
