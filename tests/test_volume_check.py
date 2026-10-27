import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import volume_check as vc  # noqa: E402


def test_later_splits_scale_the_stored_volume(tmp_path, capsys):
    assert vc.factor([("2022-06-06", 20.0)], "2020-07-01") == 1 / 20
    assert vc.factor([("2022-06-06", 20.0)], "2023-01-01") == 1.0
    assert vc.factor([("2021-01-05", 0.05)], "2020-07-01") == 20.0          # 1:20 reverse split
    feats, mk = tmp_path / "f", tmp_path / "m"
    (mk / "splits").mkdir(parents=True)
    feats.mkdir()
    uni = [{"month": "2015-01-01", "cik": "1", "ticker": "AMZN", "code": "AMZN", "price": 300.0, "adv60": 2e6,
            "eligible": True, "size_q": 5},
           {"month": "2015-01-01", "cik": "2", "ticker": "DIE", "code": "DIE", "price": 3.0, "adv60": 5e4,
            "eligible": False, "size_q": 0}]
    pq.write_table(pa.Table.from_pylist(uni), feats / "universe.parquet")
    pq.write_table(pa.table({"date": ["2022-06-06"], "ratio": [20.0]}), mk / "splits" / "AMZN.parquet")
    pq.write_table(pa.table({"date": ["2016-01-05"], "ratio": [0.05]}), mk / "splits" / "DIE.parquet")
    lab = [{"month": "2015-01-01", "cik": c, "years": 5, "status": "complete", "max_mult": m, "perm_loss": pl}
           for c, m, pl in (("1", 6.0, False), ("2", 0.5, True))]
    pq.write_table(pa.Table.from_pylist(lab), feats / "labels.parquet")
    assert vc.main(["--features", str(feats), "--market", str(mk), "--month", "2015-01-01", "--tickers", "AMZN"]) == 0
    out = capsys.readouterr().out
    assert "1 eligible rows fall below" in out and "1 ineligible rows rise above" in out
    assert "corrected $0.1M" in out and "in             dev" in out


def test_split_jump_check_tells_adjusted_from_raw_volume(tmp_path, capsys):
    import numpy as np
    (tmp_path / "eod").mkdir()
    dates = [f"2020-01-{d:02d}" for d in range(1, 31)] + [f"2020-02-{d:02d}" for d in range(1, 21)]
    for code, ratio in (("FWD", 2.0), ("REV", 0.1)):
        k = 30
        raw = [10.0] * k + [10.0 / ratio] * 20          # the quoted price moves at the split
        adj = [10.0 / ratio] * 50                       # adjusted: continuous
        vol = [1000.0 * ratio] * 50                     # volume adjusted for the split: continuous
        pq.write_table(pa.table({"date": dates, "close": raw, "adjusted_close": adj, "volume": vol}),
                       tmp_path / "eod" / f"{code}.parquet")
    vc.split_jumps(tmp_path, {"FWD": [("2020-01-31", 2.0)], "REV": [("2020-01-31", 0.1)]}, 10)
    out = capsys.readouterr().out
    assert "share volume after/before 1 (as a power of the ratio -0.00" in out or "+0.00" in out
    assert out.count("adjusted close x volume 1 (") == 2
