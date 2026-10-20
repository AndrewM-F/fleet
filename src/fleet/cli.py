"""`bin/fleet` command line."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from . import claude, control, events, holds, manager, opencode, ops, paths, pi, recovery
from .store import BUSY, database


def out(value):
    print(json.dumps(value, indent=2) if not isinstance(value, str) else value, flush=True)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, TypeError, ValueError):
        return False


def manager_running():
    paths.DATA.mkdir(parents=True, exist_ok=True)
    with paths.MANAGER_LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
        return False


def controller_record(lane):
    try:
        return json.loads((paths.lane_dir(lane) / "controller.json").read_text())
    except FileNotFoundError:
        return {}


def processes():
    pi_slots, _ = paths.slots()
    lanes = {}
    for slot in pi_slots:
        record = controller_record(slot["lane"])
        lanes[slot["lane"]] = {"running": pi.running(slot["lane"]),
                               "controllerPid": record.get("controller_pid"),
                               "piPid": record.get("pi_pid")}
    status = {}
    if paths.STATUS.exists():
        status = json.loads(paths.STATUS.read_text())
    return {"manager": {"running": manager_running(), "pid": status.get("pid")}, "lanes": lanes}


DISABLED = paths.DATA / "supervisor.disabled"


def cmd_start(args):
    paths.slots()  # validate configuration before launching anything
    DISABLED.unlink(missing_ok=True)
    started = []
    for slot in paths.slots()[0]:
        if args.lane and slot["lane"] not in args.lane:
            continue
        (paths.lane_dir(slot["lane"]) / "disabled").unlink(missing_ok=True)
        started.append(pi.start(slot["lane"]))
    result = {"paused": paths.PAUSE.exists(), "controllers": started}
    if not args.controllers_only:
        if manager_running():
            result["manager"] = {"alreadyRunning": True}
        else:
            log_dir = paths.LOGS / "manager"
            log_dir.mkdir(parents=True, exist_ok=True)
            paths.rotate(log_dir / "fleet.log")
            with (log_dir / "fleet.log").open("a") as log:
                child = subprocess.Popen([sys.executable, str(paths.BIN), "run"],
                                         cwd=paths.ROOT, stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=log, start_new_session=True)
            result["manager"] = {"pid": child.pid}
    out(result)


def cmd_stop(args):
    pi_slots, _ = paths.slots()
    if args.lane:
        pi_slots = [s for s in pi_slots if s["lane"] in args.lane]
        args.manager_only = False
        for slot in pi_slots:  # keep the supervisor from reviving a deliberately stopped lane
            (paths.lane_dir(slot["lane"]) / "disabled").write_text("fleet stop --lane\n")
    else:
        DISABLED.write_text("stopped by `fleet stop`; `fleet start` re-enables supervision\n")
    if not args.force:
        busy = []
        for slot in pi_slots:
            if not pi.running(slot["lane"]):
                continue
            try:
                state = pi.query(slot)
            except Exception as error:
                busy.append({"lane": slot["lane"], "error": str(error)})
                continue
            if not state or state["state"] in BUSY:
                busy.append({"lane": slot["lane"], "state": state})
        if busy:
            out({"refused": "Pi lanes are busy or unverifiable; retry later or pass --force",
                 "lanes": busy})
            sys.exit(2)
    targets = []
    if manager_running() and not args.lane:
        status = json.loads(paths.STATUS.read_text()) if paths.STATUS.exists() else {}
        if not alive(status.get("pid")):
            out({"error": "Manager lock is held but its pid is unknown; inspect with ps"})
            sys.exit(1)
        targets.append(("manager", status["pid"]))
    if not args.manager_only:
        for slot in pi_slots:
            if pi.running(slot["lane"]):
                targets.append(("controller:" + slot["lane"],
                                controller_record(slot["lane"]).get("controller_pid")))
    for _, pid in targets:
        if alive(pid):
            os.kill(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = processes()
        lanes = {s["lane"] for s in pi_slots}
        if (args.lane or not state["manager"]["running"]) and (args.manager_only or not any(
                v["running"] or alive(v["piPid"]) for k, v in state["lanes"].items() if k in lanes)):
            break
        time.sleep(.5)
    final = processes()
    working = [sid for sid in (p.name for p in paths.CLAUDE.iterdir() if p.is_dir())
               if (paths.CLAUDE / sid / "current.json").exists()
               and _claude_working(sid)] if paths.CLAUDE.exists() else []
    out({"stopped": [name for name, _ in targets], "processes": final,
         "claudeTurnsStillRunning": working})


def _claude_working(sid):
    _, claude_slots = paths.slots()
    slot = next((s for s in claude_slots if s["sessionId"] == sid), None)
    if not slot:
        return False
    try:
        return claude.print_state(slot)["state"] == "working"
    except Exception:
        return False


def cmd_status(args):
    status = json.loads(paths.STATUS.read_text()) if paths.STATUS.exists() else {}
    if args.raw:
        out(status)
        return
    db = database()
    out({"paused": paths.PAUSE.exists(), "processes": processes(),
         "statusUpdatedAt": status.get("updatedAt"),
         "statusAgeSeconds": int(time.time() - status["updatedAt"]) if status.get("updatedAt") else None,
         "stale": bool(status.get("updatedAt")) and time.time() - status["updatedAt"] > 90,
         "supervisor": {"installed": ops.plist_path().exists(), "disabled": DISABLED.exists()},
         "sessions": {sid: s.get("state") for sid, s in status.get("sessions", {}).items()},
         "tasks": dict(db.execute("SELECT state,count(*) FROM tasks GROUP BY state").fetchall()),
         "heldTasks": sorted(holds.held_ids(db)),
         "unackedEvents": db.execute("SELECT count(*) FROM events WHERE acked=0").fetchone()[0]})


def cmd_doctor(args):
    cfg = paths.config()
    pi_slots, claude_slots = paths.slots(cfg)
    opencode_slots = paths.opencode_slots(cfg)
    problems, warnings = [], []
    ids = [s["sessionId"] for s in pi_slots + claude_slots + opencode_slots]
    if len(ids) != len(set(ids)):
        problems.append("duplicate sessionId in configuration")
    configured_executables = (("pi", pi_slots), ("claude", claude_slots))
    if opencode_slots:
        configured_executables += (("opencode", opencode_slots),)
    for name, configured in configured_executables:
        if not configured:
            continue
        try:
            paths.executable(name, cfg)
        except RuntimeError as error:
            problems.append(str(error))
    for slot in pi_slots:
        try:
            pi.verify_session(slot)
        except Exception as error:
            problems.append(f"pi {slot['lane']}: {error}")
    for slot in claude_slots:
        if not Path(slot["cwd"]).is_dir():
            problems.append(f"claude {slot['sessionId']}: cwd missing {slot['cwd']}")
        hits = claude.native_transcript(slot["sessionId"])
        if not hits:
            if slot.get("resume", True):
                problems.append(f"claude {slot['sessionId']}: native transcript not found")
            continue
        expected = claude.project_slug(slot["cwd"])
        if all(h.parent.name != expected for h in hits):
            warnings.append(f"claude {slot['sessionId']}: transcript is under "
                            f"{hits[0].parent.name}, not {expected}; native --resume from the "
                            "canonical cwd may not find it (see docs/troubleshooting.md)")
    for slot in opencode_slots:
        try:
            opencode.verify_slot(slot, cfg)
            opencode.verify_native_session(slot["sessionId"], slot["cwd"], slot["databasePath"])
        except Exception as error:
            problems.append(f"opencode {slot.get('sessionId', '<unknown>')}: {error}")
        warnings.append(f"opencode {slot.get('sessionId', '<unknown>')}: dispatch remains opt-in; "
                        "confirm exact-model canary and sole Fleet ownership before enabling")
    paths.DATA.mkdir(parents=True, exist_ok=True)
    for path in (paths.DB,):
        db = sqlite3.connect(path)
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            problems.append(f"{path.name}: integrity {result}")
    out({"ok": not problems, "sessions": len(ids), "problems": problems, "warnings": warnings,
         "paused": paths.PAUSE.exists()})
    sys.exit(0 if not problems else 1)


def cmd_enqueue(args):
    tasks = json.loads(args.tasks)
    tasks = tasks if isinstance(tasks, list) else [tasks]
    out(control.cycle({"tasks": tasks, "timeout": 0}))


def cmd_control(args):
    try:
        print(json.dumps(control.cycle(json.loads(args.request))), flush=True)
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        sys.exit(1)


def cmd_completions(args):
    ids = [int(v) for v in args.ids.split(",") if v]
    print(json.dumps(events.completions(database(), args.action, args.after, args.limit,
                                        args.timeout, ids)), flush=True)


def cmd_resolve(args):
    db = database()
    if args.events:
        out(ops.resolve_events(db, [int(v) for v in args.events.split(",") if v], args.note))
    elif args.task:
        if not args.outcome:
            raise SystemExit("--as finished|cancelled required with --task")
        out(ops.resolve_task(db, args.task, args.outcome, args.note))
    else:
        out(ops.resolve_worker(db, args.worker, args.note))


def cmd_recovery_snapshot(args):
    out(recovery.worktree_snapshot(args.cwd))


def cmd_recover_opencode(args):
    sources = [bool(args.ownership_task), bool(args.adoption_id), bool(args.direct_turn_receipt)]
    if sum(sources) != 1:
        raise SystemExit("Choose exactly one ownership source: --ownership-task, "
                         "--adoption-id or --direct-turn-receipt")
    out(recovery.authorize_opencode(database(), session_id=args.worker,
                                    failed_task_id=args.task, turn=args.turn,
                                    recovery_id=args.recovery_id,
                                    successor_task_id=args.successor_task,
                                    ownership_task_id=args.ownership_task,
                                    owned_worktree=args.owned_worktree,
                                    branch=args.branch, head=args.head,
                                    worktree_sha256=args.worktree_sha256, note=args.note,
                                    adoption_id=args.adoption_id,
                                    direct_turn_receipt=args.direct_turn_receipt))


def cmd_adopt_ownership(args):
    try:
        out(recovery.adopt_ownership(database(), adoption_id=args.adoption_id,
                                     owner_task_id=args.owner_task,
                                     session_id=args.worker, failed_task_id=args.failed_task,
                                     turn=args.turn, owned_worktree=args.owned_worktree,
                                     branch=args.branch, head=args.head,
                                     worktree_sha256=args.worktree_sha256,
                                     decision=args.decision,
                                     evidence_reference=args.evidence_reference,
                                     ownership_evidence=args.ownership_evidence,
                                     note=args.note))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        sys.exit(1)


def cmd_reconcile_recovery(args):
    try:
        out(recovery.reconcile_successor(database(), args.task, args.note))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        sys.exit(1)


def cmd_hold(args):
    try:
        out(holds.hold(database(), args.task, args.note))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        sys.exit(1)


def cmd_release(args):
    try:
        out(holds.release(database(), args.task, args.note))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}), flush=True)
        sys.exit(1)


def cmd_controller(args):
    if args.action == "start":
        out(pi.start(args.lane))
    elif args.action == "serve":
        pi.serve(args.lane)
    else:
        out(pi.send(args.lane, json.loads(args.command)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fleet")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start", help="start lane controllers and the manager (pause unchanged)")
    p.add_argument("--lane", action="append")
    p.add_argument("--controllers-only", action="store_true")
    p.set_defaults(fn=cmd_start)
    sub.add_parser("pause", help="stop new dispatches").set_defaults(
        fn=lambda a: (paths.PAUSE.write_text("paused\n"), out({"paused": True})))
    sub.add_parser("resume", help="allow dispatch").set_defaults(
        fn=lambda a: (paths.PAUSE.unlink(missing_ok=True), out({"paused": False})))
    p = sub.add_parser("stop", help="stop manager and controllers; verify exit")
    p.add_argument("--force", action="store_true")
    p.add_argument("--manager-only", action="store_true")
    p.add_argument("--lane", action="append", help="stop only these lane controllers")
    p.set_defaults(fn=cmd_stop)
    p = sub.add_parser("status")
    p.add_argument("--raw", action="store_true")
    p.set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="validate configuration, sessions, executables, database").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("enqueue", help="queue root-authorized task(s) given as JSON")
    p.add_argument("tasks")
    p.set_defaults(fn=cmd_enqueue)
    p = sub.add_parser("control", help="enqueue+ack+wait in one call (JSON request)")
    p.add_argument("request")
    p.set_defaults(fn=cmd_control)
    p = sub.add_parser("completions", help="collect/wait/ack relay events with receipts")
    p.add_argument("action", choices=["collect", "wait", "ack"])
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=2)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--ids", default="")
    p.set_defaults(fn=cmd_completions)
    sub.add_parser("run", help="run the manager in the foreground").set_defaults(
        fn=lambda a: manager.run())
    p = sub.add_parser("wait", help="next unacknowledged event of any kind")
    p.add_argument("--timeout", type=float, default=300)
    p.set_defaults(fn=lambda a: print(json.dumps(manager.wait(a.timeout))))
    p = sub.add_parser("ack")
    p.add_argument("--event-id", type=int, required=True)
    p.set_defaults(fn=lambda a: manager.ack(a.event_id))
    p = sub.add_parser("controller", help="Pi lane transport (internal)")
    p.add_argument("action", choices=["start", "serve", "send"])
    p.add_argument("--lane", required=True)
    p.add_argument("--command")
    p.set_defaults(fn=cmd_controller)
    p = sub.add_parser("claude-turn", help="run one managed Claude turn (internal)")
    p.add_argument("request")
    p.set_defaults(fn=lambda a: claude.run_turn(Path(a.request).resolve()))
    p = sub.add_parser("opencode-turn", help="run one managed OpenCode turn (internal)")
    p.add_argument("request")
    p.set_defaults(fn=lambda a: opencode.run_turn(Path(a.request).resolve()))
    p = sub.add_parser("resolve", help="record an operator decision (events, uncertain task, failed worker)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--events", help="comma-separated event IDs to acknowledge")
    g.add_argument("--task")
    g.add_argument("--worker")
    p.add_argument("--as", dest="outcome", choices=["finished", "cancelled"])
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_resolve)
    p = sub.add_parser("recovery-snapshot",
                       help="read exact branch/head/partial-worktree fingerprint")
    p.add_argument("--cwd", required=True)
    p.set_defaults(fn=cmd_recovery_snapshot)
    p = sub.add_parser("recover-opencode",
                       help="authorize one durable successor for a verified failed OpenCode turn")
    p.add_argument("--worker", required=True)
    p.add_argument("--task", required=True, help="failed terminal task ID")
    p.add_argument("--turn", required=True, help="failed managed OpenCode turn UUID")
    p.add_argument("--recovery-id", required=True)
    p.add_argument("--successor-task", required=True)
    p.add_argument("--ownership-task", default="",
                   help="finished Fleet task that records the owner branch and worktree")
    p.add_argument("--owned-worktree", required=True,
                   help="exact owner Git worktree; the native session CWD remains unchanged")
    p.add_argument("--branch", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--worktree-sha256", required=True)
    p.add_argument("--adoption-id", default="",
                   help="immutable ownership adoption recorded by adopt-ownership")
    p.add_argument("--direct-turn-receipt", default="",
                   help="durable direct-turn ownership receipt when no owner task exists")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_recover_opencode)
    p = sub.add_parser("adopt-ownership",
                       help="append immutable ownership evidence for a recovery successor")
    p.add_argument("--id", dest="adoption_id", required=True)
    p.add_argument("--owner-task", required=True,
                   help="existing finished task that owns the checkout and branch")
    p.add_argument("--worker", required=True)
    p.add_argument("--failed-task", required=True, help="failed terminal task ID")
    p.add_argument("--turn", required=True, help="failed managed OpenCode turn UUID")
    p.add_argument("--owned-worktree", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--head", required=True, help="full HEAD commit SHA")
    p.add_argument("--worktree-sha256", required=True)
    p.add_argument("--decision", required=True, help="explicit operator ownership decision")
    p.add_argument("--evidence-reference", required=True,
                   help="locator (URL, path, message ID, or record ID) for the source to inspect")
    p.add_argument("--ownership-evidence", required=True,
                   help="substantive ownership excerpt/statement; a URL or checksum alone is rejected")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_adopt_ownership)
    p = sub.add_parser("reconcile-recovery",
                       help="release a claimed recovery reservation after a verified native stop")
    p.add_argument("--task", required=True, help="dispatched successor task owned by a recovery")
    p.add_argument("--note", required=True, help="operator evidence that native execution stopped")
    p.set_defaults(fn=cmd_reconcile_recovery)
    p = sub.add_parser("hold", help="hold an unstarted queued task without cancelling it")
    p.add_argument("task")
    p.add_argument("--note", required=True, help="operator reason kept with the hold")
    p.set_defaults(fn=cmd_hold)
    p = sub.add_parser("release",
                       help="release only this task-level hold; other dispatch holds stay in force")
    p.add_argument("task")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=cmd_release)
    p = sub.add_parser("cancel", help="cancel queued work or abort a sent/running turn; never resent")
    p.add_argument("task")
    p.add_argument("--note", required=True)
    p.set_defaults(fn=lambda a: out(ops.cancel(database(), a.task, a.note)))
    p = sub.add_parser("add-pi", help="add an existing Pi session as a lane")
    for flag in ("--lane", "--cwd", "--session-file", "--provider", "--model", "--thinking"):
        p.add_argument(flag, required=True)
    p.add_argument("--api-provider")
    p.add_argument("--role")
    p.set_defaults(fn=lambda a: out(ops.add_pi(a.lane, a.cwd, a.session_file, a.provider, a.model,
                                               a.thinking, a.api_provider, a.role)))
    p = sub.add_parser("add-claude", help="add a Claude session as a managed print worker")
    for flag in ("--session-id", "--cwd", "--model", "--effort"):
        p.add_argument(flag, required=True)
    p.add_argument("--new", action="store_true", help="session will be created on first turn")
    p.add_argument("--enable-dispatch", action="store_true")
    p.set_defaults(fn=lambda a: out(ops.add_claude(a.session_id, a.cwd, a.model, a.effort,
                                                   a.new, a.enable_dispatch)))
    p = sub.add_parser("remove", help="remove a worker from configuration (history kept)")
    p.add_argument("session_id")
    p.set_defaults(fn=lambda a: out(ops.remove(a.session_id)))
    sub.add_parser("supervise", help="restart dead components every 30s (launchd runs this)").set_defaults(
        fn=lambda a: ops.supervise())
    p = sub.add_parser("supervisor", help="install/uninstall the launchd supervisor")
    p.add_argument("action", choices=["install", "uninstall"])
    p.set_defaults(fn=lambda a: out(ops.install_launchd() if a.action == "install"
                                    else ops.uninstall_launchd()))
    args = parser.parse_args(argv)
    paths.DATA.mkdir(parents=True, exist_ok=True)
    args.fn(args)
