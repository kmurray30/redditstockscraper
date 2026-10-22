# Reddit long-horizon stock scout: project context for Claude

Read this first. It is the handoff between sessions. The data layer, point-in-time machinery
and backtest engine are built and tested; what remains is the **evaluation logic** (LLM thesis
scoring etc.), which plugs in through the `Scorer` interface (see "Handoff: evaluation work").

## How to work with this user

- **Run things yourself.** The data now lives on Railway (see "Hosted worker" below), not on the
  user's Mac. The user does not want to run things locally any more. Start jobs on the worker and
  read their logs yourself. Do not ask the user to paste output. Ask only for real decisions.
- When you *do* give the user commands, give **one command per code block**; combine only if
  there is almost no risk of error. No walls of commands.
- The user's machine is macOS, Homebrew, pyenv Python 3.11, repo at `~/Repos/redditstockscraper`.
  It may still hold an old copy of `data/`; the Railway volume is the source of truth.
- Work on branch `claude/confident-gates-n5noak`. **Do not open a PR** unless asked. Commit
  messages end with the `Co-Authored-By: Claude ...` trailer the harness specifies.
- Never print or commit API keys; they live in the gitignored `.env`. Do not send the user's
  email or any contact string to a service unless they ask (the SEC wants one: see Next steps).
- Explain results plainly, say when something is unverified, and own mistakes. The earlier
  session made several wrong guesses about aria2c that the user paid for in retries.

## Hosted worker (Railway)

- Railway project `redditstockscraper`, service `worker`, built from this repo and branch with
  the `Dockerfile` (Python 3.11). One volume is mounted at `/app/data`, so every script's
  default `data/...` path lands on it. The service has **no public domain and no TCP proxy**.
  Keep it that way: the Reddit data must never be on a public endpoint.
- The start command is `scripts/jobs.py`. Request a job by setting service variables (this
  redeploys): `JOB` = a preset (`tests`, `du`, `df`, `export-tickers`, `refilter`, `corpus`,
  `wordstats`, `mentions`, `universe`, `splits`, `baselines`, `edgar`, `ladder-dev`,
  `ladder-validate`, `run`, `rebuild` = the whole chain, `pull-data`, `verify-data`,
  `bucket-status`) or `script:<name>`; `JOB_ARGS` = extra
  arguments (`run` needs `--baseline X` or `--scorer scripts/file.py:factory`); and `JOB_ID` =
  a new string. Then read the result with the Railway `get-logs` tool. Logs also persist on the
  volume in `data/logs/`, with a run index in `data/logs/jobs.jsonl`.
- A `JOB_ID` runs once. A redeploy, for example a push to the branch, does not repeat a finished
  job, but it **kills a running one**, which then re-runs from the start. Do not push while a
  long job is running. Queued deployments all run eventually, each with the variables current
  at its start, so an older build may pick up a newer `JOB` first; a refused job no longer
  consumes its `JOB_ID`. With slow queues, bundle steps into one preset (`gold-eval-both`,
  `pilot`) and change variables with `skipDeploys` before the push that deploys them.
- **Railway can pause deploys for Hobby accounts** ("Deployments paused - limited access": Pro
  demand is prioritised; seen 2026-10-21). `set-variables`, pushes and `redeploy` then create
  no deployment (redeploy errors with "Deploys have been paused temporarily"); queued deploys run
  on their own when it lifts, nothing is lost, running containers keep running. Check with
  `list-deployments`; the only way around it is the Pro plan (user's call).
- Anything touching the holdout split or `--accept-contaminated` is refused unless the user sets
  `ALLOW_HOLDOUT=1` themselves.
- Secrets (`EODHD_API_KEY`, `MASSIVE_API_KEY`, `SEC_USER_AGENT`, later `OPENAI_API_KEY`) are
  service variables set by the user in the dashboard. **Never call `list-variables`**, because
  it prints them. Set non-secret variables with `set-variables` only.
- No torrent client in the image. Downloading wallstreetbets or Arctic Shift from Railway has
  not been checked against Railway's terms; ask the user first.
- **Moving data: the private bucket `data-backup`** (S3-compatible, $0.015/GB-month, free
  transfer, no public access). Use `scripts/bucket_sync.py`, with `status`, `push`, `pull`
  and `verify`, `--only FOLDER`, and `--dry-run`. It resumes by skipping same-size files and
  ends with a size check of every file. The Mac pushes `data/` up with keys in its `.env`. The
  worker pulls with `JOB=pull-data`, and `JOB=verify-data` / `bucket-status` show the state.
  The worker's `S3_*` variables are references to the bucket's own variables, so no key passes
  through a session. The bucket doubles as the volume's backup. To back up new results, push
  from the worker with `JOB=script:bucket_sync JOB_ARGS="push --only features"` or similar.
- `tar | railway ssh` works but is silent and was measured at about 0.1 MB/s. Do not suggest it
  for bulk data.
- Cost: the volume is $0.15/GB-month. The idle container costs cents. A job is billed only for
  the RAM and CPU it uses while it runs.
- **Data verified on the volume 2026-10-18:** 71,638 files pulled from the bucket with matching
  sizes, and `ladder-dev` on the worker reproduced the dev ladder below to the last digit in
  548 s. `LLM_MODEL=gpt-5-nano` and `LLM_SPEND_CAP_USD=5` are set; the user sets the keys.

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
  **Exception by user decision (2026-10-19): the thesis pipeline has no lock** (see "Thesis
  scorer", "No lock"); the baselines keep the 24h window.
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

Tests: `python -m pytest -q` (309 tests, ~5 s, no network). `requirements.txt`: zstandard,
orjson, pytest, pyarrow, duckdb, numpy, boto3. `data/` and `.env` are gitignored.

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

## Thesis scorer (built 2026-10-18; the LLM evaluation layer)

Design decided with the user in chat. Read `scripts/thesis_scorer.py`'s docstring for the maths.

**Pipeline, all worker jobs** (`JOB=...`, `JOB_ARGS` passthrough):

| Job | What | Spends? |
|---|---|---|
| `census` (`thesis_funnel.py`) | selects threads, writes `data/features/threads.parquet`, prints the funnel, the cost estimate and 10+10 samples | no |
| `gold-build` (`goldset.py build [--dump]`) | 30 single-company dev threads (company in the title preferred) to `data/gold/{threads.md,ratings.csv}`, pushed to the bucket; `--dump` prints both files through the log so `gold_page.py` can build the phone rating page | no |
| `gold-eval` (`goldset.py evaluate`) | grades the rated threads with `why`, prints your rating vs the model's Q and the rank correlation | cents |
| `llm-run` (`thesis_llm.py run --variant identified|blinded [--years ...] [--limit N] [--dry-run]`) | grades the planned threads, cached per thread under `data/llm/cache/<model>/<prompt>/<variant>/` | yes, capped |
| `llm-collect` (`--variant`) | cache -> `data/features/thesis_threads_<variant>.parquet` | no |
| `check-blind` | greps the blinded texts for the company's ticker or name; must report 0 | no |
| `thesis-run` (`backtest.py run --scorer scripts/thesis_scorer.py:factory --split dev`) | the backtest; `THESIS_VARIANT`, `THESIS_PARAMS` (JSON), `THESIS_VOTES` select the configuration | no |
| `thesis-timeline` | per-company score series after every thread event, `thesis_series_<variant>.parquet`, for graphs | no |
| `calibrate --split dev` | P(5x), P(10x), P(permanent loss) and CAGR quartiles per score decile (`--exclude-late` before `calibrate` for the check) | no |
| `gold-eval-both` | `gold-eval` on `LLM_MODEL`, then on `jev-1.13.0` (`evaluate --model`), one deployment | cents |
| `pilot` | `llm-run` 2014+2017 identified and blinded, `llm-collect --years`, `check-blind`, `thesis-run --years 2014 2017` both variants; one deployment, grader = `LLM_MODEL` | yes, about $0.6 on Jev |
| `script:comment_lag` | how long after a post its comments and edits arrive, per year | no |

**Variables the user sets on the worker:** `LLM_MODEL` (`gpt-5-nano`, or `jev-1.13.0`; `fake`
is a free deterministic backend for dry runs), `OPENAI_API_KEY`, `JEV_API_KEY` (set by the user
2026-10-20; TypeSafe's own key, direct endpoint `api.typesafe.ai/v1/systemone`), `LLM_SPEND_CAP_USD`
(default 5). I switch `LLM_MODEL` between jobs with `set-variables`; caches are per model.
**Jev is not a chat model** (an earlier note here was wrong): it answers typed questions over a
block of text, a `score` on 2-10 ordered rubric levels, a `noul` yes-probability or a `choice`,
with no text output. `thesis_prompt.jev_questions()` restates the prompt's grades as rubrics
(five levels each, x25 to 0-100; voices 0-9; stance five levels to -1..1), `decode_jev` maps
the answers back into the same JSON the chat model returns, raw probabilities kept under `_jev`,
so collect, scoring and the gold evaluation are backend-agnostic. No `why` column from Jev.
Input $0.042/M, output free, so a full dev variant is about $1 vs $1.34 on nano. The route
contract (`llm.JevClient`) follows public references of Sept 2026 and is mock-tested only; the
first live call is the test. `OPENROUTER_API_KEY` reaches Jev too (alpha decisions endpoint). Prices are a table in `llm.py` from third-party trackers (Oct 2026); an unknown
model needs `LLM_PRICE_IN`/`LLM_PRICE_OUT`. Every call's real cost goes to `data/llm/spend.jsonl`;
a run is refused up front if its estimate would cross the cap and stops mid-run if real spend does.

**The funnel** (`thesis_funnel.py`): a (post, company) row is a candidate if the mention is a
cashtag or clean/contextual bare ticker and the company is a subject of the post (cashtag, in
the title, or mentioned twice), the post names <= 2 companies (or <= 6 with this company in the
title or the most mentioned), the author is not deleted, the post has substance (self post >=
400 chars, a link post with >= 200 chars or a primary-source link, or a thin post with >= 15
comments in the window and >= 10 votes), and the company is in the eligible universe that month. At every monthly
cutoff the top 3 per company in the 90-day view by a pre-score (percentile ranks of length,
primary/news links, DD flair, log votes) are selected; the scored set is the union over cutoffs.
Point-in-time by construction.

**Votes are used** (user decision): the view has `votes="final"` (`pit.PITConfig`), exposing the
dumps' final post and comment scores as `votes`. Reddit archived posts after six months until
2021-09-29 (confirmed from the r/modnews announcement), so for dev and validate a final score is
a six-month score; the leak is the window between maturation (24h) and archive. **For 2021+
(holdout, forward) votes may include much later ones**, subreddit by subreddit: say so next to
any holdout number. Votes enter in three places: comment pruning/weighting inside a thread, the
funnel pre-score, and the thread weight `(1 + log(1 + votes))^gamma` (gamma 0.5 base, 0 and 1
as ablations). `votes` is in every ledger entry.

**No lock** (user decision 2026-10-19, "Indefinite"): the thesis pipeline's view is
`thesis_prompt.thesis_pit_config()`: `maturation_hours=0`, `edited_policy="keep"`,
`comments="all"` (`pit.PITConfig`). A post counts from the first monthly cutoff after it was
written, the model sees every comment the thread ever got (including ones written years
later) and the post's final, possibly edited text. Measured with `comment_lag.py` on
2012-2020: 88% of comments arrive within 24h of the post, 95% within 48h, 98.4% within 7 days;
0.3% of long self posts are edited after 7 days. The funnel records per thread `decided_utc`
(first cutoff), `late_comments`, `late_shown` (late comments the prompt actually shows) and
`edited_late`. **Kept by default; `exclude_late` drops threads with `late_shown > 0` or
`edited_late`**: `THESIS_PARAMS='{"exclude_late": true}'` for `thesis-run`, `JOB_ARGS="--exclude-late
calibrate --split dev"` for `calibrate`. Run both and report the gap next to any thesis
result: a large gap means hindsight in late comments is doing the work. Dropping late threads
also drops the most discussed ones, so the exclude run is a check, not a clean counterfactual.
The baseline ladder is unchanged (24h window). Builder version 2 marks this in the prompt version.

**The prompt** (`prompts/thesis_v1.md`, built by `thesis_prompt.py`): grades reasoning, sourcing
and tone only; numbers-only JSON (`is_thesis`, `stance`, `horizon`, argument, evidence,
specificity, tone_stability, falsifiability, balance, hype_risk, comment corroboration, confidence).
Usernames are never sent (u1, u2, OP). Body capped at 8k chars (head+tail), comments at 3.6k chars,
best-voted first, two levels deep. Blinded = `entity_resolver.blind_text` on everything. The prompt
version is a hash of the prompt text plus a builder version; it is in the cache path and the ledger.
**Prompt changes are tuned on the gold set only, never on backtest numbers.**

**Scoring** (`thesis_scorer.py`, weights tuned on the gold set 2026-10-22, see below): `Q =
100*(.25 argument + .15 evidence + .05 tone + .05 falsifiability + .20 balance + .30 discussion)
- 10*hype`, with `discussion = (independent_support + independent_opposition +
strongest_objection_severity)/3`: how much substantive, independent discussion the thread drew,
whichever way it cut (direction lives in `stance`). Specificity is out of Q. No thesis gate by
default (`thesis_gate` none | hard | soft): a question post with educated comments still counts,
and its small stance keeps its contribution small. `x = stance*(Q/100)^2*confidence*vote_weight`;
company score at a cutoff = sum of visible threads' `x` decayed by half-life per stated horizon
(days 14, weeks 30, months 90, years 180, unclear 60). Params (all in the ledger): `gamma`, `beta`
(price-since-thesis discount `(P_thesis/P_now)^beta`, capped at 1, base 0), `use_author` (causal
shrunk author prior, base off), `min_evidence` gate (base 0), `use_comments`, `corroboration`
(`discussion` | `net`), `thesis_gate`, `use_specificity`, `exclude_late`. The first formula
(`WEIGHTS_V1`, net corroboration, hype 20, hard gate, specificity in) is reachable through those
params. Bear theses push the score down. Expected value is reported as calibrated bucket rates
(`calibrate`), not as one number.

**Gold-set evaluation (2026-10-22, 30 threads rated 1-10 by the user, $0.011 total):**

| Grader | Spearman with the ratings, first formula | after retuning | notes |
|---|---|---|---|
| gpt-5-nano (reasoning minimal) | 0.21 (Pearson 0.40) | 0.46 | `stance` came back 0 on 27 of 30 threads: unusable as the direction signal |
| **Jev 1.13 (direct API)** | 0.43 (Pearson 0.48) | **0.72** | decisive stances; `is_thesis` false on 11 of 30, including threads rated 7 (hence no hard gate) |

Per-component rank correlations with the ratings (Jev / nano): balance .74/.35, argument
.52/.17, strongest objection .51/.38, independent opposition .48/.39, independent support
.41/.47, evidence .33/.16; specificity, tone, falsifiability, hype all within +-0.17; raw votes
-0.04. The user's stated basis ("comments are what sanity-check an emotionally biased post")
is what the numbers show: the comment grades and balance carry the rating, direction does
not (opposition correlates *positively*). The retuning (`goldset.py evaluate` writes
`data/gold/eval_<model>.csv`; the grid over weights is flat, mean rho 0.61-0.63 across the top
dozen combinations, worst 0.42) is on 30 points, so treat the exact weights as a starting
point, not a result. **Decision: Jev is the grader.** The gold sample matches the planned set
on comments (median 8; 43% with >= 10, 19-20% with >= 25) and votes; gold posts are longer.
The corpus, not the sample, is thin on discussion.

**Pilot on Jev (2026-10-22; dev cohorts of 2014 and 2017 only, 24 months, 3,022 threads graded
per variant in 80 s each, $0.92 lifetime spend, check-blind 0 of 3,022; top decile per month =
538 company-months over 188 companies; `pilot` preset):**

| Variant | 5y lift (95% CI) | size-matched | vs mentioned | 10x lift | loss lift (size-m) | perm p | 3y lift (size-m, perm p) | CAGR vs SPY | IR | DSR |
|---|---|---|---|---|---|---|---|---|---|---|
| identified | 2.68 [1.96, 3.47] | 3.37 | 1.65 | 5.28 | 0.92 (1.47) | 0.007 | 3.22 (5.22, 0.003) | 12.0% vs 12.3% | 0.06 | 0.31 |
| blinded | 2.56 [2.02, 3.12] | 3.21 | 1.56 | 4.94 | 0.95 (1.51) | 0.007 | 2.58 (4.15, 0.007) | 12.1% vs 12.3% | 0.07 | 0.32 |

Read: (1) the blinded run is within noise of the identified one, so hindsight from seeing the
company name is not what makes the ranking work; (2) the ranking adds about 60% over a random
pick among mentioned companies in those months (vs mentioned 1.65 / 1.56) and its permanent-loss
rate is below the universe's (0.92) where mention count's is above (1.05); (3) the equal-weight
portfolio only matches SPY and the median pick is 42% behind the market at 5y: the lift is a
few multibaggers, which is the asymmetric profile the project wants, not a portfolio; (4) these
two years are not the ladder's 2012-2018, so compare with `pilot-checks` (every baseline on the
same years, and `exclude_late` both variants), not with the ladder table; (5) Jev called 799
threads theses when it saw the name and 941 when blinded. 1y rows are empty (no 5x in a year).

**Like-for-like rows on the pilot's years (`pilot-checks`, 2026-10-22; dev 2014+2017, 5y, top
decile per month; baselines pick from every mentioned company (1,874 company-months), the thesis
scorer only from companies with a graded thread (538), so its candidate pool is already better
(5x rate 7.3% vs 5.4% for all mentioned), which is why its "vs mentioned" is lower while its
plain and size-matched lifts are higher):**

| Scorer, 2014+2017 | lift | size-m | vs mentioned | loss (size-m) | perm p | CAGR vs SPY 12.3% |
|---|---|---|---|---|---|---|
| mention count | 2.39 [1.87, 2.96] | 3.04 | 1.99 | 1.14 (1.90) | 0.003 | 12.3% |
| mentions incl. comments | 2.27 | 2.95 | 1.87 | 0.83 (1.47) | 0.003 | 12.4% |
| sentiment (lexicon) | 0.88 | 0.99 | 0.64 | 0.83 (1.04) | 1.0 | 7.9% |
| sentiment x volume | 2.07 | 2.63 | 1.68 | 0.95 (1.55) | 0.003 | 11.3% |
| DD flair count | 1.40 | 1.60 | 1.07 | 1.29 (1.64) | 0.91 | 8.1% |
| evidence links | 1.85 | 2.29 | 1.47 | 0.70 (1.08) | 0.003 | 10.8% |
| structure proxy | 1.97 | 2.46 | 1.58 | 0.65 (1.02) | 0.003 | 11.3% |
| crowd-penalised structure | 0.87 | 0.95 | 0.64 | 1.00 (1.16) | 1.0 | 7.4% |
| 12-1 momentum | 1.63 | 1.61 | 1.87 | 1.79 (1.75) | 0.003 | 7.8% |
| random among mentioned | 1.47 | 1.67 | 1.13 | 1.25 (1.59) | 0.48 | 8.1% |
| **thesis, identified** | 2.68 [1.96, 3.47] | 3.37 | 1.65 | 0.92 (1.47) | 0.007 | 12.0% |
| **thesis, blinded** | 2.56 [2.02, 3.12] | 3.21 | 1.56 | 0.95 (1.51) | 0.007 | 12.1% |
| thesis, identified, late content excluded | 2.96 [2.21, 3.81] | 3.73 | 1.84 | 0.96 (1.54) | 0.003 | 12.1% |
| thesis, blinded, late content excluded | 2.81 [2.26, 3.34] | 3.50 | 1.73 | 0.92 (1.46) | 0.003 | 12.3% |

Read: the thesis scorer beats mention count on plain and size-matched upside (2.68 vs 2.39,
intervals overlap) and clearly on ruin (loss 0.92 vs 1.14, size-matched 1.47 vs 1.90); dropping
the threads that hold late comments *raises* the lift, so hindsight in late discussion is not
what works; blinded stays within noise of identified; no scorer beats SPY on these two cohort
years. Decision 2026-10-22: run the full dev (`full-dev` preset, about $2 on Jev).

**Census of dev (2026-10-18, funnel v3):** 9,429 unique threads over 84 cohorts (662 in 2012
rising to 1,929 in 2018), 12.1M estimated input tokens; at GPT-5 nano $1.28 per variant, $2.57
for identified + blinded. Rejects per cohort row: passing_mention 85k, not_in_universe 64k,
many_companies 62k, thin_self_post 52k, unreliable_mention 35k, deleted_author 17k, bare_link
12k, over_cap 10k. History: funnel v1 (<= 2 companies, no discussion rule) gave 10,172 threads
and rejected a 10k-char Valeant DD for naming competitors; v2 (focus + discussion rules) gave
12,854 but its gold sample held threads where the ticker was a false or passing mention ("ATR"
in a volatility post, "END" in a portfolio update); v3 requires a cashtag, the ticker in the
title, or two mentions.
The census job peaks at about 8.7 GB RAM (the 90-day views); keep that in mind on Hobby.

**Gold set rating page (`gold-ui`, a second Railway service):** `scripts/gold_server.py`
serves the page built by `scripts/gold_page.py` (one thread per card, 1-10 buttons, reason box; 1-5 ratings from before 2026-10-19 are mapped onto 1-10 by `gold_page.to_ten`)
behind HTTP Basic auth (`GOLD_PASSWORD`, chosen by the user) at
https://gold-ui-production-98fb.up.railway.app. It reads `gold/threads.md` and `gold/ratings.csv`
from the bucket at start and rewrites `gold/ratings.csv` in the bucket after every rating, so
the worker reads the same file: `pull-data --only gold`, then `gold-eval`. **This is the one
deliberate exception to "no public endpoint"**: the user asked for a phone page with just a
password; the service holds only the 30 gold threads (about 110 KB), never the dataset, and it
is separate from the worker so the worker keeps no domain. Tear it down with `delete-service`
once the gold set is rated. (An earlier claude.ai artifact version of the page,
https://claude.ai/artifact/S1GTrszHG1kJtAjtxd682y, only saves when its owner is signed in and is
no longer the path.)

**Budget and order** ($5 total, user decision): ~~census -> gold set -> gold-eval -> pick~~ done
2026-10-22 (Jev; $0.011 spent) -> `pilot` (one job; the cache makes the full run pay only for
the other years) -> `thesis-run` next to the ladder -> full dev both variants (about $2 on Jev)
-> free ablations (gamma, beta, use_author, min_evidence, no comments; `gamma=0` is the
no-votes ablation of the aggregation, though the prompt still showed vote counts) -> validate
for the one finalist. Holdout untouched. Model choice: GPT-5 nano (about $0.05/M in, $0.40/M out,
reasoning_effort minimal) and Jev 1.13 ($0.042/M in, output free, early access) cost about the
same for this workload; the gold set decides.

## Next steps

0. ~~Re-run the data layer without the survivorship bias~~ **Done 2026-10-17** (see "Results so far").
   If the filter, ticker list, resolver, price cleaning or universe change again, repeat the chain:
   `export-tickers`, re-filter `pipeline`, `corpus.py`, `entity_resolver.py wordstats` and `mentions`,
   `universe.py` (twice, splits pull in between), `baselines.py`, then the ladders. Cached scores are
   keyed on the data files, so they cannot go stale.
1. **User actions:** set `LLM_MODEL`, `OPENAI_API_KEY` and `LLM_SPEND_CAP_USD` on the worker
   (Railway Variables) before `gold-eval`; rate `data/gold/ratings.csv`; the EODHD
   subscription can be cancelled once no more symbols are needed (everything is cached; 2025+
   bars were not needed). SIC sectors are done (`data/ref/sic.parquet`, needs `SEC_USER_AGENT`
   only if it is ever re-pulled).
2. **Decisions for the user:** add wallstreetbets (last, postponed); backfill 2025+ via
   `arctic` (the corpus ends 2024-12, so cohorts end there too); the sealed holdout (2021-2022)
   is untouched: decide who consults it and when.
3. **Run the thesis scorer** (built; see "Thesis scorer"): `census` -> user rates the gold set ->
   `gold-eval` -> pilot -> full dev -> ablations -> validate once. Record the ladder rows,
   calibration table, blinded-vs-identified gap and spend here.
4. Phase 2 (later): fundamental-resilience gate (SEC XBRL), forward data from non-Reddit
   sources, Postgres/pgvector, alerts, dashboard.
