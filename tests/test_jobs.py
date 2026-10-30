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


def test_full_dev_chain_grades_collects_backtests_calibrates_then_checks_blinding():
    cmds = jobs.resolve("full-dev")
    assert len(cmds) == 10 and not any("--years" in c for c in cmds)
    assert [c[-1] for c in cmds[:2]] == ["identified", "blinded"] and cmds[-1][-1] == "check-blind"
    assert sum('THESIS_PARAMS={"exclude_late": true}' in c for c in cmds) == 2
    assert cmds[-2][-3:] == ["calibrate", "--split", "dev"] and "holdout" not in " ".join(" ".join(c) for c in cmds)


def test_ablations_change_one_parameter_at_a_time_on_the_identified_variant():
    import json
    cmds = jobs.resolve("ablations")
    assert len(cmds) == 14 and all(c[1] == "THESIS_VARIANT=identified" for c in cmds)
    changed = [json.loads(c[2].split("=", 1)[1]) for c in cmds]
    assert all(len(d) == 1 for d in changed[:-1]) and changed[-1] == {"flat_quality": True, "direction": "none"}
    assert {next(iter(d)) for d in changed} == {"gamma", "beta", "use_author", "min_evidence", "use_comments",
                                                "thesis_gate", "corroboration", "use_specificity", "direction",
                                                "flat_quality"}
    assert all(c[-2:] == ["--split", "dev"] and "--years" not in c for c in cmds)
    with pytest.raises(jobs.JobError, match="no JOB_ARGS"):
        jobs.resolve("ablations", "--years 2014")


def test_finalist_checks_cover_the_control_the_leak_checks_and_the_calibrations():
    import json
    cmds = jobs.resolve("finalist-checks")
    assert len(cmds) == 9 and cmds[0][-4:] == ["--baseline", "substantive", "--split", "dev"]
    params = [json.loads(c[2].split("=", 1)[1]) for c in cmds[1:]]
    assert all(p["direction"] == "none" for p in params)
    assert sum(p.get("flat_quality", False) for p in params) == 4
    assert sum(p.get("exclude_late", False) for p in params) == 3 and sum(p.get("gamma") == 0 for p in params) == 3
    assert cmds[4][1] == "THESIS_VARIANT=blinded" and all(c[-3:] == ["calibrate", "--split", "dev"] for c in cmds[-2:])
    assert "holdout" not in " ".join(" ".join(c) for c in cmds)


def test_substantive_checks_switch_off_one_rule_per_run_starting_with_deleted_authors():
    import json
    cmds = jobs.resolve("substantive-checks")
    assert len(cmds) == 4 and all(c[-6:-1] == ["--baseline", "substantive", "--split", "dev", "--rules"] for c in cmds)
    rules = [json.loads(c[-1]) for c in cmds]
    assert rules[0] == {"drop_deleted": False} and all(len(r) == 1 for r in rules)
    assert {next(iter(r)) for r in rules} == {"drop_deleted", "subject", "max_companies", "substance"}


def test_gold_dump_pulls_the_gold_folder_then_prints_the_ratings():
    cmds = jobs.resolve("gold-dump")
    assert cmds[0][-3:] == ["pull", "--only", "gold"] and cmds[1][1:3] == ["scripts/dump_file.py", "data/gold/ratings.csv"]


def test_regrade_chain_runs_census_grading_collect_blind_check_gold_tuning_and_export():
    cmds = jobs.resolve("regrade")
    assert cmds[0][1] == "scripts/thesis_funnel.py" and [c[-1] for c in cmds[1:3]] == ["identified", "blinded"]
    assert cmds[5][-1] == "check-blind" and cmds[6][-2:] == ["evaluate", "--tune"] and cmds[-1][-1] == "--push"
    assert "holdout" not in " ".join(" ".join(c) for c in cmds)


def test_gated_dev_runs_the_gated_formula_both_ways_plus_controls_calibrations_and_the_export():
    import json
    cmds = jobs.resolve("gated-dev")
    params = [json.loads(c[2].split("=", 1)[1]) for c in cmds[:5]]
    assert params[0] == {"quality": "gated"} and params[1]["direction"] == "none" and cmds[2][1] == "THESIS_VARIANT=blinded"
    assert params[3] == {"direction": "none"} and params[4] == {} and all(c[-2:] == ["--split", "dev"] for c in cmds[:7])
    assert all(c[-3:-2] == ["calibrate"] for c in cmds[5:7]) and cmds[-1][-1] == "--push"
    assert "holdout" not in " ".join(" ".join(c) for c in cmds)


def test_v2_presets_carry_the_prompt_on_every_command():
    import json
    gold = jobs.resolve("v2-gold")
    assert len(gold) == 2 and all(c[:2] == ["env", "THESIS_PROMPT=thesis_v2"] for c in gold)
    assert gold[0][-4:] == ["run", "--variant", "identified", "--dry-run"] and gold[1][-2:] == ["evaluate", "--tune"]
    full = jobs.resolve("v2-full")
    assert all(c[:2] == ["env", "THESIS_PROMPT=thesis_v2"] for c in full)
    assert [c[-1] for c in full[:2]] == ["identified", "blinded"] and [c[-1] for c in full[2:4]] == ["identified", "blinded"]
    assert [c[4] for c in full[:4]] == ["run", "run", "collect", "collect"]
    assert full[4][-1] == "check-blind" and full[5][-2:] == ["evaluate", "--tune"]
    runs = [c for c in full if "scripts/backtest.py" in c]
    params = [json.loads(c[3].split("=", 1)[1]) for c in runs]
    assert len(runs) == 5 and all(p["quality"] == "direct" for p in params)
    assert [c[2] for c in runs] == ["THESIS_VARIANT=identified"] * 2 + ["THESIS_VARIANT=blinded"] * 2 + ["THESIS_VARIANT=identified"]
    assert sum(p.get("direction") == "none" for p in params) == 2 and params[-1].get("exclude_late") is True
    assert all(c[-3:] == ["calibrate", "--split", "dev"] for c in full[-3:-1]) and full[-1][-1] == "--push"
    assert "holdout" not in " ".join(" ".join(c) for c in full)
    with pytest.raises(jobs.JobError, match="no JOB_ARGS"):
        jobs.resolve("v2-full", "--years 2014")


def test_v2_halves_split_the_full_run_by_variant():
    import json
    ident = jobs.resolve("v2-identified")
    assert all(c[:2] == ["env", "THESIS_PROMPT=thesis_v2"] for c in ident) and "run" not in [c[4] for c in ident]
    assert ident[0][4:] == ["collect", "--variant", "identified"] and ident[1][-2:] == ["evaluate", "--tune"]
    runs = [json.loads(c[3].split("=", 1)[1]) for c in ident if "scripts/backtest.py" in c]
    assert len(runs) == 3 and all(p["quality"] == "direct" for p in runs) and runs[1]["direction"] == "none"
    assert all(c[2] == "THESIS_VARIANT=identified" for c in ident if "THESIS_VARIANT" in " ".join(c))
    assert all(c[-3:] == ["calibrate", "--split", "dev"] for c in ident[-3:-1]) and ident[-1][-1] == "--push"
    blind = jobs.resolve("v2-blinded")
    assert blind[0][4:] == ["run", "--variant", "blinded"] and blind[1][4:] == ["collect", "--variant", "blinded"]
    assert blind[2][-1] == "check-blind" and all(c[2] == "THESIS_VARIANT=blinded" for c in blind[3:])
    assert len(blind) == 5 and "holdout" not in " ".join(" ".join(c) for c in ident + blind)


def test_gold_eval_both_runs_nano_then_jev():
    cmds = jobs.resolve("gold-eval-both")
    assert len(cmds) == 2 and cmds[0][-1] == "evaluate" and cmds[1][-2:] == ["--model", "jev-1.13.0"]


def test_darlings_preset_reads_the_v2_grades_and_takes_args():
    cmds = jobs.resolve("darlings", "--inspect ASML")
    assert cmds == [["env", "THESIS_PROMPT=thesis_v2", jobs.PY, "scripts/darlings.py", "run", "--inspect", "ASML"]]
    with pytest.raises(jobs.JobError, match="holdout"):
        jobs.resolve("darlings", "--split holdout")


def test_darlings_recent_chain():
    cmds = jobs.resolve("darlings-recent")
    assert cmds[0][1:3] == ["scripts/darling_census.py", "--years"] and "--write" in cmds[0]
    assert "--only" in cmds[1] and cmds[1][-1] == "data/features/darlings/threads.parquet"
    llm = [c for c in cmds if "scripts/thesis_llm.py" in c]
    assert all(c[:2] == ["env", "THESIS_PROMPT=thesis_v2"] and "data/features/darlings/threads.parquet" in c for c in llm)
    assert [c[c.index("--variant") + 1] for c in llm if "--variant" in c] == ["identified", "blinded"] * 2
    assert cmds[-1][-1] == "blinded" and "recent" in cmds[-1] and "holdout" not in " ".join(" ".join(c) for c in cmds)


def test_darlings_anytime_grades_identified_first_and_checks_the_blinding():
    cmds = jobs.resolve("darlings-anytime")
    assert cmds[0][-6:] == ["--write", "data/features/darlings_b/candidates.parquet", "--write-level", "B",
                            "--blue-chip-top", "200"]
    crowd = [i for i, c in enumerate(cmds) if "darlings.py" in " ".join(c)]
    blind = [i for i, c in enumerate(cmds) if "blinded" in c]
    assert len(crowd) == 2 and crowd[0] < min(blind) and "check-blind" in cmds[crowd[1] - 1]
    assert all("--rule" in cmds[i] and "crowd" in cmds[i] for i in crowd)


def test_fix_rebuild_keeps_the_old_universe_and_rebuilds_around_the_splits_pull():
    cmds = jobs.resolve("fix-rebuild")
    assert cmds[0][:2] == ["sh", "-c"] and "[ -e data/features/universe_before_fix.parquet ] ||" in cmds[0][2]
    names = [" ".join(c) for c in cmds]
    u = [i for i, n in enumerate(names) if "universe.py" in n]
    sp = [i for i, n in enumerate(names) if "market_data.py splits" in n]
    assert cmds[sp[0]][:2] == ["sh", "-c"] and "|| echo" in cmds[sp[0]][2]
    assert len(u) == 2 and u[0] < sp[0] < u[1]
    assert not any("validate" in n and "substantive" in n for n in names)     # the finalist stays off validate


def test_darlings_more_grades_on_jev_and_runs_the_crowd_rule_on_every_grade_set():
    cmds = jobs.resolve("darlings-more")
    llm_steps = [c for c in cmds if "thesis_llm.py" in " ".join(c)]
    assert len(llm_steps) == 5 and all("LLM_MODEL=jev-1.13.0" in c and "THESIS_PROMPT=thesis_v2" in c for c in llm_steps)
    census = cmds[0]
    assert census[census.index("--watch") + 1:census.index("--watch") + 4] == ["NVDA", "ASML", "RKLB"]
    assert "--top-k" in cmds[1] and cmds[1][cmds[1].index("--top-k") + 1] == "6"
    crowd = [c for c in cmds if "darlings.py" in " ".join(c)]
    assert all("all" in c and "data/features/darlings_c" in c and "data/features" in c for c in crowd)
    assert "--push" in cmds[-1] and "darling_export.py" in " ".join(cmds[-1])
