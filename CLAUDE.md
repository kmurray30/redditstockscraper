# Reddit long-horizon stock scout: project context for Claude

Read this first. It is the handoff between sessions. The data layer, point-in-time machinery
and backtest engine are built and tested; what remains is the **evaluation logic** (LLM thesis
scoring etc.), which plugs in through the `Scorer` interface (see "Handoff: evaluation work").

## How to work with this user

- **Run things yourself.** This session runs on the user's Mac, so run commands, read the output,
  fix problems, rerun. Do not ask the user to paste output. Ask only for real decisions.
- When you *do* give the user commands, give **one command per code block**; combine only if
  there is almost no risk of error. No walls of commands.
- macOS, Homebrew, pyenv Python 3.11. Repo at `~/Repos/redditstockscraper`.
- Work on branch `claude/confident-gates-n5noak`. **Do not open a PR** unless asked. Commit
  messages end with the `Co-Authored-By: Claude ...` trailer the harness specifies.
- Never print or commit API keys; they live in the gitignored `.env`. Do not send the user's
  email or any contact string to a service unless they ask (the SEC wants one: see Next steps).
- Explain results plainly, say when something is unverified, and own mistakes. The earlier
  session made several wrong guesses about aria2c that the user paid for in retries.

## What we are building

A **Reddit thesis-discovery engine** for long-horizon stock ideas, **not** a sentiment trader.
Goal: surface companies with asymmetric upside (a plausible multibagger with a financially
resilient base) by scoring *information quality* in Reddit investment discussions, then validating
with a strictly point-in-time backtest. Nothing can guarantee a safe downside; the system finds
candidates for human research.

Key ideas from the research report (full text: `docs/Reddit_Long_Horizon_Stock_Scouting_Full_Report.md`;
read it before designing the scorer, backtest or validation):

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
- Market data (changed from the original plan on cost): **EODHD** (one month of the ~$20 plan,
  bulk-pulled, then cancel) for daily prices of ~60k active + delisted US symbols, and
  **Massive's free tier** only for point-in-time ticker snapshots (`/v3/reference/tickers?date=`),
  which carry CIK/FIGI. Massive prices cost $199/mo for 2012-era history, so we did not buy
  them. **SEC EDGAR** (free) for SIC sectors and later XBRL fundamentals. Keys in `.env`:
  `EODHD_API_KEY`, `MASSIVE_API_KEY`, later `OPENAI_API_KEY` and `SEC_USER_AGENT`.
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

Tests: `python -m pytest -q` (233 tests, ~3 s, no network). `requirements.txt`: zstandard,
orjson, pytest, pyarrow, duckdb. `data/` and `.env` are gitignored.

**Reddit side** (`scripts/download_reddit.py`, `reddit_filter.py`): commands `check`, `list`,
`preview`, `pipeline`, `arctic`, `torrent`, `stats`, `report` (`--sub NAME` / `--all`,
`--write-stoplist` merges *every* flagged word, so review it: real companies such as COST, META,
NET, DOW get flagged too), `clean-raw`. The filter (`FILTER_VERSION = "posts-v4"`) is described
below. All 5 subreddits are filtered with `data/ref/stoplist.txt` (67 hand-picked words) and the
raw files are kept in `data/raw/reddit/reddit/subreddits24/` (re-filtering costs minutes, not a
download). Not downloaded: wallstreetbets (7.6 GB, ~2 h, mostly memes): a decision for the user.
**The filter's bare-ticker rule defaults to the SEC's *current* ticker list**, which is a
survivorship bias in the corpus (see "Sanity check" below); pass
`--tickers data/ref/tickers_all.txt`. The manifest now records a fingerprint of the ticker list
(`tickers`) and its source, so a new list re-runs finished subreddits automatically.

**Research pipeline** (each module has a docstring with the rules; run in this order):

| Step | Command | Output |
|---|---|---|
| Ticker list for the filter (after snapshots + symbols exist) | `python scripts/security_master.py export-tickers` | `data/ref/tickers_all.txt` |
| Re-filter with it (minutes; raw files are kept) | `python scripts/download_reddit.py pipeline --subs stocks investing StockMarket ValueInvesting SecurityAnalysis --keep-raw --tickers data/ref/tickers_all.txt --stoplist data/ref/stoplist.txt` | `data/corpus/reddit/<sub>/` |
| Parquet corpus | `python scripts/corpus.py` | `data/corpus/parquet/{posts,comments}/<sub>.parquet` |
| EODHD symbol lists | `python scripts/market_data.py symbols` | `data/market/eodhd/symbols.parquet` |
| Daily prices (resumable) | `python scripts/market_data.py prices --workers 16` | `eod/<CODE>.parquet`, ~60k files |
| Price index | `python scripts/market_data.py index` | `eod_index.parquet` |
| Splits (universe only) | `python scripts/market_data.py splits --universe data/features/universe.parquet` | `splits/<CODE>.parquet` |
| Ticker snapshots (free key, ~12 s/page) | `python scripts/security_master.py snapshots` | `data/market/massive/snapshots/<date>.parquet`, yearly 2010-2026 |
| Word lists | `python scripts/entity_resolver.py wordstats` | `data/ref/word_like.txt`, `common_words.txt` |
| Mentions | `python scripts/entity_resolver.py mentions` | `data/features/mentions.parquet` |
| Universe + labels | `python scripts/universe.py` | `data/features/{universe,labels}.parquet` |
| Post text features | `python scripts/baselines.py` | `data/features/post_features.parquet` |
| SIC sectors (needs `SEC_USER_AGENT`) | `python scripts/edgar.py` | `data/ref/sic.parquet` |
| Backtest | `python scripts/backtest.py run --baseline mentions --split dev` / `ladder` / `run --scorer file.py:factory` | report + `data/ledger/experiments.jsonl` |

Order matters, and one step is two-pass: `universe.py` runs once to produce the list of codes the
splits pull needs, then `splits --universe ...`, then `universe.py` again (it warns when any
code lacks split data, because the price-adjustment consistency check is skipped for it).
`mentions` needs the snapshots + `symbols.parquet`; `backtest.py` needs mentions, universe,
labels, `post_features.parquet`.

Modules: `security_master.py` (point-in-time `resolve(ticker, date)` -> CIK and `price_code(cik,
date)` -> the right EODHD series), `entity_resolver.py` (cashtag > bare ticker > name; dated;
`blind_text()` for the ticker-blinded LLM test), `names.py`, `prices.py` (adjusted series,
cleaning, `outcome()` labels), `universe.py`, `pit.py` (the point-in-time corpus view),
`scorer.py` (interface), `baselines.py`, `backtest.py` (engine), `stats.py` (KM, block bootstrap,
permutation, deflated Sharpe), `ledger.py` (experiment ledger, sealed holdout, embargo).

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

### Data-quality traps found in this build (all fixed; do not regress)

These each produced fake results before they were caught. The tests encode them.
- **Ticker reuse and renames.** EODHD files a code's history under its *current* holder: `BBBY`
  is Overstock's history, the real Bed Bath & Beyond is `BBBYQ`; DCP Midstream held `DPM` in
  2017 but its prices are under `DCP`. A code is accepted for a company only if (a) no other
  company takes the code over after this one's lifetime, (b) EODHD's name for the code resembles a
  name the company has had (`SecurityMaster.price_code`), and successors (Google -> Alphabet) are
  recognised by the ticker sitting in consecutive snapshots.
- **Bad prices.** EODHD bars contain absurd prints (298,098 for a $5 stock), zero prices,
  unadjusted reverse splits, and whole series with corrupt adjustment (`EE_old`). `prices.py`
  drops isolated spikes, repairs unadjusted splits (needs the splits pull), and marks a window
  `bad_data` if it holds an unexplained >6x one-day move or `adjusted_close/close` is
  inconsistent with splits+dividends. `bad_data` rows are *excluded* from every statistic. Without
  this a 279,691x "multibagger" dominated every mean and a random portfolio showed 115% CAGR.
- **Missing security types.** Massive left `type` empty on a third of 2010-2016 rows and never
  typed 1,280 dead companies; `fill_types` infers them (same ticker elsewhere, then EODHD's type).
  Without it the development period silently lost its dead companies.
- **Look-ahead in ticker resolution.** A ticker found only in the *next* snapshot is accepted
  only if the company did not already exist under another ticker (IPO yes; FB -> META no).
- **Bot / list posts.** 1,674 posts naming 31+ companies were 20% of all post mentions;
  baselines ignore posts naming more than 10 companies (`n_companies` in the view).
- **Name matching is the weakest evidence** (`Azure` matched Azure Power). Only full registered
  names are used, single words need capitalisation + finance context + not a corpus-common word,
  and the baselines use only cashtag / bare-ticker mentions by default.

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

## Results so far

**Corpus (filter v4, 67-word stoplist, all-time ticker list `data/ref/tickers_all.txt`):** 331k
posts / 6.2M comments over 5 subs. Re-filtering with every ticker ever listed (instead of the SEC's
*current* list) added 35% more posts, 62% more in `investing`: the earlier corpus had dropped posts
about companies that later died unless they used a cashtag.

| Subreddit | Posts kept | Comments kept | Kept posts <=2018 / 2019-20 / 2021+ |
|---|---|---|---|
| stocks | 141,253 of 376k (38%) | 2.76M | 33.2k / 37.6k / 70.5k |
| investing | 95,697 of 399k (24%) | 1.82M | 43.9k / 19.1k / 32.7k (the deep history) |
| StockMarket | 66,613 of 179k (37%) | 1.07M | 10.2k / 8.7k / 47.7k |
| ValueInvesting | 18,999 of 31.8k (60%) | 456k | 0.2k / 0.6k / 18.2k (recent periods only) |
| SecurityAnalysis | 8,531 of 28.4k (30%) | 74k | 4.6k / 2.1k / 1.8k (mostly link posts) |

**Market data:** 59,916 price series (2.3 GB), 17 yearly Massive snapshots, 11.3k split files.
Universe: ~3.1-3.6k eligible companies per month in 2012-2020 (price >= $1, 60-day median dollar
volume >= $200k; size buckets are liquidity quintiles because we have no shares outstanding),
835k company-month rows, 4.2M labels at 1/3/5/7/10 years. 1.72M resolved mention rows.
**Companies listed at a cohort date with no usable price series: 6,986** (5,601 not in EODHD at
all, 842 where EODHD's name for the code is a different company, 543 whose code was taken over
after delisting); they carry 3.3% of 2012-2018 stock mentions (96.7% land on priced companies),
so permanent losses are still slightly understated. State this next to any loss-lift number.

**Baseline ladder, after the survivorship fix** (top decile of scored companies per month, equal
weight, 5y horizon; `lift` = P(5x | selected) / P(5x | eligible universe), `size-m` = the same with
the universe re-weighted to the picks' liquidity mix, `loss` = the permanent-loss ratio, plain and
size-matched; `perm p` shuffles scores within month x size bucket x SIC division; CAGR is the
overlapping-lots portfolio vs SPY, which returned 16.1% in dev and 14.8% in validate):

| Scorer | dev lift | dev size-m | dev loss (size-m) | dev perm p | dev CAGR | val lift | val size-m | val loss | val CAGR |
|---|---|---|---|---|---|---|---|---|---|
| mention count | 2.25 | 2.92 | 1.05 (1.84) | 0.003 | 18.3% | 1.49 | 1.91 | 1.69 | 14.3% |
| mentions incl. comments | 2.20 | 2.92 | 0.82 (1.51) | 0.003 | 18.7% | 1.32 | 1.73 | 1.35 | 16.7% |
| sentiment (lexicon) | 0.90 | 1.00 | 1.05 (1.29) | 1.0 | 13.0% | 1.03 | 1.14 | 0.84 | 13.9% |
| sentiment x volume | 1.95 | 2.50 | 0.85 (1.43) | 0.003 | 16.4% | 1.14 | 1.47 | 1.27 | 13.3% |
| DD flair count | 1.28 | 1.45 | 1.38 (1.78) | 0.60 | 12.6% | 1.16 | 1.31 | 1.36 | 15.4% |
| evidence links | 1.69 | 2.06 | 0.90 (1.35) | 0.003 | 16.0% | 1.13 | 1.37 | 1.26 | 13.1% |
| structure proxy | 1.80 | 2.23 | 0.84 (1.31) | 0.003 | 16.0% | 1.15 | 1.42 | 1.12 | 15.0% |
| crowd-penalised structure | 0.91 | 0.99 | 1.09 (1.29) | 1.0 | 12.0% | 0.90 | 0.96 | 1.22 | 10.5% |
| 12-1 momentum | 1.65 | 1.65 | 1.55 (1.56) | 0.003 | 13.9% | 1.29 | 1.33 | 1.89 | 8.6% |
| random among mentioned | 1.37 | 1.56 | 1.25 (1.59) | 0.38 | 14.3% | 1.25 | 1.41 | 1.30 | 13.6% |

**Before vs after the survivorship fix** (mention count, dev, 5y): plain lift 2.44 -> 2.25;
loss-lift **0.65 -> 1.05**. The old "popular companies suffer fewer permanent losses" result was
mostly the bias; with the dead companies back it is gone. The size-matched loss-lift of 1.84 says
the opposite of the old story: mention-count picks lose permanently *more* often than
liquidity-matched peers (they look average in the plain number only because they are large and
liquid). The old table is kept in `data/features/ladder_before_survivorship_fix.log` (not in git).

Read this carefully: (1) a random pick among mentioned companies already has lift 1.4 / loss 1.6
size-matched: being mentioned at all means more upside *and* more ruin, so compare scorers with the
random row. (2) Mention count is still the bar to beat on upside (vs mentioned: 1.98); sentiment and
DD flair add nothing; the crowding penalty hurts. (3) Only the mention-based scorers beat SPY in dev (information ratio 0.28 and 0.34), and
none clearly does in validate (0.10 and 0.22). The deflated Sharpe is computed on returns *in excess
of SPY* with every configuration tried counted (10 baselines): mention count 0.82 dev / 0.26
validate, mentions incl. comments 0.86 / 0.34, random 0.06 / 0.30, everything else below 0.35. So
in dev mention count is probably real, and in validate it is not distinguishable from luck. (An
earlier version computed it on raw returns and gave 0.95 to random picks: any long-equity portfolio
in a bull market has a positive raw Sharpe. Do not reintroduce that.) (4) Validate
has now been seen by every baseline; compare finalists there, do not tune on it. (5) Nothing here
has touched the sealed holdout (2021-2022): it is unconsulted.

## Sanity check of the data layer (2026-10-17, before any LLM spend)

A second session read `pit.py`, `universe.py`, `prices.py`, `backtest.py`, `baselines.py`,
`stats.py`, `ledger.py`, `security_master.py` against the validation rules. Verified sound:
the point-in-time view (maturation window, comments inside it, edited-after-maturation
excluded, `edited_utc = -1` treated as edited after, no final scores), entry at the open after
the cutoff plus slippage, censored and `bad_data` rows excluded rather than counted as failures,
the Kaplan-Meier touch probabilities, the permutation test (it shuffles outcomes among the
*scored candidates* within month x size bucket, so it measures ranking skill among mentioned
companies, not "mentioned vs universe"), the sealed-holdout guard, and the ticker-reuse logic.

Findings, most important first. 1-2 change numbers in the ladder and must be done before
building the evaluator:

1. **Corpus survivorship bias (FIXED and re-run on 2026-10-17; results in the ladder above).** The download filter kept a
   post for a bare ALL-CAPS ticker only if the ticker is on the SEC's *current* list. A 2014
   post saying "RSH?" (RadioShack, dead) was dropped; the same post about AAPL was kept, unless
   it used a cashtag, a DD flair, was long, or was a discussed link. So mentions of companies
   that later died are undercounted, which tilts every Reddit scorer toward survivors: inflated
   5x lift, deflated permanent-loss lift. Mention count's dev loss-lift of 0.65 is the number to
   distrust most. Fix: `security_master.py export-tickers` (every ticker in any Massive snapshot
   plus EODHD's active + delisted stock/ETF codes), re-filter with `--tickers`, rebuild corpus,
   mentions, post features, and re-run the ladder. Result: lift5 fell (2.44 -> 2.25) and mention
   count's loss-lift rose (0.65 -> 1.05); the before/after is recorded in "Results so far".
2. **Companies without a usable price series were dropped silently.** `price_code` rejects a
   code another company took over after this one's lifetime (correct: EODHD's series under
   that code is the new holder's), and if no `CODEQ`/`CODE_old` series exists the company
   vanished from the universe *and* from every candidate set. Those are mostly dead companies,
   so permanent losses are understated on both sides of the lift. `universe.py` now logs the
   counts by reason and writes `data/features/universe_dropped.parquet`; look at the
   `code_reused_after_delisting` count and state it next to any loss-lift number. With yearly
   snapshots, a ticker reassigned within the same year looks like a *successor* (Google ->
   Alphabet); only the EODHD-name check (`name_matches`) stops the new holder's prices being
   used, so that check must stay.
3. **Plain lift conflates picking well with picking a kind of stock.** The denominator is every
   eligible stock, and Reddit talks about volatile growth and small names, which 5x (and die)
   more often, so a random pick among mentioned companies already has lift about 1.4 with a
   loss-lift above 1. `backtest.py` now also reports a **size-matched lift** (universe
   re-weighted to the picks' liquidity-bucket mix; `lift_matched` in the report and ledger).
   Read the ladder as: plain lift answers "better than a dart at the market?", matched lift
   answers "better than a dart at the same kind of stock?", and the random-among-mentioned row
   answers "does the ranking add anything to the fact of being mentioned?". All three matter.
   The S&P 500 is the right benchmark for the *portfolio* diagnostic (CAGR vs SPY, information
   ratio, deflated Sharpe), which `run` already computes; the ladder table above omitted it.
   Print the portfolio line for every baseline next time.
4. `universe.py` now prints the price data end date. Validate-period 5y labels are complete
   only for cohorts up to five years before that date; later cohorts are censored (excluded
   from fixed-horizon rates, included in the KM estimate).
5. Left as known imperfections: `blind_text` hides tickers and names but not products, CEOs
   or cities, so the blinded run understates, not eliminates, hindsight; the validate split has
   now been seen by eight baselines (compare finalists there, do not iterate on it); the
   `discussed_link` filter rule uses a post's *final* comment count to decide what to store
   (storage only; those posts reach the baselines only via name matches, which they exclude).

## Handoff: evaluation work (what is deliberately not built)

Not built, by design: the OpenAI thesis scorer and prompts, comment adjudication, causal author
credibility, embedding novelty, information-event de-duplication/clustering, the quality formula
`Q`, the fundamental-resilience gate. Everything below is ready for them.

**The contract** (`scripts/scorer.py`): a scorer has `name`, `version`, `lookback_days`,
`maturation_hours`, `params()` and `score(cohort) -> {cik: {"score": float, ...}}`. `Cohort` gives
`month`, `cutoff_utc`, `universe` (eligible companies that month: ticker, code, price, adv60, mom,
size_q) and `view` (`pit.CohortView`). Run it with
`python scripts/backtest.py run --scorer path/to/file.py:factory --split dev`.

**What the view guarantees** (`scripts/pit.py`, tested): only posts whose maturation window has
closed by the cutoff, comments only inside each post's window, nothing edited after maturation
(`edited_policy="keep"` relaxes it), and *no* `score` / `num_comments` / `upvote_ratio` anywhere
(the dumps hold end-of-life values). The view is a separate in-memory database, so even raw SQL
through `view.query(...)` cannot reach later data. Tables: `post_mentions` (one row per post x
company: text, flair, url, method, `n_companies`, text features), `comment_mentions`,
`thread_comments`. For LLM calls: `view.posts(cik)`, `view.comments_for(post_ids)`,
`entity_resolver.blind_text()` for the ticker-blinded run.

**Rules to keep** (also in "Validation rules"): run identified *and* blinded; keep every
configuration in the ledger (it is automatic); the holdout allows one look (`SealedHoldoutError`
after that; never pass `--accept-contaminated` without telling the user); labels look 1-10 years
ahead, so when tuning on dev and testing later use `ledger.embargo()`; report lift against the
random-among-mentioned row, not only against 1.0; never use Reddit's final scores as features;
posts naming many companies are bots, not theses. Read `docs/Reddit_Long_Horizon_Stock_Scouting_Full_Report.md`
before designing the scorer.

**Known limits to be honest about:** size is a liquidity proxy (no shares outstanding, so no true
market cap); sector matching uses SIC divisions (coarse: 10 groups, 3.7% of companies have no code); delisted holdings are valued at the last close (generous for bankruptcies); removed or
deleted posts are invisible, so taken-down pump content never reaches any scorer; ~4% of
mentions resolve from a later ticker snapshot (flagged `from_later_snapshot`; bare tokens in that
state are dropped); snapshots are yearly, so a ticker that changes hands twice inside one year is
not detected (tighten with `snapshots --per-year 2`); entry is the open of the first bar after the
cohort date, plus 25 bps slippage, with no market-impact model.

## Next steps

0. ~~Re-run the data layer without the survivorship bias~~ **Done 2026-10-17** (see "Results so far").
   If the filter, ticker list, resolver, price cleaning or universe change again, repeat the chain:
   `export-tickers`, re-filter `pipeline`, `corpus.py`, `entity_resolver.py wordstats` and `mentions`,
   `universe.py` (twice, splits pull in between), `baselines.py`, then the ladders. Cached scores are
   keyed on the data files, so they cannot go stale.
1. **User actions:** add `OPENAI_API_KEY` to `.env` when the scorer is built; the EODHD
   subscription can be cancelled once no more symbols are needed (everything is cached; 2025+
   bars were not needed). SIC sectors are done (`data/ref/sic.parquet`, needs `SEC_USER_AGENT`
   only if it is ever re-pulled).
2. **Decisions for the user:** add wallstreetbets (last, postponed); backfill 2025+ via
   `arctic` (the corpus ends 2024-12, so cohorts end there too); the sealed holdout (2021-2022)
   is untouched: decide who consults it and when.
3. **Build the evaluation scorer** against the contract above, then run it on `dev`, compare to
   the ladder, check `validate`, run blinded vs identified, and only then (once) the holdout.
4. Phase 2 (later): fundamental-resilience gate (SEC XBRL), forward data from non-Reddit
   sources, Postgres/pgvector, alerts, dashboard.
