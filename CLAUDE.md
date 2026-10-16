# Reddit long-horizon stock scout: project context for Claude

Read this first. It is a handoff from an earlier cloud session (which could not reach the
user's data or the torrent network), so everything the new session needs is here.

## How to work with this user

- **Run things yourself.** This session runs on the user's Mac, so run commands, read the output,
  fix problems, rerun. Do not ask the user to paste output. Ask only for real decisions.
- When you *do* give the user commands, give **one command per code block**; combine only if
  there is almost no risk of error. No walls of commands.
- macOS, Homebrew, pyenv Python 3.11. Repo at `~/Repos/redditstockscraper`.
- Work on branch `claude/confident-gates-n5noak` (already pushed). **Do not open a PR** unless
  asked. Commit message trailers used in this repo:
  `Co-Authored-By: Claude <noreply@anthropic.com>` is fine.
- Explain results plainly, say when something is unverified, and own mistakes. The earlier
  session made several wrong guesses about aria2c that the user paid for in retries.

## What we are building

A **Reddit thesis-discovery engine** for long-horizon stock ideas, **not** a sentiment trader.
Goal: surface companies with asymmetric upside (a plausible multibagger with a financially
resilient base) by scoring *information quality* in Reddit investment discussions, then validating
with a strictly point-in-time backtest. Nothing can guarantee a safe downside; the system finds
candidates for human research.

Key ideas from the research report (the report file itself is not in the repo):

- Academic evidence: raw mention counts / sentiment are weak or negative at 1y horizons;
  well-sourced DD, external links and *niche* low-saturation discussion do better at 8+ weeks.
  Reddit signal quality is regime-dependent (WSB DD worked before Jan 2021, not after).
- Four engines: (1) conversation intelligence (post + comments + author), (2) information-event
  dedup (500 reposts of one fact = one event), (3) independent fundamental-asymmetry screening,
  (4) point-in-time validation.
- Separate **direction** (stance) from **quality**. Sentiment is a descriptor, never the alpha.
- Quality score `Q = 100(.22A + .16E + .15C + .15R + .10N + .08F + .06H + .08B) - 25M - 15U`
  (argument, evidence, author credibility, independent corroboration, novelty, falsifiability,
  horizon fit, balance, minus manipulation risk and crowd saturation). Starting hypothesis only.
- Author credibility must use history *before* the post (shrinkage, no future leakage).
- Time-decay half-lives differ by signal type; alerts need several gates, not one score.
- Headline metric: **top-decile multibagger lift** (P(5x/10x | top decile) / P(5x/10x | universe))
  alongside **permanent-loss lift**. Use survival methods (right-censoring) for recent cohorts.

### Validation rules that must never be broken
- **Point-in-time everything**: no feature may use data after the decision time. Reddit scores and
  comment counts in the dumps are *final* values (not point-in-time); use text and
  comments created within a fixed maturation window (e.g. T+24h) for any backtest feature.
- **Survivorship**: universe must include delisted names and historical tickers.
- **LLM hindsight leakage**: run scoring both identified and ticker-blinded; if performance
  collapses when names are hidden, assume contamination. Posts dated after the model's training
  cutoff are the cleanest check.
- **Overfitting**: keep an experiment ledger of every configuration tried; sealed holdout used
  once; block-bootstrap / permutation tests; deflated Sharpe.
- Baselines to beat: random and size/sector-matched random, momentum, mention counts, sentiment,
  fundamentals alone.

### Chosen split by year (planned)
| Years | Role |
|---|---|
| 2012-2018 | develop (full 5y labels) |
| 2019-2020 | validate |
| 2021-2022 | sealed holdout (spans the GME regime shift) |
| 2023-2025 | LLM-hindsight check and early forward test (labels censored) |

## Decisions already made

- LLM: **OpenAI** (structured outputs, versioned prompts, cache by hash of text+prompt+model,
  spend cap). Not built yet.
- Market data: **Massive (formerly Polygon.io)** for prices, delisted tickers, ticker-change
  events, splits/dividends; **SEC EDGAR** (free) for CIK mapping and XBRL fundamentals (~2009+).
  Check which Massive tier includes financials and how many years of history before buying
  (reportedly ~10y on the mid tier, 20+ on the top tier). Keys via env: `MASSIVE_API_KEY`,
  `OPENAI_API_KEY`. Never commit keys.
- Phase 1 = **research core only**. Phase 2 later: fundamental-resilience gate, forward data from
  non-Reddit sources (Hacker News Algolia, Bluesky Jetstream, Substack RSS), Postgres/pgvector,
  alerts, dashboard. Railway is a possible later home for scheduled jobs; not needed now.
- Storage: DuckDB + Parquet for research. No Elasticsearch/Kafka/vector DB.

## Data access reality (as of Oct 2026)

- Reddit closed self-serve API access (Nov 2025). New requests stop Oct 31 2026; public API
  access ends by Mar 2027; unauthenticated `.json` returns 403. **Do not scrape, rotate accounts or
  circumvent limits.** There is no sanctioned live Reddit source for this user.
- Historical data comes from the community **Watchful1 per-subreddit dumps** on Academic Torrents
  (infohash `1614740ac8c94505e4ecb9d88be8bed7b6afddd4`, 2005-06 to 2024-12, one
  `<sub>_submissions.zst` + `<sub>_comments.zst` per subreddit, zstd NDJSON). **Gray area**:
  not licensed by Reddit. Rules: keep private, never redistribute (do not commit data, do not put
  it on a public endpoint), use for inference/analysis only, **never train models on it**,
  honour deleted content (we drop `[removed]`/`[deleted]` comments).
- 2025+ can be backfilled from the Arctic Shift API (free, best effort, times out under load,
  may stop when Reddit's API access ends).
- Torrent download speed on this user's connection is about 1 MB/s.

## What exists in the repo

`scripts/download_reddit.py` (CLI) and `scripts/reddit_filter.py`; `tests/` (66 tests, run
`python -m pytest -q`); `README.md` documents usage. `requirements.txt`: zstandard, orjson, pytest.
`data/` is gitignored.

Commands (`python scripts/download_reddit.py <cmd>`):
`check` (test each external service), `list` (file sizes from torrent metadata), `preview`
(sample the filter on one month from Arctic Shift), `pipeline` (download one file at a time,
filter, delete raw), `arctic` (2025+ filtered fetch), `torrent` (plain download), `stats`
(per-year counts of a .zst), `report` (`--sub NAME` or `--all`; keep rates, top tickers with
"probably ordinary word" flags, sample titles, comments per post by year; `--write-stoplist FILE`),
`clean-raw` (remove scratch/unverified raw files; `--force` discards an interrupted download).

### The filter (`FILTER_VERSION = "posts-v4"`, recall-first, cheap, run while downloading)
- Keep a post if: a ticker appears (cashtag `$NET`, or a bare ALL-CAPS token that is on the SEC
  ticker list and not on the stoplist), or its flair is in an allowlist (DD, Fundamentals,
  Discussion, ...), or it is a long write-up (>=800 chars with >=2 finance keywords), or it is a
  link post with >= `--min-link-comments` (default 5) comments (`discussed_link`).
- Drop: stickied/megathreads (daily/advice/"moronic Monday" threads), removed posts, noise.
- Comments: keep only comments in kept threads, drop empty/removed ones.
- **Ticker mention counts are tallied over ALL content before anything is dropped**
  (`mentions_posts.csv`, `mentions_comments.csv`) for later saturation/mention baselines.
- Bare-ticker false positives are the main weakness. Defence: a stoplist (English words, finance
  abbreviations like VC/IP/NPV/CF, Roman numerals, state/country codes, personal-finance words
  like CD/TD/LOT/YOU/LINK/NOTE), no bare matching in mostly-uppercase text, extra user words via
  `--stoplist FILE`, and `report` flags tokens that are rarely written in capitals. Cashtags always
  count. GM/GE/GS/MS are deliberately allowed bare. Proper company-name resolution is a later,
  point-in-time resolver (not built): e.g. BYD is Boyd Gaming on the SEC list but usually the
  Chinese carmaker in posts; META means Facebook only from 2022.
- Output per subreddit in `data/corpus/reddit/<sub>/`: `posts.zst`, `comments.zst`,
  `mentions_*.csv`, `manifest.json` (filter version, years, link threshold, stoplist
  fingerprint, counts by reason). A subreddit is skipped if its manifest matches the current
  settings, so changing the filter version / threshold / stoplist re-runs it.

### aria2c / torrent lessons (hard-won; do not undo)
- A torrent's files are contiguous and aria2c downloads whole ~16 MiB pieces, so fetching one file
  also writes the edges of its neighbours (sparse files that can have their *full length* while
  mostly empty). **Size never proves a raw file is good.**
- aria2c refuses to start in an existing multi-file torrent folder without its progress file
  (`<folder>.aria2`), errorCode 13. And a stale progress file makes it report "OK 0B/s" without
  writing the file.
- Therefore each fetch runs in its **own scratch folder** `data/raw/reddit/.work/<sub>_<kind>/`;
  only a file aria2c finished with the exact size is moved to
  `data/raw/reddit/reddit/subreddits24/` and given a `<file>.ok` marker; the scratch folder is
  then deleted. A scratch folder is resumed only if it still has a progress file. Never delete
  raw files by hand.
- The tracker message "Torrent not registered with Academic Torrents" and "Checksum error
  detected" after an integrity check are normal. 64 MiB shown for an 8.6 MB file is normal.
- `.torrent` metadata is cached at `data/raw/reddit/subreddits_2024-12.torrent`; there are
  fallbacks (URL variants, browser user-agent, magnet link via aria2c).
- Arctic Shift reports query timeouts as HTTP 422 "Timeout. Maybe slow down a bit"; the code
  retries with backoff and shrinks the page size. Dates are sent as ISO strings.

## Results so far (filter versions differ; re-run everything under v4)

Sizes (compressed): investing 0.96 GB, stocks 0.91 GB, StockMarket 0.28 GB,
wallstreetbets 7.6 GB (postponed: ~2 h and mostly memes), SecurityAnalysis and ValueInvesting small.

| Subreddit | Posts kept | Comments kept | Notes |
|---|---|---|---|
| SecurityAnalysis | ~7.5k of 28.4k (26%) | ~71k of 137k | mostly link posts; 77% of kept posts are pre-2021; comments per post collapse after 2021 (specific to this sub, not the dump) |
| ValueInvesting | 18.1k of 31.8k (57%) | 448k of 589k | 96% of kept posts are 2021+, so it serves the recent test periods, not the 5y backtest |
| investing | 68.3k of 399k (17%) | 1.45M of 6.1M | the deep history we need: ~32k kept posts 2012-2018; 13-26 comments/post throughout; 2022 volume dips |
| stocks, StockMarket | downloaded/filtered under an older version; reports not reviewed yet | | |

False tickers found in `investing` and fixed in v4 (needs a re-run): CD, TD, LOT, YOU, LINK,
NOTE, IEX, plus a "Moronic Monday" advice thread being kept.

## Next steps

1. `git pull`; re-run `pipeline --subs investing stocks StockMarket --keep-raw` (raw files are
   kept, so no download), also SecurityAnalysis and ValueInvesting (their manifests are old
   versions and re-run automatically), then `report --all`. Inspect the top tickers, add
   false ones with `report --all --write-stoplist data/ref/stoplist.txt` and
   `pipeline ... --stoplist data/ref/stoplist.txt`; repeat until the lists look clean.
2. Decide whether to add more subreddits (wallstreetbets last; maybe sector subs after quality
   review) and whether to backfill 2025+ via `arctic`.
3. Build, in order (all with look-ahead tests):
   - **Security master**: Massive client with Parquet cache (active + inactive tickers, ticker
     change events, splits, dividends, daily aggregates), EDGAR CIK map, point-in-time
     `resolve(ticker, date) -> security_id`, delisting returns. Replace the SEC ticker list used
     by the filter with Massive's active + inactive list so delisted names match bare too.
   - **Entity resolver**: cashtag > ticker + financial context > company-name aliases > reject
     English words; point-in-time (FB to META).
   - **Deterministic baselines** (mention counts, lexicon sentiment, saturation percentile,
     URL features) and the **causal backtest engine** (monthly cohorts, entry after the
     information cutoff, 1/3/5y excess return, 2x/5x/10x touch, drawdown, delisting, censoring,
     lift metrics, experiment ledger, sealed-holdout guard). Run the dumb baselines first, before
     spending any LLM money.
   - **OpenAI thesis scorer** (blinded vs identified, prefilter to the top ~5-15% of content,
     cost guard), then comment adjudication, causal author stats, embedding novelty, info-event
     clustering, ticker aggregation, and the ablation ladder from the report.
4. Needs from the user when reached: `MASSIVE_API_KEY` (check the tier first), `OPENAI_API_KEY`.
