import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import edgar  # noqa: E402


def test_division_mapping():
    assert edgar.division("7372") == "I"      # software services
    assert edgar.division("2834") == "D"      # pharma (manufacturing)
    assert edgar.division("6021") == "H"      # banks
    assert edgar.division("") is None and edgar.division("abc") is None


def test_user_agent_required_and_never_invented(monkeypatch, tmp_path):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(SystemExit):
        edgar.user_agent(tmp_path / "nope.env")
    env = tmp_path / ".env"
    env.write_text("SEC_USER_AGENT='Jane Doe jane@example.com'\n")
    assert edgar.user_agent(env) == "Jane Doe jane@example.com"


def test_pull_is_resumable_and_records_missing_companies(tmp_path):
    calls = []

    def fake(cik, ua):
        calls.append(cik)
        return None if cik == "9" else {"sic": "7372", "sicDescription": "Services-Prepackaged Software"}

    out = tmp_path / "sic.parquet"
    assert edgar.pull(["1", "9"], out, "ua", fetcher=fake, sleep=lambda s: None, log=lambda m: None) == 2
    assert edgar.pull(["1", "9", "2"], out, "ua", fetcher=fake, sleep=lambda s: None, log=lambda m: None) == 1
    rows = {r["cik"]: r for r in pq.read_table(out).to_pylist()}
    assert calls == ["1", "9", "2"] and rows["1"]["division"] == "I" and rows["9"]["sic"] is None
