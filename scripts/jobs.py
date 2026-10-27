"""Job runner for the hosted worker (Railway). It is the service's start command.

The worker is one container with the data volume mounted at ``/app/data``. A job is requested
by setting two service variables, which redeploys the service:

    JOB      a preset name (see ``PRESETS``) or ``script:<name>`` to run ``scripts/<name>.py``
    JOB_ARGS extra arguments, appended to a ``script:`` job (and to ``ladder``/``run`` presets)
    JOB_ID   any new string; a job runs once per JOB_ID

Each run streams its output to the deploy log and to ``data/logs/<utc>_<job>.log``. It also
appends ``started`` / ``finished`` records to ``data/logs/jobs.jsonl``. A JOB_ID that already
finished is not run again (whatever its exit code), so a code push that redeploys the service
does not repeat finished work. A run killed half-way (redeploy) has no ``finished`` record
and runs again. After the job, or when nothing is requested, the process idles so the
container stays up for ``railway ssh`` / ``scp``.

The sealed holdout is guarded twice: any job whose arguments name the ``holdout`` split or
``--accept-contaminated`` is refused unless the service variable ``ALLOW_HOLDOUT=1`` is set.
Only the user sets that (the ledger's own one-look guard still applies on top).
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SUBS = ["stocks", "investing", "StockMarket", "ValueInvesting", "SecurityAnalysis"]


def _py(script: str, *args: str) -> list[str]:
    return [PY, f"scripts/{script}", *args]


_V2 = ("env", "THESIS_PROMPT=thesis_v2")   # the second prompt, carried by every v2 preset command


PRESETS: dict[str, list[list[str]]] = {
    "tests": [[PY, "-m", "pytest", "-q"]],
    "du": [["du", "-sh", "data"], ["sh", "-c", "du -sh data/* data/*/* 2>/dev/null | sort -h"]],
    "df": [["df", "-h", "data"]],
    "export-tickers": [_py("security_master.py", "export-tickers")],
    "refilter": [_py("download_reddit.py", "pipeline", "--subs", *SUBS, "--keep-raw",
                     "--tickers", "data/ref/tickers_all.txt",
                     "--stoplist", "data/ref/stoplist.txt")],
    "corpus": [_py("corpus.py")],
    "wordstats": [_py("entity_resolver.py", "wordstats")],
    "mentions": [_py("entity_resolver.py", "mentions")],
    "universe": [_py("universe.py")],
    "splits": [_py("market_data.py", "splits", "--universe", "data/features/universe.parquet")],
    "baselines": [_py("baselines.py")],
    "edgar": [_py("edgar.py")],
    "ladder-dev": [_py("backtest.py", "ladder", "--split", "dev")],
    "ladder-validate": [_py("backtest.py", "ladder", "--split", "validate")],
    "run": [_py("backtest.py", "run")],
    "pull-data": [_py("bucket_sync.py", "pull")],
    "bucket-status": [_py("bucket_sync.py", "status")],
    "verify-data": [_py("bucket_sync.py", "verify", "--direction", "pull")],
    # the thesis scorer (see CLAUDE.md "Thesis scorer"); none of these spend money except
    # llm-run and gold-eval, and those stop at LLM_SPEND_CAP_USD
    "census": [_py("thesis_funnel.py")],
    "llm-run": [_py("thesis_llm.py", "run")],
    "llm-collect": [_py("thesis_llm.py", "collect")],
    "check-blind": [_py("thesis_llm.py", "check-blind")],
    "gold-build": [_py("goldset.py", "build")],
    # the user's ratings and written reasons, through the log (the rating page is not
    # reachable from a session; the bucket copy is the truth)
    "gold-dump": [_py("bucket_sync.py", "pull", "--only", "gold"),
                  _py("dump_file.py", "data/gold/ratings.csv", "--chunk", "3000")],
    "gold-eval": [_py("goldset.py", "evaluate")],
    # both graders on the gold set in one deployment (Railway queues can take an hour each)
    "gold-eval-both": [_py("goldset.py", "evaluate"), _py("goldset.py", "evaluate", "--model", "jev-1.13.0")],
    "thesis-timeline": [_py("thesis_scorer.py", "timeline")],
    # the graded threads with the text the model saw, for the password-gated thread browser
    "export-threads": [_py("thread_export.py", "--push")],
    "calibrate": [_py("thesis_scorer.py", "calibrate")],
    "thesis-run": [_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory")],
    # the pilot in one deployment: grade 2014 and 2017 both ways, collect, check the blinding,
    # backtest both variants on dev. LLM_MODEL decides the grader. Cached per thread, so the
    # later full dev run pays only for the other years.
    "pilot": [_py("thesis_llm.py", "run", "--variant", "identified", "--years", "2014", "2017"),
              _py("thesis_llm.py", "run", "--variant", "blinded", "--years", "2014", "2017"),
              _py("thesis_llm.py", "collect", "--variant", "identified", "--years", "2014", "2017"),
              _py("thesis_llm.py", "collect", "--variant", "blinded", "--years", "2014", "2017"),
              ["env", "THESIS_VARIANT=identified", *_py("backtest.py", "run", "--scorer",
                                                        "scripts/thesis_scorer.py:factory", "--split", "dev",
                                                        "--years", "2014", "2017")],
              ["env", "THESIS_VARIANT=blinded", *_py("backtest.py", "run", "--scorer",
                                                     "scripts/thesis_scorer.py:factory", "--split", "dev",
                                                     "--years", "2014", "2017")],
              _py("thesis_llm.py", "check-blind")],     # last: a leak flags the job, the reports still print
    # the full dev run in one deployment: grade every planned thread both ways (the pilot's years
    # are cached), collect, backtest both variants plain and with late content excluded,
    # calibrate, and check the blinding last. About $2 on Jev.
    "full-dev": [_py("thesis_llm.py", "run", "--variant", "identified"),
                 _py("thesis_llm.py", "run", "--variant", "blinded"),
                 _py("thesis_llm.py", "collect", "--variant", "identified"),
                 _py("thesis_llm.py", "collect", "--variant", "blinded"),
                 ["env", "THESIS_VARIANT=identified", *_py("backtest.py", "run", "--scorer",
                                                           "scripts/thesis_scorer.py:factory", "--split", "dev")],
                 ["env", "THESIS_VARIANT=blinded", *_py("backtest.py", "run", "--scorer",
                                                        "scripts/thesis_scorer.py:factory", "--split", "dev")],
                 ["env", "THESIS_VARIANT=identified", 'THESIS_PARAMS={"exclude_late": true}',
                  *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")],
                 ["env", "THESIS_VARIANT=blinded", 'THESIS_PARAMS={"exclude_late": true}',
                  *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")],
                 _py("thesis_scorer.py", "calibrate", "--split", "dev"),
                 _py("thesis_llm.py", "check-blind")],
    # the free ablations on the full dev run (identified variant; every row is a ledger entry):
    # one parameter changed at a time against the default. gamma=0 is the no-votes aggregation
    # (the prompt still showed vote counts), gamma=1 the strong-votes one; beta=1 the full
    # price-since-thesis discount; use_author the causal author prior; min_evidence=50 a
    # sourcing gate; use_comments=false drops the discussion term; the thesis gates and the
    # net corroboration reach the first formula's choices; use_specificity puts it back in;
    # direction abs / none drop the sign of the stance; flat_quality (with direction none)
    # is the funnel-only control: the vote-weighted count of graded threads, no grades at all.
    "ablations": [["env", "THESIS_VARIANT=identified", f"THESIS_PARAMS={params}",
                   *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")]
                  for params in ('{"gamma": 0}', '{"gamma": 1}', '{"beta": 1}', '{"use_author": true}',
                                 '{"min_evidence": 50}', '{"use_comments": false}',
                                 '{"thesis_gate": "hard"}', '{"thesis_gate": "soft"}',
                                 '{"corroboration": "net"}', '{"use_specificity": true}',
                                 '{"direction": "abs"}', '{"direction": "none"}',
                                 '{"flat_quality": true}', '{"flat_quality": true, "direction": "none"}')],
    # the checks on the ablation winner (score = vote-weighted count of graded threads, no
    # direction, no grades): the LLM-free 24h-window control (`substantive` baseline), then
    # the winner without votes, without late content, and both; the graded no-direction
    # variant blinded, without late content and without votes; and the calibration tables
    # of both no-direction configurations. No spend.
    "finalist-checks": [_py("backtest.py", "run", "--baseline", "substantive", "--split", "dev"),
                        *[["env", f"THESIS_VARIANT={variant}", f"THESIS_PARAMS={params}",
                           *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")]
                          for variant, params in (
                              ("identified", '{"flat_quality": true, "direction": "none", "exclude_late": true}'),
                              ("identified", '{"flat_quality": true, "direction": "none", "gamma": 0}'),
                              ("identified", '{"flat_quality": true, "direction": "none", "gamma": 0, "exclude_late": true}'),
                              ("blinded", '{"direction": "none"}'),
                              ("identified", '{"direction": "none", "exclude_late": true}'),
                              ("identified", '{"direction": "none", "gamma": 0}'))],
                        ["env", "THESIS_VARIANT=identified", 'THESIS_PARAMS={"direction": "none"}',
                         *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                        ["env", "THESIS_VARIANT=identified", 'THESIS_PARAMS={"flat_quality": true, "direction": "none"}',
                         *_py("thesis_scorer.py", "calibrate", "--split", "dev")]],
    # the substantive baseline with one rule switched off per run: drop_deleted first (the
    # one rule with a look-ahead flavour: an author deleted later is missing from the dump),
    # then the subject rule, the two-company cap and the substance rule. No spend.
    "substantive-checks": [_py("backtest.py", "run", "--baseline", "substantive", "--split", "dev", "--rules", rules)
                           for rules in ('{"drop_deleted": false}', '{"subject": false}',
                                         '{"max_companies": 10}', '{"substance": false}')],
    # the re-grade after the cross-post merge (user decision 2026-10-22): a new census (threads
    # with copies), both variants graded (only merged threads are new to the cache), collected,
    # the blinding checked, the gold set re-evaluated with the gated-formula grid, and the
    # browser export refreshed. Spends cents (the merged threads only).
    "regrade": [_py("thesis_funnel.py"),
                _py("thesis_llm.py", "run", "--variant", "identified"),
                _py("thesis_llm.py", "run", "--variant", "blinded"),
                _py("thesis_llm.py", "collect", "--variant", "identified"),
                _py("thesis_llm.py", "collect", "--variant", "blinded"),
                _py("thesis_llm.py", "check-blind"),
                _py("goldset.py", "evaluate", "--tune"),
                _py("thread_export.py", "--push")],
    # the gated formula on the full dev after the re-grade: signed, direction none, the
    # blinded direction-none check, the additive formula on the merged threads for the
    # like-for-like, both calibration tables, and the browser export. No spend.
    "gated-dev": [*[["env", f"THESIS_VARIANT={variant}", f"THESIS_PARAMS={params}",
                     *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")]
                    for variant, params in (("identified", '{"quality": "gated"}'),
                                            ("identified", '{"quality": "gated", "direction": "none"}'),
                                            ("blinded", '{"quality": "gated", "direction": "none"}'),
                                            ("identified", '{"direction": "none"}'),
                                            ("identified", '{}'))],
                  ["env", "THESIS_VARIANT=identified", 'THESIS_PARAMS={"quality": "gated"}',
                   *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                  ["env", "THESIS_VARIANT=identified", 'THESIS_PARAMS={"quality": "gated", "direction": "none"}',
                   *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                  _py("thread_export.py", "--push")],
    # thesis_v2 (user request 2026-10-23): the grader returns its own final direction (1-5) and
    # credibility (1-10) next to the component grades. Every command carries THESIS_PROMPT so the
    # presets are right whether or not the service variable is set, and nothing of v1 is touched
    # (its own cache dir, grades files with a .thesis_v2 suffix, its own ledger prompt version).
    # v2-gold first (cents): the cost estimate of the full set, then the 30 gold threads against
    # the user's ratings with the direct credibility next to the formulas. v2-full (about $3.4 on
    # Jev): both variants graded, collected, blinding checked, gold table again, the direct
    # scorer signed and without direction on both variants, signed without late content, the two
    # calibration tables, and the browser export (the browser then opens on the 5 x 10 grid).
    "v2-gold": [[*_V2, *_py("thesis_llm.py", "run", "--variant", "identified", "--dry-run")],
                [*_V2, *_py("goldset.py", "evaluate", "--tune")]],
    "v2-full": [[*_V2, *_py("thesis_llm.py", "run", "--variant", "identified")],
                [*_V2, *_py("thesis_llm.py", "run", "--variant", "blinded")],
                [*_V2, *_py("thesis_llm.py", "collect", "--variant", "identified")],
                [*_V2, *_py("thesis_llm.py", "collect", "--variant", "blinded")],
                [*_V2, *_py("thesis_llm.py", "check-blind")],
                [*_V2, *_py("goldset.py", "evaluate", "--tune")],
                *[[*_V2, f"THESIS_VARIANT={variant}", f"THESIS_PARAMS={params}",
                   *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")]
                  for variant, params in (("identified", '{"quality": "direct"}'),
                                          ("identified", '{"quality": "direct", "direction": "none"}'),
                                          ("blinded", '{"quality": "direct"}'),
                                          ("blinded", '{"quality": "direct", "direction": "none"}'),
                                          ("identified", '{"quality": "direct", "exclude_late": true}'))],
                [*_V2, "THESIS_VARIANT=identified", 'THESIS_PARAMS={"quality": "direct"}',
                 *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                [*_V2, "THESIS_VARIANT=identified", 'THESIS_PARAMS={"quality": "direct", "direction": "none"}',
                 *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                [*_V2, *_py("thread_export.py", "--push")]],
    # v2-full's identified half and blinded half as separate jobs: the first full run graded the
    # identified variant and then hit TypeSafe's "no available API credits" (HTTP 402) on the
    # blinded one (2026-10-23), so the identified results are produced now (no spend: the grades
    # are cached) and the blinded half runs once the user has topped the TypeSafe account up.
    "v2-identified": [[*_V2, *_py("thesis_llm.py", "collect", "--variant", "identified")],
                      [*_V2, *_py("goldset.py", "evaluate", "--tune")],
                      *[[*_V2, "THESIS_VARIANT=identified", f"THESIS_PARAMS={params}",
                         *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")]
                        for params in ('{"quality": "direct"}', '{"quality": "direct", "direction": "none"}',
                                       '{"quality": "direct", "exclude_late": true}')],
                      [*_V2, "THESIS_VARIANT=identified", 'THESIS_PARAMS={"quality": "direct"}',
                       *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                      [*_V2, "THESIS_VARIANT=identified", 'THESIS_PARAMS={"quality": "direct", "direction": "none"}',
                       *_py("thesis_scorer.py", "calibrate", "--split", "dev")],
                      [*_V2, *_py("thread_export.py", "--push")]],
    "v2-blinded": [[*_V2, *_py("thesis_llm.py", "run", "--variant", "blinded")],
                   [*_V2, *_py("thesis_llm.py", "collect", "--variant", "blinded")],
                   [*_V2, *_py("thesis_llm.py", "check-blind")],
                   *[[*_V2, "THESIS_VARIANT=blinded", f"THESIS_PARAMS={params}",
                      *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev")]
                     for params in ('{"quality": "direct"}', '{"quality": "direct", "direction": "none"}')]],
    # Reddit darlings (user decision 2026-10-26): only companies with an informed bull case that
    # independent commenters backed; counts per year first, then outcomes vs the S&P and a
    # liquidity x momentum matched control, once. From the thesis_v2 grades already paid for.
    "darlings": [[*_V2, *_py("darlings.py", "run")]],
    # where the darlings are: counts per year across 2012-2024, no model, no returns (free)
    "darling-census": [_py("darling_census.py")],
    # the darling study on 2019-2024 (user decisions 2026-10-27: TypeSafe topped up; the sealed
    # 2021-2022 holdout is given up for it): the candidates (several authors, heavy discussion,
    # eligible stocks only), their top threads at each candidate cutoff, graded with thesis_v2
    # both ways into data/features/darlings (the dev grades are untouched), the blinding checked,
    # then the darling rule and its outcomes, identified and blinded. About $1 on Jev.
    "darlings-recent": [_py("darling_census.py", "--years", "2019", "2024", "--write",
                            "data/features/darlings/candidates.parquet", "--write-level", "C"),
                        _py("thesis_funnel.py", "--only", "data/features/darlings/candidates.parquet",
                            "--out", "data/features/darlings/threads.parquet"),
                        *[[*_V2, *_py("thesis_llm.py", "--threads", "data/features/darlings/threads.parquet",
                                      "--features", "data/features/darlings", step, "--variant", v)]
                          for step in ("run", "collect") for v in ("identified", "blinded")],
                        [*_V2, *_py("thesis_llm.py", "--threads", "data/features/darlings/threads.parquet", "check-blind")],
                        *[[*_V2, *_py("darlings.py", "run", "--split", "recent", "--grades-dir", "data/features/darlings",
                                      "--variant", v)] for v in ("identified", "blinded")]],
    # the pilot's like-for-like rows, no spend: every baseline on the same two years, and the
    # thesis scorer with threads holding late content excluded (the hindsight check)
    "pilot-checks": [_py("backtest.py", "ladder", "--split", "dev", "--years", "2014", "2017"),
                     ["env", "THESIS_VARIANT=identified", 'THESIS_PARAMS={"exclude_late": true}',
                      *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev",
                           "--years", "2014", "2017")],
                     ["env", "THESIS_VARIANT=blinded", 'THESIS_PARAMS={"exclude_late": true}',
                      *_py("backtest.py", "run", "--scorer", "scripts/thesis_scorer.py:factory", "--split", "dev",
                           "--years", "2014", "2017")]],
}
# The full rebuild after a filter / ticker-list / resolver / price-cleaning change (CLAUDE.md).
PRESETS["rebuild"] = [c for name in ("export-tickers", "refilter", "corpus", "wordstats",
                                     "mentions", "universe", "splits", "universe", "baselines",
                                     "ladder-dev", "ladder-validate") for c in PRESETS[name]]
# Presets that accept JOB_ARGS (appended to their single command).
TAKES_ARGS = {"run", "ladder-dev", "ladder-validate", "pull-data", "census", "llm-run", "llm-collect",
              "gold-build", "gold-eval", "thesis-timeline", "calibrate", "thesis-run", "export-threads",
              "darlings", "darling-census"}


class JobError(ValueError):
    pass


def resolve(job: str, args: str = "", env: dict | None = None,
            scripts_dir: Path = ROOT / "scripts") -> list[list[str]]:
    """The commands for a job request, or JobError if it is not allowed."""
    env = env or {}
    extra = shlex.split(args or "")
    if job.startswith("script:"):
        name = job.split(":", 1)[1]
        path = scripts_dir / f"{name}.py"
        if not name or "/" in name or name.startswith(".") or name == "jobs" or not path.is_file():
            raise JobError(f"no such script: scripts/{name}.py")
        cmds = [_py(f"{name}.py", *extra)]
    elif job in PRESETS:
        if extra and job not in TAKES_ARGS:
            raise JobError(f"job {job!r} takes no JOB_ARGS")
        cmds = [list(c) for c in PRESETS[job]]
        if extra:
            cmds[-1] += extra
    else:
        raise JobError(f"unknown job {job!r}; presets: {', '.join(sorted(PRESETS))}, "
                       "or script:<name>")
    if job == "run" and not any(a in extra for a in ("--baseline", "--scorer")):
        raise JobError("job 'run' needs JOB_ARGS with --baseline NAME or --scorer FILE:FACTORY")
    flat = [a for c in cmds for a in c]
    touches_holdout = "holdout" in flat or "--accept-contaminated" in flat
    if touches_holdout and env.get("ALLOW_HOLDOUT") != "1":
        raise JobError("this job touches the sealed holdout; the user must set ALLOW_HOLDOUT=1")
    return cmds


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def finished_ids(ledger: Path) -> set[str]:
    if not ledger.exists():
        return set()
    out = set()
    for line in ledger.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("event") == "finished":
            out.add(rec.get("job_id"))
    return out


def _append(ledger: Path, rec: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def _git_commit() -> str | None:
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    if sha:
        return sha[:7]
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5, cwd=ROOT)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_child: subprocess.Popen | None = None


def _on_term(signum, frame):  # forward a redeploy's SIGTERM to the running step, then stop
    if _child and _child.poll() is None:
        _child.terminate()
    raise SystemExit(128 + signum)


def run_commands(cmds: list[list[str]], log_path: Path, cwd: Path = ROOT) -> int:
    """Run the steps in order, stopping at the first failure. Output goes to stdout and the log."""
    global _child
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with log_path.open("a") as log:
        def emit(line: str) -> None:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()

        for i, cmd in enumerate(cmds, 1):
            emit(f"[jobs] step {i}/{len(cmds)}: {shlex.join(cmd)}\n")
            t0 = time.time()
            try:
                _child = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True, bufsize=1)
            except OSError as e:
                emit(f"[jobs] could not start: {e}\n")
                return 127
            assert _child.stdout is not None
            for line in _child.stdout:
                emit(line)
            code = _child.wait()
            _child = None
            emit(f"[jobs] step {i} exit {code} after {time.time() - t0:.0f}s\n")
            if code != 0:
                return code
    return 0


def handle(env: dict, data: Path = ROOT / "data", scripts_dir: Path = ROOT / "scripts") -> int | None:
    """Run the requested job if there is one. Returns its exit code, or None if nothing ran."""
    job = (env.get("JOB") or "").strip()
    job_id = (env.get("JOB_ID") or "").strip()
    ledger = data / "logs" / "jobs.jsonl"
    if not job:
        print("[jobs] no job requested (JOB is empty)", flush=True)
        return None
    if not job_id:
        print(f"[jobs] JOB={job} but JOB_ID is empty; set a new JOB_ID to run it", flush=True)
        return None
    if job_id in finished_ids(ledger):
        print(f"[jobs] JOB_ID {job_id} already finished; set a new JOB_ID to run again", flush=True)
        return None
    try:
        cmds = resolve(job, env.get("JOB_ARGS", ""), env, scripts_dir)
    except JobError as e:
        print(f"[jobs] refused: {e}", flush=True)
        # A refusal does not consume the JOB_ID: an older queued deployment running older code
        # can refuse a preset the newer code knows, and the newer deployment must still run it.
        _append(ledger, {"event": "refused", "job_id": job_id, "job": job, "ts": _now(),
                         "exit": None, "refused": str(e)})
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = job.replace(":", "_").replace("/", "_")
    log_path = data / "logs" / f"{stamp}_{safe}.log"
    base = {"job_id": job_id, "job": job, "args": env.get("JOB_ARGS", ""), "git": _git_commit(),
            "log": str(log_path.relative_to(data.parent)) if data.parent in log_path.parents
            else str(log_path)}
    _append(ledger, {"event": "started", "ts": _now(), **base})
    t0 = time.time()
    code = run_commands(cmds, log_path)
    _append(ledger, {"event": "finished", "ts": _now(), "exit": code,
                     "seconds": round(time.time() - t0), **base})
    print(f"[jobs] JOB_ID {job_id} ({job}) finished with exit {code}; log {base['log']}", flush=True)
    return code


def main() -> None:
    signal.signal(signal.SIGTERM, _on_term)
    os.chdir(ROOT)
    handle(dict(os.environ))
    if os.environ.get("JOBS_NO_IDLE") == "1":
        return
    print("[jobs] idle", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
