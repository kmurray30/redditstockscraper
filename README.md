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

# 1. Exact sizes for the chosen subs, read from the torrent metadata (no data downloaded)
python scripts/download_reddit.py list --subs stocks investing wallstreetbets SecurityAnalysis ValueInvesting StockMarket

# 2. Download only those files (each is the sub's full 2005–2024 history)
python scripts/download_reddit.py torrent --subs stocks investing SecurityAnalysis ValueInvesting --out data/raw/reddit

# 3. Optional: fill 2025+ from Arctic Shift as monthly files (resumable)
python scripts/download_reddit.py arctic --subs stocks investing --after 2025-01-01 --before 2026-10-01

# 4. See how a file's records and bytes split by year
python scripts/download_reddit.py stats data/raw/reddit/*/stocks_comments.zst
```

The torrent contains one file per subreddit per kind (submissions and comments), each
covering the full history. You therefore choose **which subreddits** to download, not
which years. Year filtering happens later, at ingest, so only the years you keep take
up space in the research database.
