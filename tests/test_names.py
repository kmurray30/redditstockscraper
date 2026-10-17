import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import names as n  # noqa: E402


def test_curly_apostrophes_and_dotcoms_match():
    assert n.name_similarity("Carter’s Inc", "CARTERS INC") == 1.0
    assert n.name_similarity("Macy’s Inc", "Macy's, Inc.") == 1.0
    assert n.name_similarity("Overstockcom Inc", "Overstock.com, Inc.") == 1.0


def test_different_companies_do_not_match():
    assert n.name_similarity("Century Communities Inc", "COMCAST CORP") == 0.0
    assert n.name_similarity("Healthpeak Properties Inc", "Physicians Realty Trust") == 0.0
    assert n.name_similarity(None, "x") == 0.0


def test_renames_share_no_tokens_by_design():
    assert n.name_similarity("Meta Platforms Inc", "Facebook Inc") == 0.0
