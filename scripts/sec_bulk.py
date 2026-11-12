#!/usr/bin/env python3
"""Check the SEC's bulk XBRL downloads before pulling any of them. Read only.

    python scripts/sec_bulk.py check        (needs SEC_USER_AGENT; never printed)

Prints, for each candidate bulk file, whether it exists, its size and when it was last modified:
the nightly ``companyfacts.zip`` (every company's XBRL facts, the same JSON ``fundamentals.py``
reads one company at a time), ``submissions.zip`` (filing histories), and the quarterly Financial
Statement Data Sets (every 10-K / 10-Q / 20-F value as filed, one zip per quarter).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request

CANDIDATES = [
    "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip",
    "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2009q1.zip",
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2009q2.zip",
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2012q1.zip",
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q2.zip",
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q3.zip",
    "https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2019Q1.json",
]


def probe(url: str, ua: str, opener=urllib.request.urlopen) -> dict:
    """Size and date of ``url`` from a one-byte range request (some SEC hosts refuse HEAD)."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Range": "bytes=0-0",
                                               "Accept-Encoding": "identity"})
    try:
        with opener(req, timeout=60) as r:
            total = (r.headers.get("Content-Range") or "").rpartition("/")[2] or r.headers.get("Content-Length")
            return {"status": r.status, "bytes": int(total) if total and total.isdigit() else None,
                    "modified": r.headers.get("Last-Modified")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "bytes": None, "modified": None}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"status": f"error {e}", "bytes": None, "modified": None}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("cmd", choices=["check"])
    p.parse_args(argv)
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise SystemExit("SEC_USER_AGENT is not set")
    for url in CANDIDATES:
        r = probe(url, ua)
        size = f"{r['bytes'] / 1e6:,.1f} MB" if r["bytes"] else "size unknown"
        print(f"{r['status']}  {size:>14s}  {r['modified'] or '':29s}  {url}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
