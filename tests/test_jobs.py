import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import jobs  # noqa: E402


def _records(data: Path) -> list[dict]:
    return [json.loads(x) for x in (data / "logs" / "jobs.jsonl").read_text().splitlines()]


def test_presets_and_args():
    assert jobs.resolve("corpus") == [[jobs.PY, "scripts/corpus.py"]]
    cmds = jobs.resolve("ladder-dev", "--top 0.05")
    assert cmds[-1][-2:] == ["--top", "0.05"]
    with pytest.raises(jobs.JobError):
        jobs.resolve("corpus", "--subs stocks")       # presets without args refuse extras
    with pytest.raises(jobs.JobError):
        jobs.resolve("rm -rf data")
    with pytest.raises(jobs.JobError):
        jobs.resolve("run")                           # run needs a baseline or scorer
    assert jobs.resolve("run", "--baseline mentions")[0][-2:] == ["--baseline", "mentions"]


def test_rebuild_chain_order():
    steps = [" ".join(c[1:3]) for c in jobs.PRESETS["rebuild"]]
    assert steps[0] == "scripts/security_master.py export-tickers"
    assert steps.count("scripts/universe.py") == 2    # universe, splits pull, universe again
    i = steps.index("scripts/market_data.py splits")
    assert steps[i - 1] == steps[i + 1] == "scripts/universe.py"
    assert steps[-1] == "scripts/backtest.py ladder"


def test_script_jobs_only_reach_existing_scripts(tmp_path):
    (tmp_path / "thesis.py").write_text("")
    assert jobs.resolve("script:thesis", "--x 1", scripts_dir=tmp_path)[0][1:] == \
        ["scripts/thesis.py", "--x", "1"]
    for bad in ("script:", "script:../x", "script:missing", "script:jobs"):
        with pytest.raises(jobs.JobError):
            jobs.resolve(bad, scripts_dir=tmp_path)


def test_holdout_needs_explicit_permission():
    for args in ("--baseline mentions --split holdout",
                 "--baseline mentions --split dev --accept-contaminated"):
        with pytest.raises(jobs.JobError, match="holdout"):
            jobs.resolve("run", args)
        assert jobs.resolve("run", args, {"ALLOW_HOLDOUT": "1"})


def test_runs_once_per_job_id_and_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(jobs.PRESETS, "hello", [[sys.executable, "-c", "print('hi there')"]])
    env = {"JOB": "hello", "JOB_ID": "a1"}
    assert jobs.handle(env, data=tmp_path) == 0
    assert "hi there" in capsys.readouterr().out
    recs = _records(tmp_path)
    assert [r["event"] for r in recs] == ["started", "finished"] and recs[1]["exit"] == 0
    log = tmp_path.parent / recs[1]["log"] if not Path(recs[1]["log"]).is_absolute() else Path(recs[1]["log"])
    assert "hi there" in log.read_text()
    # a redeploy with the same JOB_ID does nothing; a new JOB_ID runs again
    assert jobs.handle(env, data=tmp_path) is None
    assert jobs.handle({**env, "JOB_ID": "a2"}, data=tmp_path) == 0


def test_chain_stops_at_first_failure_and_failed_id_is_not_retried(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(jobs.PRESETS, "chain", [
        [sys.executable, "-c", "print('one')"],
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        [sys.executable, "-c", "print('never')"]])
    env = {"JOB": "chain", "JOB_ID": "c1"}
    assert jobs.handle(env, data=tmp_path) == 3
    out = capsys.readouterr().out
    assert "one" in out and "never" not in out
    assert jobs.handle(env, data=tmp_path) is None


def test_interrupted_run_is_retried(tmp_path, monkeypatch):
    monkeypatch.setitem(jobs.PRESETS, "ok", [[sys.executable, "-c", "pass"]])
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "jobs.jsonl").write_text(
        json.dumps({"event": "started", "job_id": "k1", "job": "ok"}) + "\n")
    assert jobs.handle({"JOB": "ok", "JOB_ID": "k1"}, data=tmp_path) == 0


def test_nothing_requested_or_refused(tmp_path, capsys, monkeypatch):
    assert jobs.handle({}, data=tmp_path) is None
    assert jobs.handle({"JOB": "corpus"}, data=tmp_path) is None      # no JOB_ID
    assert jobs.handle({"JOB": "nope", "JOB_ID": "r1"}, data=tmp_path) == 2
    assert "refused" in capsys.readouterr().out
    assert [r["event"] for r in _records(tmp_path)] == ["refused"]
    # a refusal does not consume the JOB_ID: a newer deployment that knows the job still runs it
    monkeypatch.setitem(jobs.PRESETS, "nope", [[sys.executable, "-c", "print('now known')"]])
    assert jobs.handle({"JOB": "nope", "JOB_ID": "r1"}, data=tmp_path) == 0
    assert jobs.handle({"JOB": "nope", "JOB_ID": "r1"}, data=tmp_path) is None


def test_bucket_presets():
    assert jobs.resolve("pull-data")[0][1:] == ["scripts/bucket_sync.py", "pull"]
    assert jobs.resolve("pull-data", "--only ref")[0][-2:] == ["--only", "ref"]
    assert jobs.resolve("verify-data")[0][-2:] == ["--direction", "pull"]


def test_thesis_presets_and_holdout_guard():
    assert jobs.resolve("census", "--years 2014")[0][-2:] == ["--years", "2014"]
    assert jobs.resolve("llm-run", "--variant blinded")[0][1:3] == ["scripts/thesis_llm.py", "run"]
    assert jobs.resolve("thesis-run", "--split dev")[0][-4:] == ["--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev"]
    with pytest.raises(jobs.JobError, match="holdout"):
        jobs.resolve("thesis-run", "--split holdout")
    with pytest.raises(jobs.JobError, match="holdout"):
        jobs.resolve("calibrate", "--split holdout")


def test_dump_file_round_trip():
    import dump_file as df
    data = ("hello\n" * 500).encode()
    lines = df.chunks("a.md", data, chunk=100) + df.chunks("b.csv", b"x,y\n1,2\n", chunk=100)
    assert df.reassemble(["noise"] + lines[::-1]) == {"a.md": data, "b.csv": b"x,y\n1,2\n"}
    with pytest.raises(ValueError, match="missing"):
        df.reassemble(lines[1:])
    with pytest.raises(ValueError, match="checksum"):
        df.reassemble([lines[0].replace("DUMP a.md 1/", "DUMP a.md 1/")[:-4] + "AAAA"] + lines[1:])


def test_pilot_chain_covers_both_variants_and_restricts_the_years():
    cmds = jobs.resolve("pilot")
    assert len(cmds) == 7 and cmds[0][-3:] == ["--years", "2014", "2017"]
    assert all("2014" in c and "2017" in c for c in cmds if "check-blind" not in c)
    assert cmds[-3][:2] == ["env", "THESIS_VARIANT=identified"] and cmds[-2][1] == "THESIS_VARIANT=blinded"
    assert "--split" in cmds[-2] and "dev" in cmds[-2] and "holdout" not in " ".join(cmds[-2])
    assert cmds[-1][-1] == "check-blind"


def test_pilot_checks_run_the_ladder_and_the_late_exclusion_on_the_same_years():
    cmds = jobs.resolve("pilot-checks")
    assert cmds[0][1:3] == ["scripts/backtest.py", "ladder"] and cmds[0][-3:] == ["--years", "2014", "2017"]
    assert all('THESIS_PARAMS={"exclude_late": true}' in c for c in cmds[1:])
    assert {c[1] for c in cmds[1:]} == {"THESIS_VARIANT=identified", "THESIS_VARIANT=blinded"}


def test_gold_eval_both_runs_nano_then_jev():
    cmds = jobs.resolve("gold-eval-both")
    assert len(cmds) == 2 and cmds[0][-1] == "evaluate" and cmds[1][-2:] == ["--model", "jev-1.13.0"]
