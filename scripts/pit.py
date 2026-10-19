"""Point-in-time view of the Reddit corpus: the only way scorers see the data.

A scorer is handed a ``CohortView`` that is already filtered to one decision time (the
cutoff). By construction it contains:

* only posts whose **maturation window has closed** by the cutoff
  (``created + maturation_hours <= cutoff``) and that were created within the lookback window;
* only comments written **within that post's maturation window** (and before the cutoff);
* nothing that was edited after the maturation window (the dump holds *final* text, so an
  "EDIT: this went 10x" would otherwise leak the future); ``edited_policy="keep"`` relaxes this;
* **no final engagement numbers** by default: no ``score``, ``num_comments`` or ``upvote_ratio``
  columns exist in the view (the dumps record end-of-life values, not values as of the cutoff).
  ``votes="final"`` is the one deliberate exception: it exposes the dump's final post and
  comment scores as a ``votes`` column. Reddit archived posts after six months until September
  2021 (no voting after that), so for cohorts before then a final score is a six-month score;
  the leak is the window between maturation and archive. Every scorer that uses it records
  ``votes`` in its ledger entry.
* ``comments="all"`` is the second deliberate exception (the thesis pipeline, user decision
  2026-10-19): every comment the thread ever got, including ones written after the cutoff, is
  in ``thread_comments`` and ``comment_mentions``. With ``maturation_hours=0`` and
  ``edited_policy="keep"`` there is then no lock at all. Measured on 2012-2020: 88% of
  comments arrive within 24h of the post, 98.4% within 7 days, and 0.3% of long self posts
  are edited after 7 days, so the hindsight this lets in is small but not zero.

Because the view is a separate in-memory database that only ever receives these filtered
rows, even arbitrary SQL run through ``CohortView.query`` cannot reach past the cutoff.

Known residual bias: posts and comments that were later removed or deleted are absent from
the dumps, so spam/pump content that was taken down is invisible to every scorer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

DAY = 86400


@dataclass(frozen=True)
class PITConfig:
    maturation_hours: int = 24
    edited_policy: str = "exclude"   # exclude | keep (text edited after maturation)
    votes: str = "none"              # none | final (expose the dump's final scores as ``votes``)
    comments: str = "window"         # window (inside maturation, before cutoff) | all (ever)

    def __post_init__(self):
        if self.edited_policy not in ("exclude", "keep"):
            raise ValueError(self.edited_policy)
        if self.votes not in ("none", "final"):
            raise ValueError(self.votes)


class PITCorpus:
    def __init__(self, parquet_root: Path, mentions_path: Path, config: PITConfig = PITConfig(),
                 post_features: Path | None = None):
        self.config = config
        self.post_features = Path(post_features) if post_features else None
        self.root = Path(parquet_root)
        self.mentions_path = Path(mentions_path)
        self.con = duckdb.connect()
        root = Path(parquet_root)
        self.con.execute(f"create view posts as select * from read_parquet('{root}/posts/*.parquet')")
        self.con.execute(f"create view comments as select * from read_parquet('{root}/comments/*.parquet')")
        self.con.execute(f"create view mentions as select * from read_parquet('{mentions_path}')")
        if self.post_features:  # pure functions of each post's own text (see baselines.py)
            self.con.execute(f"create view pfeat as select * from read_parquet('{self.post_features}')")

    def _edit_ok(self, alias: str, matured_at: str) -> str:
        if self.config.edited_policy == "keep":
            return "true"
        return f"({alias}.edited_utc = 0 or ({alias}.edited_utc > 0 and {alias}.edited_utc <= {matured_at}))"

    def at(self, cutoff_utc: int, window_days: int) -> "CohortView":
        """The corpus as it stood at ``cutoff_utc``, looking back ``window_days``."""
        mat = self.config.maturation_hours * 3600
        lo = cutoff_utc - window_days * DAY
        edit_p = self._edit_ok("p", f"(p.created_utc + {mat})")
        edit_c = self._edit_ok("c", f"(p.created_utc + {mat})")
        c_time = ("true" if self.config.comments == "all"
                  else f"c.created_utc <= p.created_utc + {mat} and c.created_utc <= {int(cutoff_utc)}")
        feat_cols = feat_join = ""
        pv = ", p.score as votes" if self.config.votes == "final" else ""
        cv = ", c.score as votes" if self.config.votes == "final" else ""
        if self.post_features:
            feat_cols = ", f.* exclude (post_id)"
            feat_join = "left join pfeat f on f.post_id = p.id"
        post_mentions = self.con.execute(f"""
            select m.cik, m.ticker, m.sec_type, m.method, m.resolve_conf, m.n, m.in_title,
                   count(*) over (partition by m.source_id) as n_companies,
                   p.id as post_id, p.subreddit, p.created_utc, p.author, p.title, p.selftext,
                   p.flair, p.url, p.domain, p.is_self, p.edited_utc{pv}{feat_cols}
            from mentions m join posts p on m.source = 'post' and m.source_id = p.id
            {feat_join}
            where p.created_utc >= ? and p.created_utc + {mat} <= ? and {edit_p}""",
            [lo, cutoff_utc]).to_arrow_table()
        comment_mentions = self.con.execute(f"""
            select m.cik, m.ticker, m.method, m.n, c.id as comment_id, c.post_id, c.created_utc,
                   c.author
            from mentions m join comments c on m.source = 'comment' and m.source_id = c.id
            join posts p on p.id = c.post_id
            where p.created_utc >= ? and p.created_utc + {mat} <= ?
              and {c_time} and {edit_p} and {edit_c}""", [lo, cutoff_utc]).to_arrow_table()
        thread_comments = self.con.execute(f"""
            select c.id as comment_id, c.post_id, c.parent_id, c.created_utc, c.author, c.body,
                   c.edited_utc{cv}
            from comments c join posts p on p.id = c.post_id
            where p.created_utc >= ? and p.created_utc + {mat} <= ?
              and {c_time} and {edit_p} and {edit_c}""", [lo, cutoff_utc]).to_arrow_table()
        return CohortView(cutoff_utc, window_days, post_mentions, comment_mentions, thread_comments)


class CohortView:
    """Everything a scorer may know at ``cutoff_utc``. Immutable; no way to widen it."""

    def __init__(self, cutoff_utc: int, window_days: int, post_mentions, comment_mentions,
                 thread_comments):
        self.cutoff_utc = cutoff_utc
        self.window_days = window_days
        self._con = duckdb.connect()
        self._con.register("post_mentions", post_mentions)
        self._con.register("comment_mentions", comment_mentions)
        self._con.register("thread_comments", thread_comments)

    def query(self, sql: str, params=None) -> list[dict]:
        """SQL over the three visible tables: ``post_mentions`` (one row per post x company),
        ``comment_mentions`` and ``thread_comments`` (comments inside maturation windows)."""
        cur = self._con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def ciks(self) -> set[str]:
        return {r["cik"] for r in self.query(
            "select cik from post_mentions union select cik from comment_mentions")}

    def posts(self, cik: str | None = None) -> list[dict]:
        sql = "select * from post_mentions" + (" where cik = ?" if cik else "") + " order by created_utc"
        return self.query(sql, [cik] if cik else None)

    def comments_for(self, post_ids: list[str]) -> list[dict]:
        if not post_ids:
            return []
        marks = ",".join("?" * len(post_ids))
        return self.query(f"select * from thread_comments where post_id in ({marks}) "
                          "order by created_utc", list(post_ids))
