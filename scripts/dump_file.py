"""Print small files from the volume through the deploy log, as numbered base64 chunks.

    python scripts/dump_file.py data/gold/threads.md data/gold/ratings.csv [--chunk 4000]

A session that drives the worker through Railway's tools can read logs but not files, so
this is the way to fetch a small text file (the gold set, a report) without a public
endpoint. Each line is ``DUMP <name> <i>/<n> <base64>`` and the last line per file is
``DUMP-END <name> <sha1> <bytes>``, so the reader can check it got everything.
Reassemble with ``reassemble(lines)`` below. Not for anything large.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path


def chunks(name: str, data: bytes, chunk: int) -> list[str]:
    b64 = base64.b64encode(data).decode()
    parts = [b64[i:i + chunk] for i in range(0, len(b64), chunk)] or [""]
    lines = [f"DUMP {name} {i + 1}/{len(parts)} {p}" for i, p in enumerate(parts)]
    lines.append(f"DUMP-END {name} {hashlib.sha1(data).hexdigest()} {len(data)}")
    return lines


def reassemble(lines: list[str]) -> dict[str, bytes]:
    """Lines (any order, other lines ignored) -> {name: bytes}; raises on a missing chunk
    or a checksum mismatch."""
    parts: dict[str, dict[int, str]] = {}
    totals: dict[str, int] = {}
    ends: dict[str, tuple[str, int]] = {}
    for line in lines:
        line = line.strip()
        if line.startswith("DUMP-END "):
            _, name, sha, n = line.split(" ", 3)
            ends[name] = (sha, int(n))
        elif line.startswith("DUMP "):
            _, name, idx, payload = (line.split(" ", 3) + [""])[:4]
            i, n = idx.split("/")
            parts.setdefault(name, {})[int(i)] = payload
            totals[name] = int(n)
    out = {}
    for name, got in parts.items():
        missing = [i for i in range(1, totals[name] + 1) if i not in got]
        if missing:
            raise ValueError(f"{name}: missing chunks {missing[:10]}")
        data = base64.b64decode("".join(got[i] for i in range(1, totals[name] + 1)))
        if name in ends and (hashlib.sha1(data).hexdigest(), len(data)) != ends[name]:
            raise ValueError(f"{name}: checksum mismatch")
        out[name] = data
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("paths", nargs="+")
    p.add_argument("--chunk", type=int, default=4000)
    a = p.parse_args(argv)
    for path in a.paths:
        data = Path(path).read_bytes()
        for line in chunks(Path(path).name, data, a.chunk):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
