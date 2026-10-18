"""Build the phone-friendly rating page for the gold set.

    python scripts/gold_page.py data/gold/threads.md data/gold/ratings.csv -o gold_ratings.html

The page shows one thread at a time (post, then the pruned comments, exactly as the model
sees them, identified), with 1-5 rating buttons and a reason box. Ratings are saved through
the artifact's `db` capability (collection ``ratings``, one document per post id), so the
session that published the page reads them back with the ArtifactData tool and writes
``gold/ratings.csv``. Nothing in the page talks to any other server.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

HEADER_RE = re.compile(r"^## (\d+)\. \[(?P<ticker>[^\]]+)\] (?P<title>.*)$")
META_RE = re.compile(r"^post_id: `(?P<post_id>[^`]+)`\s+company: (?P<company>.*)$")
COMMENT_RE = re.compile(r"^(?P<indent>\s*)(?P<arrows>>*)\s*(?:\[(?P<votes>-?\d+)\]\s*)?(?P<label>OP|u\d+):\s(?P<text>.*)$")


def parse_message(msg: str) -> dict:
    """The prompt's user message -> structured fields for display."""
    out = {"as_of": "", "company": "", "subreddit": "", "flair": "", "votes": None, "link": "",
           "title": "", "body": "", "comments": [], "comments_note": ""}
    head, _, rest = msg.partition("\n\nTITLE: ")
    for line in head.splitlines():
        if line.startswith("AS-OF DATE: "):
            out["as_of"] = line[len("AS-OF DATE: "):].split(" ")[0]
        elif line.startswith("COMPANY UNDER EVALUATION: "):
            out["company"] = line[len("COMPANY UNDER EVALUATION: "):]
        elif line.startswith("SUBREDDIT: "):
            m = re.match(r"SUBREDDIT: r/(\S+)(?:\s+FLAIR: (.*?))?(?:\s+POST VOTES: (-?\d+))?$", line)
            if m:
                out["subreddit"], out["flair"] = m.group(1), m.group(2) or ""
                out["votes"] = int(m.group(3)) if m.group(3) else None
        elif line.startswith("LINK: "):
            out["link"] = line[len("LINK: "):]
    title, _, rest = rest.partition("\n\nBODY:\n")
    out["title"] = title.strip()
    body, _, comments = rest.partition("\n\nCOMMENTS")
    out["body"] = body.strip()
    if comments:
        note, _, lines = comments.partition("\n")
        out["comments_note"] = "COMMENTS" + note.strip()
        for line in lines.splitlines():
            m = COMMENT_RE.match(line)
            if m:
                out["comments"].append({"depth": len(m.group("arrows")),
                                        "votes": int(m.group("votes")) if m.group("votes") else None,
                                        "label": m.group("label"), "text": m.group("text")})
            elif out["comments"] and line.strip():
                out["comments"][-1]["text"] += " " + line.strip()
    return out


def parse_threads_md(text: str) -> list[dict]:
    threads = []
    blocks = text.split("\n\n---\n\n")[1:]
    for block in blocks:
        lines = block.strip().splitlines()
        h = HEADER_RE.match(lines[0])
        meta = META_RE.match(lines[2]) if len(lines) > 2 else None
        if not h or not meta:
            continue
        start = block.index("```text\n") + len("```text\n")
        end = block.rindex("\n```")
        t = {"n": int(h.group(1)), "ticker": h.group("ticker"), "post_id": meta.group("post_id"),
             "company": meta.group("company"), **parse_message(block[start:end])}
        threads.append(t)
    return threads


def attach_ciks(threads: list[dict], ratings_csv: str) -> None:
    by_post = {r["post_id"]: r for r in csv.DictReader(ratings_csv.splitlines())}
    for t in threads:
        t["cik"] = by_post.get(t["post_id"], {}).get("cik", "")


PAGE = r"""<title>Gold Set Ratings</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&family=Fraunces:opsz,wght@9..144,500;9..144,700&display=swap">
<style>
/* Layout: one thread per screen, a sticky progress bar, a drawer index; reading column ~65ch. */
:root {
  --bg: #f3f5f8; --surface: #ffffff; --surface-2: #e9edf2; --fg: #1b2330; --muted: #5d6877;
  --line: #d5dbe3; --accent: #0f6f72; --accent-fg: #ffffff; --accent-soft: #d9efee;
  --good: #2b7a4b; --warn: #a15c00;
  --display: "Fraunces", Georgia, serif; --body: "IBM Plex Sans", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f141a; --surface: #171e26; --surface-2: #202934; --fg: #e4e9ef; --muted: #98a4b2;
  --line: #2b3542; --accent: #46b8b2; --accent-fg: #0b1a1a; --accent-soft: #16343a;
  --good: #6fcf97; --warn: #e2a84b; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #0f141a; --surface: #171e26; --surface-2: #202934; --fg: #e4e9ef; --muted: #98a4b2;
  --line: #2b3542; --accent: #46b8b2; --accent-fg: #0b1a1a; --accent-soft: #16343a;
  --good: #6fcf97; --warn: #e2a84b; color-scheme: dark }
* { box-sizing: border-box }
body { background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 16px; line-height: 1.5;
  padding-block: 0 24px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)) }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--bg) }
.top { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--bg); display: flex; gap: 12px; align-items: center;
  padding-block: 10px; border-bottom: 1px solid var(--line); margin-bottom: 16px }
.top h1 { font: 700 18px/1.1 var(--display); margin: 0; flex: 1; white-space: nowrap; min-width: 0 }
.progress { font: 500 13px var(--mono); color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap }
.bar { height: 4px; background: var(--surface-2); border-radius: 2px; overflow: hidden; flex-basis: 100%; order: 9 }
.bar i { display: block; height: 100%; background: var(--accent); width: 0 }
button { font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--line); background: var(--surface); color: var(--fg); padding: 10px 14px; min-height: 44px }
button:focus-visible, textarea:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }
button.ghost { background: transparent }
button.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); font-weight: 600 }
button[disabled] { opacity: .5; cursor: default }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 16px; max-width: 72ch; margin-inline: auto }
.meta { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: baseline; font: 500 13px var(--mono); color: var(--muted); margin-bottom: 8px }
.meta .tk { color: var(--accent); font-weight: 600; font-size: 15px }
.meta .pill { background: var(--surface-2); border-radius: 999px; padding: 1px 8px }
h2 { font: 500 22px/1.25 var(--display); margin: 4px 0 12px; text-wrap: balance }
.body { white-space: pre-wrap; overflow-wrap: anywhere; max-width: 65ch }
.body.clamped { max-height: 24em; overflow: hidden; position: relative }
.body.clamped::after { content: ""; position: absolute; inset: auto 0 0; height: 5em; background: linear-gradient(transparent, var(--surface)) }
.more { margin-top: 8px }
.link { font: 13px var(--mono); color: var(--muted); overflow-wrap: anywhere; margin-top: 10px }
h3 { font: 600 11px var(--body); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); margin: 20px 0 8px }
.c { margin: 0 0 10px; padding-left: 10px; border-left: 2px solid var(--line); overflow-wrap: anywhere }
.c.d1 { margin-left: 16px } .c.d2 { margin-left: 32px }
.c .who { font: 500 12px var(--mono); color: var(--muted); display: flex; gap: 8px; margin-bottom: 2px }
.c .who .v { color: var(--fg) } .c .who .op { color: var(--accent) }
.rate { position: sticky; bottom: 0; background: var(--bg); padding-block: 12px calc(8px + env(safe-area-inset-bottom, 0px)); margin-top: 20px; border-top: 1px solid var(--line) }
.rate .inner { max-width: 72ch; margin-inline: auto; display: grid; gap: 10px }
.scale { display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px }
.scale button { padding: 8px 2px; display: grid; gap: 2px; justify-items: center; line-height: 1.1 }
.scale button b { font-size: 20px; font-family: var(--mono) } .scale button small { font-size: 10px; color: var(--muted) }
.scale button.on { background: var(--accent); color: var(--accent-fg); border-color: var(--accent) } .scale button.on small { color: var(--accent-fg) }
textarea { font: inherit; width: 100%; min-height: 44px; resize: vertical; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); color: var(--fg); padding: 10px }
.row { display: flex; gap: 8px; align-items: center } .row .grow { flex: 1 }
.status { font: 12px var(--mono); color: var(--muted); min-height: 1.2em } .status.err { color: var(--warn) }
.drawer { position: fixed; inset: 0; background: var(--bg); z-index: 20; overflow: auto; padding: calc(12px + env(safe-area-inset-top, 0px)) max(16px, env(safe-area-inset-right, 0px)) 24px max(16px, env(safe-area-inset-left, 0px)) }
.drawer ol { list-style: none; padding: 0; margin: 12px auto 0; max-width: 72ch; display: grid; grid-template-columns: minmax(0, 1fr); gap: 6px }
.drawer li { min-width: 0 }
.drawer li button { width: 100%; min-width: 0; overflow: hidden; text-align: left; display: flex; gap: 10px; align-items: baseline }
.drawer li .n { font: 500 12px var(--mono); color: var(--muted); width: 2ch } .drawer li .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.drawer li .r { font: 600 13px var(--mono); color: var(--good); width: 2ch; text-align: right } .drawer li .r.none { color: var(--muted) }
.done { text-align: center; padding: 24px 0 } .done p { color: var(--muted) }
@media (prefers-reduced-motion: no-preference) { .bar i { transition: width .3s } }
</style>
<div class="top">
  <h1>Gold set ratings</h1>
  <span class="progress" id="progress">0 of 0 rated</span>
  <button class="ghost" id="indexBtn" type="button" aria-label="Open the thread index">Index</button>
  <div class="bar"><i id="barFill"></i></div>
</div>
<main id="main"><div class="card"><p class="status">Loading threads.</p></div></main>
<div class="rate" id="rate" hidden>
  <div class="inner">
    <div class="scale" id="scale"></div>
    <textarea id="reason" placeholder="Why, in a few words"></textarea>
    <div class="row">
      <button type="button" id="prev">Back</button>
      <button type="button" class="primary grow" id="save">Save and next</button>
      <button type="button" id="skip">Skip</button>
    </div>
    <div class="status" id="status"></div>
  </div>
</div>
<div class="drawer" id="drawer" hidden>
  <div class="row"><h1 style="font:700 20px var(--display);margin:0" class="grow">Threads</h1><button type="button" id="closeIndex">Close</button></div>
  <ol id="list"></ol>
</div>
<script type="application/json" id="threads">__THREADS_JSON__</script>
<script>
(function () {
  const THREADS = JSON.parse(document.getElementById('threads').textContent);
  const LABELS = ['worthless', 'weak', 'some merit', 'solid', 'would act'];
  const $ = id => document.getElementById(id);
  let ratings = {}, DB = null, idx = 0, pending = {rating: null, reason: ''}, synced = false;
  try { const s = localStorage.getItem('gold.idx'); if (s !== null) idx = Math.min(THREADS.length - 1, Math.max(0, +s)); } catch (e) {}
  try { const r = localStorage.getItem('gold.local'); if (r) ratings = JSON.parse(r); } catch (e) {}

  function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }
  function ratedCount() { return THREADS.filter(t => ratings[t.post_id] && ratings[t.post_id].rating).length; }
  function status(msg, err) { const s = $('status'); s.textContent = msg || ''; s.classList.toggle('err', !!err); }

  function renderScale() {
    $('scale').innerHTML = LABELS.map((l, i) => `<button type="button" data-r="${i + 1}" class="${pending.rating === i + 1 ? 'on' : ''}"><b>${i + 1}</b><small>${l}</small></button>`).join('');
  }
  function render() {
    const n = ratedCount();
    $('progress').textContent = `${n} of ${THREADS.length} rated`;
    $('barFill').style.width = (THREADS.length ? 100 * n / THREADS.length : 0) + '%';
    const t = THREADS[idx];
    if (!t) { $('main').innerHTML = '<div class="card done"><h2>No threads</h2></div>'; $('rate').hidden = true; return; }
    const existing = ratings[t.post_id] || {};
    if (pending.post_id !== t.post_id) pending = {post_id: t.post_id, rating: existing.rating || null, reason: existing.reason || ''};
    const votes = t.votes === null || t.votes === undefined ? '' : `<span class="pill">${t.votes} votes</span>`;
    const flair = t.flair ? `<span class="pill">${esc(t.flair)}</span>` : '';
    const comments = t.comments.length ? t.comments.map(c => `<div class="c d${c.depth}"><div class="who"><span class="${c.label === 'OP' ? 'op' : ''}">${c.label}</span>${c.votes === null ? '' : `<span class="v">${c.votes} votes</span>`}</div><div>${esc(c.text)}</div></div>`).join('')
      : '<p class="status">No comments inside the 24-hour window.</p>';
    const long = t.body.length > 1800;
    $('main').innerHTML = `<article class="card">
      <div class="meta"><span class="tk">${esc(t.ticker)}</span><span>${esc(t.company)}</span><span>r/${esc(t.subreddit)}</span><span>${esc(t.as_of)}</span>${flair}${votes}<span>${t.n} / ${THREADS.length}</span></div>
      <h2>${esc(t.title)}</h2>
      <div class="body ${long ? 'clamped' : ''}" id="body">${esc(t.body) || '<i>(no text)</i>'}</div>
      ${long ? '<button type="button" class="ghost more" id="more">Show the whole post</button>' : ''}
      ${t.link ? `<div class="link">${esc(t.link)}</div>` : ''}
      <h3>${esc(t.comments_note || 'Comments')}</h3>${comments}
    </article>`;
    if (long) $('more').onclick = () => { $('body').classList.remove('clamped'); $('more').remove(); };
    $('rate').hidden = false;
    $('reason').value = pending.reason;
    renderScale();
    $('prev').disabled = idx === 0;
    $('save').textContent = idx === THREADS.length - 1 ? 'Save' : 'Save and next';
    $('save').disabled = !pending.rating;
    try { localStorage.setItem('gold.idx', String(idx)); } catch (e) {}
    window.scrollTo({top: 0});
  }
  function renderIndex() {
    $('list').innerHTML = THREADS.map((t, i) => { const r = ratings[t.post_id] && ratings[t.post_id].rating;
      return `<li><button type="button" data-i="${i}"><span class="n">${t.n}</span><span class="t"><b>${esc(t.ticker)}</b> ${esc(t.title)}</span><span class="r ${r ? '' : 'none'}">${r || '–'}</span></button></li>`; }).join('');
  }
  const API = window.__GOLD_API__ ? {
    async load() { const r = await fetch('/api/ratings', {cache: 'no-store'}); if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); },
    async save(doc) { const r = await fetch('/api/rate', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(doc)}); if (!r.ok) throw new Error('HTTP ' + r.status); }
  } : null;
  async function save() {
    const t = THREADS[idx];
    if (!pending.rating) return;
    const doc = {post_id: t.post_id, cik: t.cik, ticker: t.ticker, n: t.n, rating: pending.rating,
                 reason: $('reason').value.trim(), ts: new Date().toISOString()};
    ratings[t.post_id] = doc;
    try { localStorage.setItem('gold.local', JSON.stringify(ratings)); } catch (e) {}
    if (API) {
      $('save').disabled = true;
      try { await API.save(doc); status(`Saved ${t.ticker} as ${doc.rating}.`); }
      catch (e) { status('Could not save (' + e.message + '). Kept on this phone; try again later.', true); }
    } else if (DB) {
      $('save').disabled = true;
      try { await DB.doc('ratings/' + t.post_id).set(doc); status(`Saved ${t.ticker} as ${doc.rating}.`); }
      catch (e) { status('Could not save (' + (e && e.code || 'error') + '). Kept on this phone; try again later.', true); }
    } else {
      status('Saved on this phone only; sync is not available in this view.', true);
    }
    if (idx < THREADS.length - 1) idx += 1;
    pending = {};
    render();
    if (ratedCount() === THREADS.length) status('All ' + THREADS.length + ' rated. You are done.');
  }
  $('scale').addEventListener('click', e => { const b = e.target.closest('button[data-r]'); if (!b) return; pending.rating = +b.dataset.r; renderScale(); $('save').disabled = false; });
  $('reason').addEventListener('input', () => { pending.reason = $('reason').value; });
  $('save').onclick = save;
  $('skip').onclick = () => { if (idx < THREADS.length - 1) { idx += 1; pending = {}; render(); } };
  $('prev').onclick = () => { if (idx > 0) { idx -= 1; pending = {}; render(); } };
  $('indexBtn').onclick = () => { renderIndex(); $('drawer').hidden = false; };
  $('closeIndex').onclick = () => { $('drawer').hidden = true; };
  $('list').addEventListener('click', e => { const b = e.target.closest('button[data-i]'); if (!b) return; idx = +b.dataset.i; pending = {}; $('drawer').hidden = true; render(); });
  document.addEventListener('keydown', e => { if (e.target.tagName === 'TEXTAREA') return; if (e.key >= '1' && e.key <= '5') { pending.rating = +e.key; renderScale(); $('save').disabled = false; } if (e.key === 'Enter' && pending.rating) save(); });

  // first unrated thread on a fresh open
  if (!ratings[THREADS[idx] && THREADS[idx].post_id]) { const first = THREADS.findIndex(t => !(ratings[t.post_id] && ratings[t.post_id].rating)); if (first >= 0 && idx === 0) idx = first; }
  render();
  function merge(next) {
    ratings = Object.assign({}, ratings, next);
    if (!synced) { synced = true; const first = THREADS.findIndex(t => !(ratings[t.post_id] && ratings[t.post_id].rating)); if (first >= 0 && !(ratings[THREADS[idx].post_id] || {}).rating) idx = first; }
    try { localStorage.setItem('gold.local', JSON.stringify(ratings)); } catch (e) {}
    if (!$('drawer').hidden) renderIndex();
    render();
  }
  (async () => {
    if (API) {
      try { merge(await API.load()); status(''); } catch (e) { status('Could not load saved ratings (' + e.message + '); reload to retry.', true); }
      return;
    }
    const db = (window.claude && window.claude.use) ? await window.claude.use('db') : null;
    if (!db) { status('Sync unavailable in this view: ratings stay on this device.', true); return; }
    DB = db;
    db.collection('ratings').onSnapshot(snap => {
      const next = {}; snap.docs.forEach(d => { next[d.id] = d.data(); });
      merge(next);
    }, err => status('Live sync lost (' + err.code + '); saves still go through.', true));
  })();
})();
</script>
"""


def build(threads: list[dict], mode: str = "artifact") -> str:
    """``artifact``: ratings go to the claude.ai artifact db; ``server``: to the gold server's
    /api routes (same origin, so the browser sends the Basic credentials)."""
    data = json.dumps(threads, ensure_ascii=False).replace("</", "<\\/")
    page = PAGE.replace("__THREADS_JSON__", data)
    if mode == "server":
        page = page.replace('<script type="application/json" id="threads">',
                            '<script>window.__GOLD_API__ = true;</script>\n'
                            '<script type="application/json" id="threads">', 1)
    elif mode != "artifact":
        raise ValueError(mode)
    return page


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("threads_md")
    p.add_argument("ratings_csv")
    p.add_argument("-o", "--out", required=True)
    a = p.parse_args(argv)
    threads = parse_threads_md(Path(a.threads_md).read_text())
    attach_ciks(threads, Path(a.ratings_csv).read_text())
    Path(a.out).write_text(build(threads))
    print(f"{len(threads)} threads -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
