"""Relocation and restart coverage: everything resolves from the fleet root and survives restarts."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


def run_in(root, code):
    """Import the package from a relocated copy with FLEET_ROOT pointing at it."""
    env = dict(os.environ, FLEET_ROOT=str(root))
    env.pop("FLEET_CONFIG", None)
    return subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                          capture_output=True, text=True, check=True).stdout


class RelocatedRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "moved fleet"
        shutil.copytree(PROJECT / "src", self.root / "src")
        shutil.copytree(PROJECT / "bin", self.root / "bin")
        self.wt = Path(self.tmp.name) / "worktrees" / "repo" / "lane-a"
        self.wt.mkdir(parents=True)
        self.legacy = "/retired/path/lane-a"
        session = self.root / "data" / "lanes" / "a" / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"type": "session", "id": "pi-1", "cwd": self.legacy}) + "\n")
        cfg = {"schemaVersion": 2, "executables": {"pi": sys.executable, "claude": sys.executable},
               "pi": [{"lane": "a", "sessionId": "pi-1", "cwd": str(self.wt),
                       "sessionFile": str(session), "sessionCwd": self.legacy,
                       "provider": "x", "model": "m", "thinking": "high"}],
               "claude": [{"sessionId": "c-1", "cwd": str(self.wt), "sessionCwd": self.legacy,
                           "model": "m", "effort": "high", "mode": "print", "resume": True,
                           "dispatchEnabled": True}]}
        (self.root / "config").mkdir()
        (self.root / "config" / "fleet.local.json").write_text(json.dumps(cfg))

    def tearDown(self):
        self.tmp.cleanup()

    def test_paths_follow_fleet_root(self):
        out = json.loads(run_in(self.root, (
            "import json,sys;sys.path.insert(0,'src');from fleet import paths;"
            "print(json.dumps([str(paths.DB),str(paths.QUEUE),str(paths.CONFIG),"
            "str(paths.socket_path('a')),str(paths.lane_logs('a'))]))")))
        for value in out:
            self.assertTrue(value.startswith(str(self.root)) or value.startswith("/tmp/fleet-"), value)

    def test_legacy_session_cwd_is_accepted_but_foreign_is_not(self):
        run_in(self.root, (
            "import sys;sys.path.insert(0,'src');from fleet import paths,pi;"
            "s=paths.slots()[0][0];pi.verify_session(s);"
            "s['sessionCwd']='/elsewhere';"
            "\ntry:\n pi.verify_session(s)\nexcept RuntimeError: pass\n"
            "else: raise SystemExit('foreign cwd accepted')"))

    def test_claude_turn_from_before_relocation(self):
        turn = self.root / "data" / "claude" / "c-1" / "t1"
        turn.mkdir(parents=True)
        (turn.parent / "current.json").write_text(json.dumps({"turn": "t1", "startedAt": 0}))
        (turn / "state.json").write_text(json.dumps(
            {"sessionId": "c-1", "cwd": self.legacy, "state": "idle", "progress": 5}))
        out = json.loads(run_in(self.root, (
            "import json,sys;sys.path.insert(0,'src');from fleet import paths,claude;"
            "print(json.dumps(claude.print_state(paths.slots()[1][0])))")))
        self.assertEqual(out["state"], "idle")
        self.assertEqual(out["cwd"], str(self.wt))  # conflict checks use the canonical cwd
        (turn / "state.json").write_text(json.dumps(
            {"sessionId": "c-1", "cwd": "/somewhere/else", "state": "idle"}))
        with self.assertRaises(subprocess.CalledProcessError):
            run_in(self.root, ("import sys;sys.path.insert(0,'src');from fleet import paths,claude;"
                               "claude.print_state(paths.slots()[1][0])"))

    def test_restart_marks_inflight_dispatch_uncertain_once(self):
        code = ("import sys;sys.path.insert(0,'src');from fleet import store,manager,dispatch,paths;"
                "paths.DATA.mkdir(exist_ok=True);db=store.database();")
        run_in(self.root, code + (
            "db.execute(\"INSERT INTO tasks VALUES ('t','pi-1','/w','{\\\"message\\\":\\\"x\\\"}','dispatching',1)\");"
            "db.commit()"))
        for _ in range(2):  # two restarts: one uncertain event, never resent
            run_in(self.root, code + "manager.recover(db)")
        out = run_in(self.root, code + (
            "calls=[];dispatch.dispatch(db,{'pi-1':{'sessionId':'pi-1','provider':'pi'}},"
            "{'pi-1':{'state':'idle','cwd':'/w'}},lambda *a:calls.append(a));"
            "print(db.execute(\"select state from tasks\").fetchone()[0],"
            "db.execute(\"select count(*) from events where kind='task_uncertain'\").fetchone()[0],len(calls))"))
        self.assertEqual(out.split(), ["uncertain", "1", "0"])

    def test_queue_and_acks_survive_relocation(self):
        code = "import sys,json;sys.path.insert(0,'src');from fleet import store,control,paths,manager;"
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "queue.json").write_text(json.dumps({"author": "root", "tasks": []}))
        run_in(self.root, code + (
            "print(json.dumps(control.cycle({'tasks':[{'id':'q1','lane':'a','message':'m',"
            "'rootAuthorized':True}],'timeout':0})))"))
        moved = Path(self.tmp.name) / "moved again"
        shutil.move(str(self.root), moved)
        out = json.loads(run_in(moved, code + (
            "db=store.database();s=paths.slots();"
            "manager.import_queue(db,{x['sessionId']:x for x in s[0]+s[1]});"
            "e=store.emit(db,'review_ready',sessionId='pi-1',taskId='q1');"
            "control.cycle({'ack':[e],'timeout':0});"
            "print(json.dumps([db.execute('select state from tasks').fetchall(),"
            "db.execute('select acked from events').fetchall()]))")))
        self.assertEqual(out, [[["queued"]], [[1]]])

    def test_controller_lock_prevents_duplicate_writer(self):
        lock_file = self.root / "data" / "lanes" / "a" / "controller.lock"
        with lock_file.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = run_in(self.root, ("import sys,json;sys.path.insert(0,'src');from fleet import pi;"
                                     "print(json.dumps(pi.start('a')))"))
        self.assertTrue(json.loads(out)["alreadyRunning"])

    def test_rotation_is_bounded(self):
        run_in(self.root, (
            "import sys;sys.path.insert(0,'src');from fleet import paths;"
            "p=paths.LOGS/'x.log';p.parent.mkdir(parents=True,exist_ok=True)\n"
            "for i in range(6):\n p.write_text('y'*20); paths.rotate(p,10,3)\n"))
        names = sorted(p.name for p in (self.root / "logs").iterdir())
        self.assertEqual(names, ["x.log.1", "x.log.2", "x.log.3"])


class PortabilityTests(unittest.TestCase):
    FORBIDDEN = [r"/Users/", r"Documents/Codex", r"task-?10", r"\bAdea\b", r"control-plane-lane",
                 r"\.bun/bin", r"\.local/bin"]

    def test_reusable_code_has_no_machine_or_task_specific_names(self):
        files = list((PROJECT / "src").rglob("*.py")) + [PROJECT / "bin" / "fleet",
                                                         PROJECT / "config" / "fleet.example.json"]
        files += [p for p in (PROJECT / "scripts").glob("*") if p.is_file()]
        for path in files:
            text = path.read_text()
            for pattern in self.FORBIDDEN:
                self.assertIsNone(re.search(pattern, text), f"{pattern} in {path}")


class LocalDeploymentTests(unittest.TestCase):
    """Checks the real local configuration when present (skipped on other machines)."""
    CONFIG = PROJECT / "config" / "fleet.local.json"

    def setUp(self):
        if not self.CONFIG.exists():
            self.skipTest("no local configuration")
        self.cfg = json.loads(self.CONFIG.read_text())

    def test_session_identities_are_unique_and_complete(self):
        ids = [s["sessionId"] for s in self.cfg["pi"] + self.cfg["claude"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 20)

    def test_worktrees_are_canonical_and_exist(self):
        root = Path(os.path.expanduser(self.cfg["worktreeRoot"]))
        for slot in self.cfg["pi"] + self.cfg["claude"]:
            cwd = Path(os.path.expanduser(slot["cwd"]))
            self.assertIn(root, cwd.parents, cwd)
            self.assertTrue(cwd.is_dir(), cwd)

    def test_pi_session_files_match_ids(self):
        for slot in self.cfg["pi"]:
            with Path(os.path.expanduser(slot["sessionFile"])).open() as stream:
                header = json.loads(stream.readline())
            self.assertEqual(header["id"], slot["sessionId"])
            self.assertEqual(header["cwd"], slot["sessionCwd"])

    def test_nothing_depends_on_archive(self):
        text = self.CONFIG.read_text()
        self.assertNotIn("/archive/", text)


if __name__ == "__main__":
    unittest.main()
