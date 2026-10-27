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
