"""Receipts for the W5 session-lifecycle acceptance: dry run, rollout evidence, withdrawal.

Everything here runs in a temporary root with a synthetic configuration, a
synthetic native session inventory and the same fake OpenCode CLI the test
suite uses. The live fleet, the live configuration and the live native database
are never touched.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from fleet import opencode_lifecycle, paths, store  # noqa: E402
from test_opencode_lifecycle import FAKE_CLI  # noqa: E402

MODEL = "opencode-go/minimax-m3.1-flash-free"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "config").mkdir()
        (root / "data").mkdir()
        (root / "wt" / "repo").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root / "wt" / "repo")], check=True)
        native = root / "opencode.db"
        with sqlite3.connect(native) as db:
            db.execute("CREATE TABLE session_v2 (id TEXT, directory TEXT, project_id TEXT, model TEXT)")
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)",
                       ("ses_EXISTING000000000001", str(root / "wt" / "repo"), "p", MODEL))
        control = root / "control.json"
        control.write_text(json.dumps({"mode": "ok", "sessionId": "ses_RECEIPT00000000000001"}))
        log = root / "fake-cli.log"
        fake = root / "fake-opencode"
        fake.write_text(FAKE_CLI.format(control=str(control), database=str(native), log=str(log)))
        fake.chmod(0o755)
        config = {"schemaVersion": 2, "worktreeRoot": str(root / "wt"),
                  "executables": {"opencode": str(fake)}, "maxConcurrent": 8,
                  "minQueuedPerWorker": 0, "pi": [], "claude": [],
                  "opencode": [{"sessionId": "ses_EXISTING000000000001",
                                "cwd": str(root / "wt" / "repo"),
                                "sessionCwd": str(root / "wt" / "repo"),
                                "databasePath": str(native), "model": MODEL,
                                "provider": "opencode", "dispatchEnabled": True}]}
        (root / "config" / "fleet.local.json").write_text(json.dumps(config))
        for name, value in (("ROOT", root), ("CONFIG", root / "config" / "fleet.local.json"),
                            ("DATA", root / "data"), ("LOGS", root / "logs"),
                            ("DB", root / "data" / "fleet.sqlite"), ("QUEUE", root / "data" / "queue.json"),
                            ("QUEUE_LOCK", root / "data" / "queue.lock"),
                            ("MANAGER_LOCK", root / "data" / "manager.lock"),
                            ("OPENCODE", root / "data" / "opencode")):
            setattr(paths, name, value)
        db = store.database(paths.DB)

        print("=== RECEIPT 1: the dry run of the total-25 request (writes nothing) ===")
        before = paths.CONFIG.read_bytes()
        plan = opencode_lifecycle.add_sessions(
            db, count=None, target_total=25, model=MODEL, cwd=str(root / "wt" / "repo"),
            root=str(root), request_key="receipt-total-25", dry_run=True)
        print(json.dumps(plan, indent=2, sort_keys=True))
        rows = db.execute("SELECT count(*) FROM opencode_provisioning").fetchone()[0]
        sessions = db.execute("SELECT count(*) FROM opencode_sessions").fetchone()[0]
        print(f"  config unchanged: {paths.CONFIG.read_bytes() == before}")
        print(f"  provisioning rows written: {rows}   session receipts written: {sessions}")

        print()
        print("=== RECEIPT 2: the same request in the sandbox, then the withdrawal ===")
        result = opencode_lifecycle.add_sessions(
            db, count=None, target_total=25, model=MODEL, cwd=str(root / "wt" / "repo"),
            root=str(root), request_key="receipt-total-25-live")
        print(json.dumps({k: result[k] for k in
                          ("requested", "rosterTotalBefore", "created", "registered", "status",
                           "logicalSessions", "admissionLimit", "heavyJobSlots", "proof",
                           "apiSupport")}, indent=2, sort_keys=True))
        print("  roster size now:", len(paths.config()["opencode"]))
        drain = opencode_lifecycle.drain_session(db, result["sessions"][0]["sessionId"],
                                                  note="receipt: drain")
        print("  drain:", json.dumps({k: drain[k] for k in
                                     ("sessionId", "draining", "outstanding", "awaitingImport",
                                      "removed", "honouredByThisBuild")}, sort_keys=True))
        removal = opencode_lifecycle.remove_session(db, result["sessions"][0]["sessionId"],
                                                    note="receipt: withdraw")
        print("  removal:", json.dumps({k: removal[k] for k in
                                        ("sessionId", "removed", "nativeSessionKept",
                                         "logicalSessions", "heavyJobSlots")}, sort_keys=True))
        print("  roster size after the withdrawal:", len(paths.config()["opencode"]))
        print("  the native session is still in the account:",
              sqlite3.connect(native).execute(
                  "SELECT count(*) FROM session_v2 WHERE id=?",
                  (result["sessions"][0]["sessionId"],)).fetchone()[0] == 1)
        db.close()


if __name__ == "__main__":
    main()
