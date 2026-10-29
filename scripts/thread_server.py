"""Password-gated browser for the graded threads: a bull/bear x substantive grid, lists, posts.

Runs as its own small Railway service (`thread-ui`), like `gold_server.py`: the worker keeps
no public domain. At start it reads ``data/ui/threads.json.zst`` (written by
``thread_export.py --push``) from the bucket, or the file named by ``THREADS_FILE``.

Routes (all behind HTTP Basic auth, any user name, password ``GOLD_PASSWORD``; ``/healthz``
is open and carries no data):

    /                      the Reddit darlings as a timeline (``data/ui/darlings.json.zst`` from
                           ``darling_export.py --push``); the grid until that export exists
    /stock/<TICKER>        a darling stock's price history with a marker at every darling
                           post, each a link to the post; the events and outcomes below it
    /darling/<post>_<cik>  a darling post as the model saw it
    /grid?measure=         the grid: five rows for the model's stance levels, ten columns
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
from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

KEY = "data/ui/threads.json.zst"
DARLINGS_KEY = "data/ui/darlings.json.zst"
PAGE_SIZE = 150
# the model's five stance levels (the rubric's levels sit at -1, -0.5, 0, 0.5, 1; the decoded
# value is probability-weighted, so the bins are the midpoints between levels)
STANCES = (("strong_bull", "Strong bull", 0.75), ("bull", "Bull", 0.25), ("neutral", "Neutral", -0.25),
           ("bear", "Bear", -0.75), ("strong_bear", "Strong bear", None))
STANCE_LABEL = {k: v for k, v, _ in STANCES}
QBINS = 10               # quality 0-100 in ten bins of ten; also the ten credibility levels
# the quality measures a record can carry: the two formulas over the component grades, and
# (thesis_v2 exports) the model's own final judgements, where the grid is exact levels: rows
# = direction_5 (1 strong bear .. 5 strong bull), columns = credibility_10 (1..10)
MEASURES = (("q", "Q, current formula"), ("q_gated", "Q, corroboration-gated"),
            ("direct", "the model's own direction x credibility"))
MEASURE_FIELD = {"q": "q", "q_gated": "q_gated", "direct": "credibility_10"}   # a record has the measure if this is set
MEASURE_TAG = {"q": "Q", "q_gated": "Qg", "direct": "cred"}
DIRECTION_CLASS = {5: "strong_bull", 4: "bull", 3: "neutral", 2: "bear", 1: "strong_bear"}
CREDIBILITY_NOTE = ("Credibility 1 is not a stock or a bare link, 5 a good write-up nobody independent "
                    "corroborated, 7 an informed post and informed comments that agree, 10 primary-source "
                    "research several independent voices corroborate with their own evidence.")


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


def col_label(b: int, measure: str) -> str:
    return str(b + 1) if measure == "direct" else qbin_label(b)


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def day(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return ""


def prepare(r: dict) -> dict:
    """The derived fields every page reads off a record."""
    r["_stance"] = stance_class(r.get("stance"))
    r["_q"] = float(r.get("q") or 0)
    d = r.get("direction_5")
    r["_dir"] = DIRECTION_CLASS.get(int(d)) if d is not None else None
    return r


class Store:
    def __init__(self, records: list[dict]):
        self.records = records
        self.by_key = {r["key"]: r for r in records}
        self.measures = [m for m, _ in MEASURES
                         if any(r.get(MEASURE_FIELD[m]) is not None for r in records)] or ["q"]
        for r in records:
            prepare(r)
        prompts = Counter(r.get("prompt") for r in records if r.get("prompt"))
        variants = Counter(r.get("variant") for r in records if r.get("variant"))
        self.prompt = prompts.most_common(1)[0][0] if prompts else None
        self.variant = variants.most_common(1)[0][0] if variants else "identified"
        # the stock filter's options: every ticker with its most common name and thread count,
        # most-discussed first
        per: dict[str, Counter] = {}
        for r in records:
            t = r.get("ticker")
            if t:
                per.setdefault(t, Counter())[r.get("name") or ""] += 1
        self.tickers = sorted(([t, c.most_common(1)[0][0], sum(c.values())] for t, c in per.items()),
                              key=lambda x: (-x[2], x[0]))
        self.ticker_set = set(per)

    def ticker(self, t: str | None) -> str | None:
        """A requested stock filter, or None when it is empty or not a known ticker."""
        t = (t or "").strip().upper()
        return t if t in self.ticker_set else None

    def _rows(self, ticker: str | None) -> list[dict]:
        return self.records if not ticker else [r for r in self.records if r.get("ticker") == ticker]

    def measure(self, name: str) -> str:
        return name if name in self.measures else self.measures[0]

    def value(self, r: dict, measure: str) -> float:
        if measure == "direct":
            v = r.get("q_direct")
            return float(v) if v is not None else 0.0
        v = r.get(measure)
        return float(v if v is not None else r.get("q") or 0)

    def row_key(self, r: dict, measure: str) -> str | None:
        """The grid row: the model's final direction level in direct mode, else its stance class."""
        return r["_dir"] if measure == "direct" else r["_stance"]

    def col(self, r: dict, measure: str) -> int | None:
        """The grid column: the credibility level (0-based) in direct mode, else the quality bin."""
        if measure == "direct":
            c = r.get("credibility_10")
            return None if c is None else max(0, min(QBINS - 1, int(c) - 1))
        return qbin(self.value(r, measure))

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

    def cell(self, stance: str, b: int, measure: str, ticker: str | None = None) -> list[dict]:
        return [r for r in self._rows(ticker) if self.row_key(r, measure) == stance and self.col(r, measure) == b]

    def grid(self, measure: str, ticker: str | None = None) -> dict:
        counts = {s: [0] * QBINS for s, _, _ in STANCES}
        total = 0
        for r in self._rows(ticker):
            k, b = self.row_key(r, measure), self.col(r, measure)
            if k is None or b is None:   # no final judgement on this record (a v1 grade): not in the direct grid
                continue
            counts[k][b] += 1
            total += 1
        return {"measure": measure, "bins": [col_label(b, measure) for b in range(QBINS)],
                "rows": "direction" if measure == "direct" else "stance",
                "total": total, "counts": counts, "prompt": self.prompt, "variant": self.variant,
                "ticker": ticker}


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
.grid{display:grid;grid-template-columns:84px repeat(5,minmax(44px,1fr));gap:4px;align-items:stretch}
.grid .head{font-weight:600;color:var(--muted);font-size:12px;text-align:center;align-self:end;padding:0 2px 4px}
.grid .corner{font-size:12px;color:var(--muted);align-self:end;padding-bottom:4px}
.grid .row{font-weight:600;display:flex;align-items:center;padding-left:4px;font-size:14px}
.grid .head .tot{display:block;margin:0}
.grid .tot{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
.cell{border-radius:8px;padding:10px 4px;text-align:center;border:1px solid var(--line);background:var(--card)}
.cell a{display:block;font-size:17px;font-weight:700;line-height:1.2;color:var(--ink)}
.cell.z a{color:var(--muted);font-weight:400}
.bull,.strong_bull{color:var(--bull)}.bear,.strong_bear{color:var(--bear)}.neutral{color:var(--neutral)}
.axis{color:var(--muted);font-size:13px;margin:8px 0 0}
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
.filter{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 16px}.filter label{font-weight:600;font-size:14px}
.combo{position:relative;flex:1;min-width:220px;max-width:420px}
.combo input{width:100%;padding:8px 10px;font-size:16px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
.dropdown{display:none;position:absolute;z-index:10;left:0;right:0;top:100%;margin-top:4px;max-height:340px;overflow-y:auto;
background:var(--card);border:1px solid var(--line);border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.12)}
.dropdown .opt{padding:8px 10px;cursor:pointer;font-size:14px;display:flex;gap:6px;align-items:baseline;border-bottom:1px solid var(--line)}
.dropdown .opt:hover{background:var(--bg)}.dropdown .opt .n{margin-left:auto;color:var(--muted);font-size:13px}
.dropdown .opt.none{color:var(--muted);cursor:default}.filter .cur{font-size:14px;color:var(--muted)}
@media(max-width:600px){.grid{grid-template-columns:64px repeat(5,1fr);gap:3px}.cell{padding:8px 2px}.cell a{font-size:15px}.grid .head{font-size:11px}.grid .row{font-size:12px}}
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
        if m not in store.measures:
            continue
        if m == "direct":
            if r.get("credibility_10") is not None:
                out.append(f"<span class=tag title='{esc(label)}'>cred {int(r['credibility_10'])}/10 &middot; "
                           f"dir {int(r.get('direction_5') or 0)}/5</span>")
            continue
        out.append(f"<span class=tag title='{esc(label)}'>{MEASURE_TAG[m]} {fmt_q(store.value(r, m))}</span>")
    return "".join(out)


def tq(ticker: str | None) -> str:
    """The stock filter as a query-string suffix, carried through every link."""
    return f"&ticker={quote(ticker)}" if ticker else ""


FILTER_JS = """
(function(){
var opts=JSON.parse(document.getElementById('stockdata').textContent),inp=document.getElementById('stock'),
box=document.getElementById('stocklist'),measure=inp.dataset.measure;
function go(t){location.href='/grid?measure='+encodeURIComponent(measure)+(t?'&ticker='+encodeURIComponent(t):'');}
function esc(x){return String(x).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function render(){
  var q=inp.value.trim().toLowerCase(),list=opts;
  if(q){list=opts.filter(function(o){return o[0].toLowerCase().indexOf(q)===0||o[1].toLowerCase().indexOf(q)>=0;});
    list.sort(function(a,b){var ea=a[0].toLowerCase()===q,eb=b[0].toLowerCase()===q;
      var pa=a[0].toLowerCase().indexOf(q)===0,pb=b[0].toLowerCase().indexOf(q)===0;
      return (eb-ea)||(pb-pa)||(b[2]-a[2])||(a[0]<b[0]?-1:1);});}
  var h='<div class=opt data-t="">All stocks</div>';
  for(var i=0;i<list.length;i++){var o=list[i];
    h+='<div class=opt data-t="'+esc(o[0])+'"><b>'+esc(o[0])+'</b> '+esc(o[1])+'<span class=n>'+o[2]+'</span></div>';}
  if(!list.length)h+='<div class="opt none">No stock matches</div>';
  box.innerHTML=h;box.style.display='block';}
inp.addEventListener('focus',render);inp.addEventListener('input',render);
box.addEventListener('mousedown',function(e){var el=e.target.closest('.opt');if(el&&el.dataset.t!==undefined&&!el.classList.contains('none')){e.preventDefault();go(el.dataset.t);}});
inp.addEventListener('blur',function(){setTimeout(function(){box.style.display='none';},150);});
inp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();var first=box.querySelector('.opt[data-t]:not([data-t=""])');
  var v=inp.value.trim().toUpperCase();var exact=opts.filter(function(o){return o[0]===v;})[0];
  if(exact)go(exact[0]);else if(first)go(first.dataset.t);else if(!v)go('');}
  if(e.key==='Escape'){inp.blur();}});
})();
"""


def filter_html(store: Store, measure: str, ticker: str | None) -> str:
    data = json.dumps(store.tickers, ensure_ascii=False).replace("</", "<\\/")
    current = ""
    if ticker:
        name = next((n for t, n, _ in store.tickers if t == ticker), "")
        current = (f"<span class=cur>Showing only <b>{esc(ticker)}</b> {esc(name)} &middot; "
                   f"<a href='/grid?measure={measure}'>clear</a></span>")
    return (f"<div class=filter><label for=stock>Stock</label>"
            f"<div class=combo><input id=stock type=text autocomplete=off spellcheck=false "
            f"placeholder='Filter by ticker or name ({len(store.tickers):,} stocks)' value='{esc(ticker or '')}' "
            f"data-measure='{esc(measure)}'><div id=stocklist class=dropdown></div></div>{current}</div>"
            f"<script type=application/json id=stockdata>{data}</script><script>{FILTER_JS}</script>")


def grid_html(store: Store, measure: str, ticker: str | None = None) -> bytes:
    g = store.grid(measure, ticker)
    direct = measure == "direct"
    peak = max((n for row in g["counts"].values() for n in row), default=1) or 1
    corner = "credibility &darr; direction &rarr;" if direct else "quality &darr; stance &rarr;"
    cells = [f"<div class=gridwrap><div class=grid><div class=corner>{corner}</div>"]
    order = list(reversed(STANCES))   # strong bear on the left, strong bull on the right
    for s, label, _ in order:
        cells.append(f"<div class='head {s}'>{label}<span class=tot>{sum(g['counts'][s]):,}</span></div>")
    for b in reversed(range(QBINS)):  # best credibility / quality at the top
        cells.append(f"<div class=row>{col_label(b, measure)}</div>")
        for s, _, _ in order:
            n = g["counts"][s][b]
            alpha = 0.08 + 0.72 * (n / peak) ** 0.5 if n else 0
            style = f" style='background:rgba(37,99,235,{alpha:.2f})'" if n else ""
            cells.append(f"<div class='cell{'' if n else ' z'}'{style}>"
                         f"<a href='/list?stance={s}&qbin={b}&measure={measure}{tq(ticker)}'>{n:,}</a></div>")
    cells.append("</div></div>")
    measure_label = dict(MEASURES)[measure]
    switch = " &middot; ".join(f"<b>{lbl}</b>" if m == measure else f"<a href='/grid?measure={m}{tq(ticker)}'>{lbl}</a>"
                               for m, lbl in MEASURES if m in store.measures)
    who = f"{store.variant} variant" + (f", prompt {esc(store.prompt)}" if store.prompt else "")
    if direct:
        axes = ("Columns are the model's own final direction of the whole thread, post weighed with the substantive "
                "comments, strong bear to strong bull; rows are its own final credibility, 10 at the top down to 1, "
                "exact levels, no formula.")
        note = CREDIBILITY_NOTE
    else:
        axes = ("Columns are the model's stance in its five levels (the decoded value runs from &minus;1 to +1, cut at "
                "&plusmn;0.25 and &plusmn;0.75); rows are thread quality in ten bins of ten, best at the top.")
        note = "Q 0 is worthless, 100 is a fully argued, sourced, level thread with strong independent discussion."
    scope = f" about {esc(ticker)}" if ticker else ""
    body = (f"<h1>Graded threads</h1><p class=sub>{g['total']:,} threads{scope} graded by the model, {who}. {axes} "
            f"Shown: {measure_label}. Click a count to see those threads.</p>"
            f"<p class=sub>Measure: {switch}</p>" + filter_html(store, measure, ticker) + "".join(cells)
            + f"<p class=axis>{note}</p>")
    return page("Graded threads", body)


def list_html(store: Store, stance: str, b: int, measure: str, sort: str, pg: int,
              ticker: str | None = None) -> bytes:
    rows = store.cell(stance, b, measure, ticker)
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
    direct = measure == "direct"
    base = f"/list?stance={stance}&qbin={b}&measure={measure}{tq(ticker)}"
    items = []
    for r in chunk:
        subs = esc(", r/".join(r.get("subreddits") or [r.get("subreddit") or ""]))
        items.append(
            f"<div class=item><a class=t href='/post/{quote(r['key'])}?measure={measure}{tq(ticker)}'>{esc(r.get('title') or '(no title)')}</a>"
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
    if direct:
        head, what = (f"{label} (model's direction), credibility {b + 1}",
                      f"the model's own final direction {label.lower()} and credibility {b + 1} of 10")
    else:
        head, what = (f"{label}, quality {qbin_label(b)}",
                      f"the model's stance {label.lower()} and {dict(MEASURES)[measure]} in {qbin_label(b)}")
    scope = f" about <b>{esc(ticker)}</b>" if ticker else ""
    body = (f"<div class=crumbs><a href='/grid?measure={measure}{tq(ticker)}'>&larr; grid</a></div>"
            f"<h1><span class='{stance}'>{label}</span>, {'credibility ' + str(b + 1) if direct else 'quality ' + qbin_label(b)}"
            f"{' &middot; ' + esc(ticker) if ticker else ''}</h1>"
            f"<p class=sub>{n:,} threads{scope} with {what}. Sort: {sorts}</p>"
            f"<div class=card>{''.join(items) or '<p class=sub>Nothing here.</p>'}</div>{pager}")
    return page(head, body)


def kv(label: str, value, bar: float | None = None) -> str:
    inner = f"<b>{esc(label)}</b>{esc(value)}"
    if bar is not None:
        inner += f"<div class=bar><i style='width:{max(0.0, min(100.0, bar)):.0f}%'></i></div>"
    return f"<div>{inner}</div>"


def post_html(store: Store, r: dict, measure: str, ticker: str | None = None, crumbs: str | None = None) -> bytes:
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
    final = ""
    if r.get("credibility_10") is not None:
        d5 = r.get("direction_5")
        final = (kv("direction (model, 1-5)", f"{int(d5)} ({STANCE_LABEL[DIRECTION_CLASS[int(d5)]].lower()}"
                    + (f", weighted {float(r['direction']):.2f})" if r.get("direction") is not None else ")"))
                 + kv("credibility (model, 1-10)", f"{int(r['credibility_10'])}"
                      + (f" (weighted {float(r['credibility']):.2f})" if r.get("credibility") is not None else ""),
                      store.value(r, "direct")))
    grades = ("<h2>The model's grades</h2><div class=card><div class=kv>"
              + final
              + kv("stance", f"{float(r.get('stance') or 0):+.2f} ({STANCE_LABEL[r['_stance']].lower()})")
              + "".join(kv(label, fmt_q(store.value(r, m)), store.value(r, m))
                        for m, label in MEASURES if m in store.measures and m != "direct")
              + kv("is a thesis", thesis)
              + kv("horizon", r.get("horizon") or "?")
              + kv("confidence", f"{float(r.get('confidence') or 0):.2f}")
              + "".join(kv(k.replace("_", " "), fmt_q(g.get(k)), float(g.get(k) or 0)) for k in
                        ("argument_quality", "evidence_quality", "specificity", "tone_stability", "falsifiability",
                         "balance", "hype_risk", "independent_support", "independent_opposition",
                         "strongest_objection_severity"))
              + kv("independent voices", g.get("n_independent_voices"))
              + (kv("prompt", r.get("prompt")) if r.get("prompt") else "")
              + "</div>" + (f"<p class=sub style='margin:8px 0 0'>Note: {'; '.join(late)}.</p>" if late else "") + "</div>")
    row_key, col = store.row_key(r, measure), store.col(r, measure)
    if row_key is None or col is None:   # a record outside the direct grid: back to its stance cell under q
        row_key, col, measure = r["_stance"], qbin(store.value(r, "q")), "q"
    crumbs = crumbs or (f"<a href='/grid?measure={measure}{tq(ticker)}'>&larr; grid</a> &middot; "
                        f"<a href='/list?stance={row_key}&qbin={col}&measure={measure}{tq(ticker)}'>back to the list</a>")
    header = (f"<div class=crumbs>{crumbs}</div>"
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


# ------------------------------------------------------------------ darlings

class Darlings:
    """The darling export: events (company, month it became a darling, its bullish threads,
    outcomes), the darling posts as records, and each darling stock's weekly price history."""

    def __init__(self, data: dict):
        self.data = data
        self.events = sorted(data.get("events") or [], key=lambda e: (e["month"], e["ticker"]))
        self.by_key = {r["key"]: prepare(r) for r in data.get("threads") or []}
        self.stocks = data.get("stocks") or {}
        self.by_ticker: dict[str, list[dict]] = {}
        for e in self.events:
            self.by_ticker.setdefault(e["ticker"], []).append(e)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Darlings":
        import zstandard as zstd
        return cls(json.loads(zstd.ZstdDecompressor().decompress(data, max_output_size=500_000_000)))

    def posts(self, ticker: str) -> list[dict]:
        """Every darling post about the stock, oldest first."""
        keys = {k for e in self.by_ticker.get(ticker, []) for k in e.get("keys") or []}
        return sorted((self.by_key[k] for k in keys if k in self.by_key), key=lambda r: r.get("created_utc") or 0)

    def ticker(self, t: str) -> str | None:
        t = (t or "").strip().upper()
        return t if t in self.by_ticker else None


DARLING_CSS = """
.tl{border-left:3px solid var(--line);margin:0 0 0 8px;padding:0 0 0 20px}
.yr{font-size:20px;font-weight:700;margin:22px 0 10px -32px;background:var(--bg);display:inline-block;padding:2px 8px;
border-radius:8px;border:1px solid var(--line)}
.ev{position:relative;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 12px}
.ev:before{content:'';position:absolute;left:-29px;top:18px;width:13px;height:13px;border-radius:50%;background:var(--accent);
border:2px solid var(--bg)}
.ev .when{color:var(--muted);font-size:13px;font-weight:600}
.ev .tk{font-size:19px;font-weight:800;letter-spacing:.02em;margin-right:8px}
.ev .nm{color:var(--muted)}.ev ul{margin:8px 0 0;padding-left:18px}.ev li{margin:3px 0;font-size:14px}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;background:var(--bg);
border:1px solid var(--line);margin:4px 6px 0 0}
.up{color:var(--bull)}.down{color:var(--bear)}
.chart svg{width:100%;height:auto;display:block}.chart .note{color:var(--muted);font-size:13px;margin-top:6px}
.chart a circle{cursor:pointer}.chart a:hover circle{r:9}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-top:4px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:-1px;margin-right:5px}
.legend i.l{border-radius:0;width:2px;height:14px;background:var(--bear)}
"""


def pct_s(x, plus=True) -> str:
    try:
        return f"{float(x):+.0%}" if plus else f"{float(x):.0%}"
    except (TypeError, ValueError):
        return "?"


def outcome_pills(e: dict) -> str:
    out = []
    for y in ("1", "3", "5"):
        o = (e.get("outcome") or {}).get(y)
        if not o or o.get("status") not in ("complete", "delisted") or o.get("excess") is None:
            out.append(f"<span class=pill>{y}y: not yet</span>")
            continue
        cls = "up" if float(o["excess"]) > 0 else "down"
        best = f", best x{float(o['max_mult']):.1f}" if o.get("max_mult") is not None else ""
        lost = ", permanent loss" if o.get("perm_loss") else ""
        out.append(f"<span class='pill {cls}'>{y}y: {pct_s(o['excess'])} vs S&amp;P{best}{lost}</span>")
    return "".join(out)


def event_html(d: Darlings, e: dict, link_ticker: bool = True) -> str:
    posts = [d.by_key[k] for k in e.get("keys") or [] if k in d.by_key]
    items = "".join(f"<li><a href='/darling/{quote(r['key'])}'>{esc(r.get('title') or '(no title)')}</a> "
                    f"<span class=meta>{day(r.get('created_utc'))} &middot; r/{esc(r.get('subreddit'))} &middot; "
                    f"{int(r.get('n_comments') or 0)} comments &middot; cred {r.get('credibility_10') or '?'}/10</span></li>"
                    for r in posts)
    tk = (f"<a class=tk href='/stock/{quote(e['ticker'])}'>{esc(e['ticker'])}</a>" if link_ticker
          else f"<b class=tk>{esc(e['ticker'])}</b>")
    stats = (f"{e.get('bulls')} bullish threads by {e.get('authors')} authors, {int(e.get('comments') or 0):,} comments, "
             f"mean credibility {float(e.get('credibility') or 0):.1f}/10"
             + (f"; {e.get('bears')} credible bearish" if e.get("bears") else ""))
    return (f"<div class=ev><div class=when>darling from {esc(e['month'][:7])}</div>"
            f"<div>{tk}<span class=nm>{esc(e.get('name') or '')}</span></div>"
            f"<div class=meta>{stats}</div><div>{outcome_pills(e)}</div><ul>{items}</ul></div>")


def timeline_html(d: Darlings) -> bytes:
    rule = d.data.get("rule") or {}
    runup = d.data.get("runup") or [2.0, 6]
    summ = (d.data.get("summary") or {}).get("3") or {}
    intro = ("<p class=sub>A company becomes a Reddit darling the first month that, over the last three months, "
             f"at least {rule.get('bulls', '?')} credible bullish threads by {rule.get('authors', '?')} or more different "
             f"authors drew {rule.get('comments', '?')}+ comments and outweigh the credible bearish ones (credibility "
             f"{rule.get('credibility', '?')}/10 or better counts). Blue chips are left out, and so is any month in which "
             f"the stock had already gone up {float(runup[0]):g}x over {int(runup[1])} months (a missed run-up). "
             "Click a ticker for its price chart with the darling posts marked.</p>")
    if summ.get("n"):
        intro += (f"<p class=sub>Track record so far (3 years later, {summ['n']} darlings with an ended horizon): "
                  f"{pct_s(summ.get('p_beat'), False)} beat the S&amp;P against {pct_s((summ.get('matched') or {}).get('p_beat'), False)} "
                  f"for comparable stocks; {pct_s(summ.get('p_loss'), False)} lost most of their value for good against "
                  f"{pct_s((summ.get('matched') or {}).get('p_loss'), False)}.</p>")
    body, year = [], None
    for e in d.events:
        y = e["month"][:4]
        if y != year:
            n = sum(1 for x in d.events if x["month"][:4] == y)
            body.append(f"<div class=yr>{y} <span class=tot>{n} darling{'s' if n != 1 else ''}</span></div>")
            year = y
        body.append(event_html(d, e))
    head = (f"<div class=crumbs><a href='/grid'>all graded threads (grid) &rarr;</a></div>"
            f"<h1>Reddit darlings, {d.events[0]['month'][:4] if d.events else ''}-{year or ''}</h1>{intro}")
    return page("Reddit darlings", head + f"<style>{DARLING_CSS}</style><div class=tl>{''.join(body)}</div>")


def _ticks(lo: float, hi: float) -> list[float]:
    """Round price levels between lo and hi for a log axis (1, 2, 5 x 10^k)."""
    import math
    out = []
    for k in range(math.floor(math.log10(max(lo, 1e-6))) - 1, math.ceil(math.log10(max(hi, 1e-6))) + 1):
        for m in (1, 2, 5):
            v = m * 10 ** k
            if lo <= v <= hi:
                out.append(v)
    if len(out) > 7:
        out = [v for v in out if str(v).lstrip("0.").startswith("1")] or out
    return out


def chart_svg(prices: list, posts: list[dict], events: list[dict], w: int = 760, h: int = 340) -> str:
    """The stock's adjusted close on a log scale, a dashed line at each darling month and a
    clickable marker at each darling post (on the day it was posted)."""
    import math
    pts = [(datetime.strptime(dte, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp(), float(c))
           for dte, c in prices if c and float(c) > 0]
    if len(pts) < 2:
        return "<p class=sub>No price history for this stock.</p>"
    left, right, top, bottom = 56, 14, 14, 30
    t0, t1 = pts[0][0], pts[-1][0]
    marks = [(float(r["created_utc"]), float(r["close_at_post"]), r) for r in posts
             if r.get("created_utc") and r.get("close_at_post")]
    lo = min([c for _, c in pts] + [c for _, c, _ in marks])
    hi = max([c for _, c in pts] + [c for _, c, _ in marks])
    llo, lhi = math.log(lo * 0.9), math.log(hi * 1.1)
    x = lambda t: left + (t - t0) / max(t1 - t0, 1) * (w - left - right)
    y = lambda c: top + (lhi - math.log(c)) / max(lhi - llo, 1e-9) * (h - top - bottom)
    out = [f"<svg viewBox='0 0 {w} {h}' role=img aria-label='price chart'>"]
    for v in _ticks(lo * 0.9, hi * 1.1):
        yy = y(v)
        out.append(f"<line x1={left} x2={w - right} y1={yy:.1f} y2={yy:.1f} stroke='#e5e7eb'/>"
                   f"<text x={left - 6} y={yy + 4:.1f} font-size=12 text-anchor=end fill='#6b7280'>"
                   f"${v:g}</text>")
    y0, y1 = datetime.fromtimestamp(t0, tz=timezone.utc).year + 1, datetime.fromtimestamp(t1, tz=timezone.utc).year
    for yr in range(y0, y1 + 1):
        t = datetime(yr, 1, 1, tzinfo=timezone.utc).timestamp()
        xx = x(t)
        out.append(f"<line x1={xx:.1f} x2={xx:.1f} y1={top} y2={h - bottom} stroke='#f0f1f3'/>"
                   f"<text x={xx:.1f} y={h - 10} font-size=12 text-anchor=middle fill='#6b7280'>{yr}</text>")
    for e in events:
        t = datetime.strptime(e["month"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        if t0 <= t <= t1:
            xx = x(t)
            out.append(f"<line x1={xx:.1f} x2={xx:.1f} y1={top} y2={h - bottom} stroke='#b91c1c' stroke-dasharray='5 4'>"
                       f"<title>darling from {esc(e['month'][:7])}</title></line>")
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(c):.1f}" for i, (t, c) in enumerate(pts))
    out.append(f"<path d='{path}' fill=none stroke='#17202a' stroke-width=1.6 />")
    for t, c, r in marks:
        if t0 <= t <= t1:
            out.append(f"<a href='/darling/{quote(r['key'])}'><title>{day(r.get('created_utc'))}: "
                       f"{esc(r.get('title') or '')} (close ${c:,.2f})</title>"
                       f"<circle cx={x(t):.1f} cy={y(c):.1f} r=7 fill='#2563eb' stroke='#fff' stroke-width=2 /></a>")
    out.append("</svg>")
    return "".join(out)


def stock_html(d: Darlings, ticker: str) -> bytes:
    st = d.stocks.get(ticker) or {}
    evs = d.by_ticker.get(ticker, [])
    posts = d.posts(ticker)
    name = st.get("name") or (evs[0].get("name") if evs else "")
    chart = (f"<div class='card chart'>{chart_svg(st.get('prices') or [], posts, evs)}"
             "<div class=legend><span><i style='background:#2563eb'></i>a darling post (click to open it)</span>"
             "<span><i class=l></i>the month it became a darling</span></div>"
             "<div class=note>Adjusted close (splits and dividends), weekly, log scale.</div></div>")
    lst = "".join(f"<div class=item><a class=t href='/darling/{quote(r['key'])}'>{esc(r.get('title') or '(no title)')}</a>"
                  f"<div class=meta>{day(r.get('created_utc'))} &middot; r/{esc(r.get('subreddit'))} &middot; "
                  f"{int(r.get('n_comments') or 0)} comments &middot; cred {r.get('credibility_10') or '?'}/10 &middot; "
                  f"dir {r.get('direction_5') or '?'}/5"
                  + (f" &middot; close that day ${float(r['close_at_post']):,.2f}" if r.get("close_at_post") else "")
                  + "</div></div>" for r in posts)
    body = (f"<div class=crumbs><a href='/'>&larr; timeline</a></div><h1>{esc(ticker)} "
            f"<span class=nm style='font-weight:400;color:var(--muted)'>{esc(name)}</span></h1>"
            f"<p class=sub>{len(evs)} darling event{'s' if len(evs) != 1 else ''}, {len(posts)} darling posts</p>"
            + chart + "<h2>The darling posts</h2><div class=card>" + (lst or "<p class=sub>None.</p>") + "</div>"
            + "<h2>When it was a darling, and what happened next</h2>"
            + "<div class=tl>" + "".join(event_html(d, e, link_ticker=False) for e in evs) + "</div>")
    return page(f"{ticker}: darling chart", f"<style>{DARLING_CSS}</style>" + body)


class DarlingsBox:
    """Holds the darling export once it is loaded; ``loader`` is retried on use, at most once a minute."""

    def __init__(self, darlings: Darlings | None = None, loader=None):
        self.darlings, self.loader, self._tried = darlings, loader, 0.0

    def get(self) -> Darlings | None:
        import time
        if self.darlings is None and self.loader and time.time() - self._tried > 60:
            self._tried = time.time()
            try:
                self.darlings = self.loader()
            except Exception as e:
                print(f"[thread-ui] darlings not loaded ({type(e).__name__})", flush=True)
        return self.darlings


# ------------------------------------------------------------------ server

def make_handler(store: Store, password: str, box: DarlingsBox | None = None):
    box = box or DarlingsBox()
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
            ticker = store.ticker(qs.get("ticker"))
            darlings = box.get()
            if u.path == "/" and darlings is not None and "measure" not in qs and "ticker" not in qs:
                return self._send(200, timeline_html(darlings))
            if u.path in ("/", "/grid"):
                return self._send(200, grid_html(store, measure, ticker))
            if u.path.startswith("/stock/") and darlings is not None:
                t = darlings.ticker(u.path[len("/stock/"):])
                if t is None:
                    return self._send(404, b"not a darling stock", "text/plain")
                return self._send(200, stock_html(darlings, t))
            if u.path.startswith("/darling/") and darlings is not None:
                r = darlings.by_key.get(u.path[len("/darling/"):])
                if r is None:
                    return self._send(404, b"no such darling post", "text/plain")
                t = r.get("darling_ticker") or r.get("ticker")
                crumbs = (f"<a href='/'>&larr; timeline</a> &middot; "
                          f"<a href='/stock/{quote(t or '')}'>{esc(t)} price chart</a>")
                return self._send(200, post_html(store, r, "direct", None, crumbs))
            if u.path == "/api/grid":
                return self._send(200, json.dumps(store.grid(measure, ticker)).encode(), "application/json; charset=utf-8")
            if u.path == "/api/stocks":
                return self._send(200, json.dumps(store.tickers).encode(), "application/json; charset=utf-8")
            if u.path == "/list":
                stance = qs.get("stance", "bull")
                try:
                    b, pg = int(qs.get("qbin", 9)), int(qs.get("page", 1))
                except ValueError:
                    return self._send(404, b"no such cell", "text/plain")
                if stance not in STANCE_LABEL or not 0 <= b < QBINS:
                    return self._send(404, b"no such cell", "text/plain")
                return self._send(200, list_html(store, stance, b, measure, qs.get("sort", "q"), pg, ticker))
            if u.path.startswith("/post/"):
                r = store.by_key.get(u.path[len("/post/"):])
                if r is None:
                    return self._send(404, b"no such thread", "text/plain")
                return self._send(200, post_html(store, r, measure, ticker))
            self._send(404, b"not found", "text/plain")

        def log_message(self, fmt, *args):  # no auth headers, no bodies
            sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))
            sys.stdout.flush()

    return Handler


def serve(store: Store, password: str, port: int, box: DarlingsBox | None = None):
    return ThreadingHTTPServer(("", port), make_handler(store, password, box))


def main() -> int:
    password = os.environ.get("GOLD_PASSWORD")
    if not password:
        raise SystemExit("GOLD_PASSWORD is not set")
    path = os.environ.get("THREADS_FILE")
    dpath = os.environ.get("DARLINGS_FILE")
    if path:
        store = Store.from_file(Path(path))
        box = DarlingsBox(loader=(lambda: Darlings.from_bytes(Path(dpath).read_bytes())) if dpath else None)
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
        box = DarlingsBox(loader=lambda: Darlings.from_bytes(bucket.get_bytes(DARLINGS_KEY)))
    port = int(os.environ.get("PORT") or 8080)
    d = box.get()
    print(f"[thread-ui] {len(store.records):,} threads, "
          + (f"{len(d.events)} darling events" if d else "no darling export yet") + f", port {port}", flush=True)
    serve(store, password, port, box).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
