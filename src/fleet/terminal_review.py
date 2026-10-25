"""One reviewed terminal disposition; no adoption, native writes or quota release."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode

from . import disposable as d

ROOT = Path.home().resolve() / "Developer/.codex/fleet"
RUN = ROOT / "runs/adea-536-idle-callback-followup-20261025-r2"
CONFIG = ROOT / "disposable-opencode.json"
RECEIPT = "terminal-reviewed-no-replay.json"
PIN = {
    "session": "ses_e8c720ce7ffe00SZeLu9ixDNM8",
    "messagesSHA256": "9f01a00d79e86f593c211096635cb976226a16281dacadbf7fd053bc376cffa9",
    "messageCount": 19,
    "latestMessageID": "msg_173b4c4b6001fEqmtEzf3YuIP4",
    "latestAssistantID": "msg_173b4bc7f001oy7hXt8TUavcMD",
    "fileHashes": {
        "continuation-attempt.json": "2ea729489b3332270740b737c44527155788e5fdec2af193d2f4d13f84c4c2fb",
        "continuation-prompt-intent.json": "5f849e9998407d4fc6a958f3ddbc319b275b735bff2ab8a585f12f8da806e721",
        "continuation-prompt-accepted.json": "9274d298e4e0a38727266c23a13fc6fd67d7483daf1b68c0699abdee9624cf1b",
        "continuation-result.json": "938a01ac48db25fbcfbd614647bbad99a6e4b558fedc9bde7f55fa9dada7e71d",
        "implementation-outcome.json": "72b426013426611a300468e1a1e8dc1a17e1ea572ab5fb5f9a82e39eb5dc1f39",
        "superseding-status-20261025.json": "378f62ea901b9455e293327210fb0ecb62074fadc324c4b1ebeda525b7723810"
    },
    "configSHA256": "324b7ef96dec57a56e134a12d5cf9afa8492ba8837c92bc9911f892ae6b16326",
    "quotaSHA256": "f3fbd25870257f50ece0dd50672664395d42f5e27adf5c1494bb18a79b6e747f",
    "runtimeSHA256": "4c6c400c1c9cf532e9f881983160ce1a26797b2a7308f3977e5937db300a4884",
    "sessionSHA256": "8c98bd0c96d6ffc6a7c7c1d61ff83710ef0313c1b71dd46761771bf63645fb31"
}
PIN.update({
    "job": "adea-536-idle-callback-followup-20261025-r2",
    "project": "156ef9474176817ca1d89b5df4ba16e30d9fb703",
    "cwd": str(Path.home().resolve() / "Developer/.worktrees/adea-536-idle-callback-regression-20261025"),
    "branch": "codex/536-idle-callback-proof-20261025",
    "head": "754e40cb24d33451974506503e14952bb754ffe6",
    "merge": "5a2c3be28c97f15d57d8c592fb530f80272ac6b0",
    "reviewOwner": "01a1713e-b8bb-7138-abb2-8f2ad2d6a02b",
})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checked(path, expected):
    if path.is_symlink() or path.resolve() != path:
        raise d.AdmissionRefused("Terminal review input path changed")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise d.AdmissionRefused("Terminal review input bytes changed")
    return body


def preserved_inputs():
    if RUN.is_symlink() or RUN.resolve() != RUN or not RUN.is_dir():
        raise d.AdmissionRefused("Terminal review custody directory changed")
    for name, expected in PIN["fileHashes"].items():
        checked(RUN / name, expected)
    done = json.loads((RUN / "superseding-status-20261025.json").read_text())
    completed = done.get("completedBoundedScope", {})
    if (completed.get("pullRequest") != 1637 or completed.get("state") != "MERGED"
            or completed.get("publishedHead") != PIN["head"]
            or completed.get("mergeCommit") != PIN["merge"]
            or completed.get("rootReviewed") is not True):
        raise d.AdmissionRefused("Separate source completion is not the pinned reviewed merge")


def history(executor, config):
    """Read every page, unlike the run loop's latest-assistant lookup."""
    messages, seen = [], set()
    params = {"limit": "100", "order": "asc"}
    for _ in range(20):
        response = executor._call(config, "GET", executor._session_path(
            PIN["session"], "/message?" + urlencode(params)))
        data = executor._data(response)
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise d.AdmissionRefused("Terminal review history is malformed")
        messages.extend(data)
        cursor = response.get("cursor")
        if not isinstance(cursor, dict):
            raise d.AdmissionRefused("Terminal review cursor is malformed")
        following = cursor.get("next")
        if following is None:
            return messages
        if (not isinstance(following, str) or not following or len(following) > 4096
                or following in seen):
            raise d.AdmissionRefused("Terminal review cursor is invalid or repeated")
        seen.add(following)
        params = {"limit": "100", "cursor": following}
    raise d.AdmissionRefused("Terminal review history exceeded its page bound")


def native_proof(executor, config, listed=None):
    sid = PIN["session"]
    session = executor._data(executor._call(config, "GET", executor._session_path(sid)))
    model = {"id": "mimo-v2.6-flash", "providerID": "opencode-go"}
    if (not isinstance(session, dict) or session.get("id") != sid
            or session.get("title") != "Fleet-disposable:" + PIN["job"]
            or session.get("projectID") != PIN["project"]
            or session.get("location") != {"directory": PIN["cwd"]}
            or session.get("model") not in (model, {**model, "variant": "default"})
            or session.get("parentID") is not None
            or digest(session) != PIN["sessionSHA256"]):
        raise d.AdmissionRefused("Terminal review native identity changed")
    if listed is not None and listed != session:
        raise d.AdmissionRefused("Terminal session changed since listing")
    if (executor._list_sessions(config, parentID=sid) != []
            or executor._session_tree(config, sid) != [sid]
            or sid in executor._active_ids(config)):
        raise d.AdmissionRefused("Terminal review has active or uncertain descendants")
    for suffix in ("/permission", "/inbox"):
        if executor._call(config, "GET", executor._session_path(sid, suffix)) != {"data": []}:
            raise d.AdmissionRefused("Terminal review requires exactly empty inbox and permissions")
    messages = history(executor, config)
    assistant, text = executor._latest_assistant(list(reversed(messages)))
    if (len(messages) != PIN["messageCount"] or digest(messages) != PIN["messagesSHA256"]
            or messages[-1].get("id") != PIN["latestMessageID"]
            or messages[-1].get("type") != "idle"
            or messages[-1].get("outcome") != "succeeded"
            or assistant.get("id") != PIN["latestAssistantID"]
            or assistant.get("finish") != "stop" or text):
        raise d.AdmissionRefused("Terminal review history or terminal no-work outcome changed")
    # Re-read after the paged history: no stale list/terminal proof is accepted.
    if (executor._data(executor._call(config, "GET", executor._session_path(sid))) != session
            or sid in executor._active_ids(config)):
        raise d.AdmissionRefused("Terminal review changed during verification")
    return {"sessionID": sid, "jobID": PIN["job"], "projectID": PIN["project"],
            "directory": PIN["cwd"], "model": session["model"],
            "sessionSHA256": digest(session), "messagesSHA256": digest(messages),
            "messageCount": len(messages), "terminalAssistantID": assistant["id"]}


def review_record(proof):
    return {"version": 1, "disposition": "terminal-no-work-preserved; source-completed-elsewhere",
            "reviewOwner": PIN["reviewOwner"], "proof": proof,
            "preservedReceiptHashes": PIN["fileHashes"],
            "separateCompletion": {"repository": "adea-ai/adea", "pullRequest": 1637,
                                   "head": PIN["head"], "merge": PIN["merge"]},
            "quotaSHA256AtReview": PIN["quotaSHA256"], "retryAllowed": False}


def verify_reviewed_terminal(executor, config, listed=None):
    """Only this immutable, live-revalidated disposition can pass clean start."""
    try:
        if listed is not None and (
                not isinstance(listed, dict) or listed.get("id") != PIN["session"]):
            raise ValueError("different leftover")
        preserved_inputs()
        path = RUN / RECEIPT
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("receipt path")
        record = json.loads(path.read_text())
        if digest(record) != digest(review_record(native_proof(executor, config, listed))):
            raise ValueError("record mismatch")
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise d.AdmissionRefused(
            "Unretired Fleet-disposable session lacks its exact reviewed terminal disposition") from error
    return True



def verify_known_terminal(executor, config):
    # The preserved session cannot disappear behind a changed title/search filter.
    # Other users without this custody record/config retain normal new-only mode.
    known_quota = ROOT / "runs/mimo-verified-paid-fallback-20261025/quota-evidence.json"
    if (config.account in {"opencode", "opencode-go"}
            and (RUN.exists() or RUN.is_symlink()
                 or config.quota_evidence_path == str(known_quota))):
        verify_reviewed_terminal(executor, config)


def refuse_reviewed_job(job):
    # The completed original job must not be replayed under a new manager.
    if job.id == PIN["job"]:
        raise d.AdmissionRefused("Reviewed no-work job was completed elsewhere; never replay it")


def run_review(apply=False, executor=None):
    checked(Path(d.__file__).resolve(), PIN["runtimeSHA256"])
    config = d.Config.parse(json.loads(checked(CONFIG, PIN["configSHA256"])))
    quota_before = checked(Path(config.quota_evidence_path), PIN["quotaSHA256"])
    lock = d._acquire_manager_lock(config)
    try:
        executor = executor or d.NativeExecutor()
        executor.verify(config)  # Local catalog/hold checks only, never an inference.
        preserved_inputs()
        for args, expected in ((["rev-parse", "HEAD"], PIN["head"]),
                               (["branch", "--show-current"], PIN["branch"]),
                               (["status", "--porcelain", "--untracked-files=all"], "")):
            result = subprocess.check_output(["git", "-C", PIN["cwd"], *args], text=True).strip()
            if result != expected:
                raise d.AdmissionRefused("Terminal review source custody changed")
        leftovers = executor._list_sessions(config, search="Fleet-disposable")
        if (len(leftovers) != 1 or leftovers[0].get("id") != PIN["session"]
                or executor._active_ids(config)):
            raise d.AdmissionRefused("Another native owner or uncertain disposable session exists")
        record = review_record(native_proof(executor, config, leftovers[0]))
        # Check all original latches/hold again before the only durable write.
        preserved_inputs()
        if checked(Path(config.quota_evidence_path), PIN["quotaSHA256"]) != quota_before:
            raise d.AdmissionRefused("Quota evidence changed during terminal review")
        path = RUN / RECEIPT
        if path.exists() or path.is_symlink():
            verify_reviewed_terminal(executor, config, leftovers[0])
            return {"state": "already-reviewed; native-session-preserved", "receipt": str(path)}
        if not apply:
            return {"state": "review-ready; no effects", "proof": record["proof"]}
        # An interrupted/partial write fails closed on restart; never replace it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(RUN, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        verify_reviewed_terminal(executor, config, leftovers[0])
        return {"state": "reviewed; native-session-preserved; original-job-not-retriable",
                "receipt": str(path), "proof": record["proof"]}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
