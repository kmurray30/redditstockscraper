# redditstockscraper

A research tool for finding long-horizon stock theses in Reddit discussions. It scores
the quality of each discussion, not its sentiment, and checks the scores against
multi-year returns in a strictly point-in-time backtest.

## Getting the historical Reddit data

Reddit no longer gives personal projects API access. Historical data comes from
community archives instead:

| Source | Covers | How |
|---|---|---|
| Watchful1 per-subreddit dumps (Academic Torrents) | 2005-06 – 2024-12 | `torrent` command (needs `aria2c`) |
| Arctic Shift API | 2025+ | `arctic` command |

**Data-use note:** Reddit doesn't license either source. Keep the data private, don't
redistribute it, and use it only for research and LLM inference (no model training).

```bash
pip install -r requirements.txt
brew install aria2          # or: sudo apt install aria2

# 0. Test each external service once (torrent index, Arctic Shift, SEC, aria2) - run this first
python scripts/download_reddit.py check

# 1. Exact sizes from the torrent metadata (no data downloaded)
python scripts/download_reddit.py list --subs stocks investing SecurityAnalysis ValueInvesting StockMarket

# 2. Sanity-check the relevance filter on one month of a sub before deleting anything
python scripts/download_reddit.py preview --sub stocks --month 2021-03

# 3. Download + filter, one file at a time (raw file deleted after filtering).
#    Use --keep-raw on the first run so you can re-filter without re-downloading.
python scripts/download_reddit.py pipeline --subs SecurityAnalysis --keep-raw
python scripts/download_reddit.py pipeline --subs stocks investing ValueInvesting StockMarket

# 4. Optional: 2025+ from Arctic Shift, same filter; comments only for kept threads
python scripts/download_reddit.py arctic --subs stocks investing --after 2025-01-01 --before 2026-10-01
```

### Checking the result

```bash
python scripts/download_reddit.py report --all
```

One paste covering every finished subreddit: keep/drop rates by reason, the most-mentioned
tickers, sample titles, comments per post by year, and a summary table (kept posts before 2019,
2019-20, 2021+). `--sub NAME` does a single subreddit.

Tickers that are mostly written in lowercase or Title case (`LOT`, `YOU`, `LINK`...) are flagged as
"probably ordinary words". `--write-stoplist data/ref/stoplist.txt` saves them (merged with what is
already in the file) so you can re-run with `pipeline --stoplist data/ref/stoplist.txt`.

The filter also keeps **link posts with discussion** (articles, letters, podcasts with at least
5 comments, change with `--min-link-comments`), since those often name a company without a
ticker, and drops removed/deleted comments. Changing the filter version or these settings
re-runs finished subreddits the next time you run `pipeline`.

### Ticker false positives

The crude matcher counts an ALL-CAPS word as a ticker if it's on the SEC list, so business
abbreviations can slip through (`VC`, `IP`, `IR`, `NPV`, `CF`, `III`...). Many are already
stoplisted, and text written mostly in capitals only matches `$cashtags`. To add your own, put one
word per line in a file and pass `--stoplist myfile.txt` to `pipeline`; changing it re-runs finished
subreddits. A stoplisted word still counts when written as a cashtag (`$IP`).

### How raw downloads are kept clean

Torrent files are stored back to back and aria2c downloads whole pieces, so fetching one file also
writes the edges of its neighbours (sometimes producing a neighbour with its full length but mostly
empty), and aria2c refuses to start in a torrent folder that exists without its own progress file.
So every file is downloaded in its own scratch folder, `data/raw/reddit/.work/<sub>_<kind>/`. Only
a file that aria2c finished, whose size matches, is moved into `data/raw/reddit/reddit/...` and
given a `<file>.ok` marker; the scratch folder, with all the spill and progress files, is then
deleted. `clean-raw` removes scratch folders and any unverified files left by older versions; it
refuses (unless you pass `--force`) while an interrupted download is waiting to be resumed.

### Don't delete raw files by hand

Let the pipeline (or `clean-raw`) do the deleting; the `.ok` markers and scratch folders are how it
knows what it can trust.

Two aria2c messages look like errors but are normal: `Torrent not registered with Academic
Torrents` (peers come from other sources) and `Checksum error detected` right after an integrity
check (it just means those pieces still need downloading). It is working while the progress line
(`DL:` speed and `ETA`) keeps moving. A small file can show a bigger total, for example 64 MiB for
an 8.6 MB file, because torrents transfer whole pieces.

### Stopping and resuming

Ctrl-C is safe at any point. Outputs are written to temporary `.part` files and renamed only
when complete, finished subreddits are skipped, and an unfinished torrent download resumes
from its `.aria2` control file. Rerun the same command to carry on.

### If something fails

Run `check`: it prints PASS/FAIL per service with the server's own error message.

- **Torrent index won't download (404):** the script tries several URL forms, then a magnet
  link through `aria2c`. If both fail, open
  <https://academictorrents.com/details/1614740ac8c94505e4ecb9d88be8bed7b6afddd4> in a
  browser, download the `.torrent`, and pass it with `--torrent /path/to/file.torrent`.
- **Arctic Shift rejects a request (4xx):** the message includes the API's reason and the
  exact URL; paste it into an issue and the parameters can be adjusted.
- **Arctic Shift says "Timeout. Maybe slow down a bit" (HTTP 422):** it's a free, best-effort
  service and its queries time out under load. The script retries automatically with
  backoff and smaller pages. If it keeps failing, raise `--delay` (e.g. `--delay 3`) or try
  at a quieter time.
- **SEC ticker list refused (403):** pass your own list with `--tickers myfile.txt`
  (one symbol per line).

### What `pipeline` does

For each subreddit:

1. Downloads just `<sub>_submissions.zst`.
2. Streams it through the relevance filter (`scripts/reddit_filter.py`). A post is kept if it
   has a ticker (`$NET`, or `NET` when it's a known ticker and not a common word), a DD- or
   analysis-type flair, or a long write-up with finance vocabulary. Megathreads, image posts,
   removed posts and chatter are dropped.
3. Deletes the raw file, then does the same for `<sub>_comments.zst`, keeping only comments
   in kept threads.

Before anything is dropped, ticker mentions are tallied per day across **all** posts and
comments. Later, crowd-saturation and mention baselines need those counts.

Output goes to `data/corpus/reddit/<sub>/`:

| File | Contents |
|---|---|
| `posts.zst`, `comments.zst` | Kept records, slimmed to the fields we use |
| `mentions_posts.csv`, `mentions_comments.csv` | `date,subreddit,ticker,source,count` |
| `manifest.json` | Filter version, year range, kept/dropped counts by reason, output sizes |

Disk peaks at the largest single raw file. Re-running skips finished subreddits; a run
interrupted during comments resumes without re-filtering posts. Changing the filter means
bumping `FILTER_VERSION` and re-downloading, unless you used `--keep-raw`.

The ticker list defaults to SEC's current tickers (cached in `data/ref/`). Delisted names
are still caught as cashtags; pass `--tickers file.txt` for a better list.

### Which years

Each torrent file holds a subreddit's whole history. The pipeline keeps 2005–2024 by
default (`--from-year`/`--to-year`).

- **How far back:** r/investing starts around 2008, r/stocks and r/SecurityAnalysis around
  2009–10, r/ValueInvesting around 2011, and r/wallstreetbets in 2012. There is little volume
  before ~2016, so keeping it is cheap. The real limit is price history: Massive's mid tier
  reportedly covers about 10 years (to ~2016), and the higher tier 20+.
- **2024 is only partly usable for returns.** As of late 2026, a 5-year outcome needs posts
  before ~Oct 2021, and 3-year needs posts before ~Oct 2023. 2024 posts give only 1–2 year
  outcomes, and anything that hasn't yet hit 5x/10x has to be treated as still running.
- **Keep 2023–25 anyway.** Posts written after the LLM's training cutoff are the cleanest
  check that the scorer isn't using hindsight.
- **Suggested split:**

  | Years | Use |
  |---|---|
  | 2012–2018 | Build the model |
  | 2019–2020 | Tune it |
  | 2021–2022 | Sealed holdout, used once |
  | 2023–2025 | LLM hindsight check and early forward test |

## Research pipeline (after the Reddit data is filtered)

Corpus to Parquet, market data, a point-in-time security master, an entity resolver, a causal
backtest engine and deterministic baselines are built and tested. The command order, the data-
quality traps found along the way, the baseline results and the interface for plugging in a
real scorer are in [CLAUDE.md](CLAUDE.md). Quick start once the data exists:

```
python scripts/backtest.py ladder --split dev
```
