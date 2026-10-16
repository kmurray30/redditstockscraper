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

### If something fails

Run `check`: it prints PASS/FAIL per service with the server's own error message.

- **Torrent index won't download (404):** the script tries several URL forms, then a magnet
  link through `aria2c`. If both fail, open
  <https://academictorrents.com/details/1614740ac8c94505e4ecb9d88be8bed7b6afddd4> in a
  browser, download the `.torrent`, and pass it with `--torrent /path/to/file.torrent`.
- **Arctic Shift rejects a request (4xx):** the message includes the API's reason and the
  exact URL; paste it into an issue and the parameters can be adjusted.
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
