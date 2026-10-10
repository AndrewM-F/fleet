"""Root-authored, bounded PR successor planning; no review or merge bypass."""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import paths
from .store import emit

_next_poll = 0.0
_repo_index = 0
FIELDS = "number,state,headRefOid,isDraft,mergeStateStatus,statusCheckRollup,url"
FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STALE"}
PASS = {"SUCCESS", "SKIPPED", "NEUTRAL"}
SAFE = (
    "Use the existing authenticated gh CLI and this PR's owned worktree. "
    "Preserve other workers' changes. Stay within the approved PR scope. "
    "No new credentials, access changes, production deployment, new repositories, "
    "force push, branch-protection bypass, weakened checks, or unrelated work. "
    "Run focused tests and applicable checks. Publish unfinished repairs only as draft. "
    "Return the exact head, actual diff, test results, and unresolved blockers for root review. "
    "Root personally reviews new code before ready or merge."
)


def enqueue(db, supplied):
    # Lazy import avoids a control -> manager -> planner import cycle.
    from .control import enqueue as add_tasks
    return add_tasks(db, supplied)


def classify(pr, reviewed_head=None):
    if pr.get("state") == "MERGED":
        return "merged", []
    if pr.get("state") != "OPEN":
        return "closed", []
    if not re.fullmatch(r"[0-9a-f]{40}", pr.get("headRefOid", "")):
        return "unknown", []
    failures, pending, succeeded, cancelled = [], False, False, False
    checks = pr.get("statusCheckRollup") or []
    for check in checks:
        if check.get("__typename") == "StatusContext":
            result = check.get("state")
        else:
            result = check.get("conclusion") if check.get("status") == "COMPLETED" else None
        if result == "SUCCESS":
            succeeded = True
        if result in FAIL:
            failures.append((check.get("name") or check.get("context") or "unnamed",
                             check.get("detailsUrl") or check.get("targetUrl") or ""))
        elif result == "CANCELLED":
            cancelled = True
        elif result not in PASS:
            pending = True
    failures.sort()
    if pr.get("mergeStateStatus") == "DIRTY":
        return "repair_conflicts", failures
    if failures:
        return "repair_ci", failures
    if pending:
        return "waiting_ci", []
    if cancelled:
        return ("needs_review" if pr.get("isDraft") else "needs_checks"), []
    if not checks:
        return ("finish_draft" if pr.get("isDraft") else "needs_checks"), []
    if not succeeded:
        return "needs_checks", []
    if reviewed_head == pr["headRefOid"]:
        return "merge", []
    return "needs_review", []


def validate(data, slots):
    if data.get("author") != "root" or data.get("enabled") is not True:
        return []
    missions, owners, prs = data.get("missions", []), set(), set()
    for mission in missions:
        repo, number, sid = mission.get("repo", ""), mission.get("pr"), mission.get("sessionId")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid PR repository")
        if type(number) is not int or number < 1 or sid not in slots:
            raise ValueError("Invalid PR or owner")
        if mission.get("rootAuthorized") is not True or sid in owners or (repo, number) in prs:
            raise ValueError("PR ownership must be explicitly authorized and unique")
        head = mission.get("reviewedHead")
        if head and not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("Invalid reviewed head")
        nxt = mission.get("nextTask")
        if nxt and (nxt.get("rootAuthorized") is not True or not nxt.get("id") or not nxt.get("message")):
            raise ValueError("Successor must be explicitly authorized")
        owners.add(sid)
        prs.add((repo, number))
    return missions


def task_for(mission, pr, phase, failures):
    repo, number, sid = mission["repo"], mission["pr"], mission["sessionId"]
    if phase == "merged":
        nxt = mission.get("nextTask")
        if not nxt:
            return None
        return {"id": nxt["id"], "sessionId": sid, "message": nxt["message"], "rootAuthorized": True}
    if phase not in {"repair_conflicts", "repair_ci", "finish_draft", "merge"}:
        return None
    head = pr["headRefOid"]
    key = json.dumps([repo, number, sid, head, phase, failures], sort_keys=True)
    tid = "pr-loop-" + hashlib.sha256(key.encode()).hexdigest()[:24]
    if phase == "merge":
        message = (
            f"Root reviewed {repo}#{number} at exact head {head}. Recheck the remote head "
            "and every applicable CI check immediately before acting. If the head changed, "
            "stop for root review. If any check is missing, pending or failed, do not merge. "
            "If all applicable checks passed, mark this reviewed draft ready if needed, "
            f"then squash-merge using gh with --match-head-commit {head}, without bypassing "
            "protections. After marking ready, re-fetch checks and wait for every newly triggered check "
            "before merging; do not assume the earlier green snapshot still suffices. "
            "Verify the PR's merged state and merge commit. Close only issues "
            "whose acceptance is actually completed. Do not start a milestone task yourself; "
            "the planner will dispatch its explicitly approved successor after verified merge. "
            "Do not edit code in this merge-only task."
        )
    else:
        focus = {
            "repair_conflicts": "Finish the main-branch integration and resolve owned conflicts.",
            "repair_ci": "Diagnose and repair the current failed CI checks: " +
                         ", ".join(name for name, _ in failures) + ".",
            "finish_draft": "Finish the remaining approved implementation and publish a reviewable candidate.",
        }[phase]
        message = (f"Continue the existing mission for {repo}#{number}, observed head {head}. "
                   + focus + " " + SAFE)
    return {"id": tid, "sessionId": sid, "message": message, "rootAuthorized": True}


def fetch(repo, numbers):
    # Avoid nested check rollups for 100 historical PRs: observe open work first,
    # then explicitly retrieve owned PRs absent from that bounded list.
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "30", "--json", FIELDS],
        capture_output=True, text=True, timeout=20, check=True,
    )
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("Invalid gh response")
    observed = {row["number"]: row for row in rows}
    for number in sorted(set(numbers) - observed.keys()):
        result = subprocess.run(
            ["gh", "pr", "view", str(number), "--repo", repo, "--json", FIELDS],
            capture_output=True, text=True, timeout=20, check=True,
        )
        row = json.loads(result.stdout)
        if not isinstance(row, dict) or row.get("number") != number:
            raise ValueError("Invalid owned PR response")
        observed[number] = row
    return observed


def write_state(path, state):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(temporary, path)


def tick(db, slots, states, now=None):
    """Poll one repo per minute. Existing dispatcher remains the only sender."""
    global _next_poll, _repo_index
    now = time.time() if now is None else now
    config_path = paths.DATA / "pr-missions.json"
    if paths.PAUSE.exists() or now < _next_poll or not config_path.exists():
        return
    _next_poll = now + 60
    try:
        data = json.loads(config_path.read_text())
        missions = validate(data, slots)
        if not missions:
            return
        repos = sorted({m["repo"] for m in missions})
        repo = repos[_repo_index % len(repos)]
        _repo_index += 1
        observations = fetch(repo, [m["pr"] for m in missions if m["repo"] == repo])
        ledger_path = paths.DATA / "pr-planner.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        for mission in [m for m in missions if m["repo"] == repo]:
            sid, number = mission["sessionId"], mission["pr"]
            key = f"{repo}#{number}"
            pr = observations.get(number)
            if pr is None:
                phase, failures = "not_observed", []
            else:
                phase, failures = classify(pr, mission.get("reviewedHead"))
            snapshot = {"phase": phase, "head": pr.get("headRefOid") if pr else None,
                        "failures": failures, "sessionId": sid}
            signature = json.dumps(snapshot, sort_keys=True)
            previous = ledger.get(key, {})
            if previous.get("signature") != signature:
                emit(db, "pr_queue_progress", pr=key, sessionId=sid, phase=phase,
                     head=snapshot["head"])
            task = task_for(mission, pr, phase, failures) if pr else None
            idle = states.get(sid, {}).get("state") in {"idle", "done", "completed"}
            unfinished = db.execute(
                "SELECT 1 FROM tasks WHERE sid=? AND state NOT IN ('finished','cancelled') LIMIT 1",
                (sid,),
            ).fetchone()
            if task and idle and not unfinished:
                existing = db.execute("SELECT state FROM tasks WHERE id=?", (task["id"],)).fetchone()
                if existing is None:
                    added = enqueue(db, [task])
                    if not added[0]["alreadyQueued"]:
                        emit(db, "pr_successor_queued", pr=key, sessionId=sid, taskId=task["id"], phase=phase)
                elif phase != "merged" and existing[0] == "finished" and previous.get("stalledTask") != task["id"]:
                    emit(db, "pr_needs_root", pr=key, sessionId=sid, taskId=task["id"],
                         reason="Worker finished without changing the observed PR outcome")
                    previous["stalledTask"] = task["id"]
            ledger[key] = dict(previous, signature=signature, observedAt=now, **snapshot)
        write_state(ledger_path, ledger)
    except Exception as error:
        # Do not leak gh stderr, tokens, or raw configuration into notifications.
        emit(db, "pr_planner_error", errorType=type(error).__name__)
        _next_poll = now + 120

