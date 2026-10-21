"""Durable, single-use recovery authorization for a verified failed OpenCode turn."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

from . import opencode, paths
from .store import emit
from .util import atomic

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
# A bare URL, a bare Git/SHA checksum, or a bare filesystem path is a locator, not
# substantive ownership evidence. The locator belongs in evidence_reference.
_LOCATOR_ONLY = re.compile(
    r"^(?:"
    r"[a-z][a-z0-9+.-]*://\S+"                         # URL
    r"|(?:sha(?:1|256):)?[0-9a-f]{40}(?:[0-9a-f]{24})?"  # SHA-1 or SHA-256
    r"|(?:~?/|\.{1,2}/|[A-Za-z]:[\\/]).*"               # filesystem path
    r")$", re.I)
# Fields that make an ownership adoption immutable and comparable on replay.
_ADOPTION_FIELDS = ("id", "owner_task_id", "owner_sid", "failed_sid", "failed_turn",
                    "failed_task_id", "cwd", "owned_worktree", "branch", "head",
                    "worktree_sha256", "decision", "evidence_reference",
                    "ownership_evidence", "note")
# Recovery statuses that still reserve a worker, worktree, cwd, or failed turn.
ACTIVE_STATUSES = ("preparing", "authorized", "claimed")
# Terminal statuses that keep the audit record but release every reservation.
RETIRED = "retired"        # queued successor cancelled before it ever started
RECONCILED = "reconciled"  # claimed successor released after verified native stop
TERMINAL_STATUSES = ("completed", RETIRED, RECONCILED)

_RATE_LIMIT = re.compile(
    r"rate.?limit|too many requests|\b(?:403|429)\b|forbidden|access denied|permission denied|"
    r"quota exceeded|resource exhausted|insufficient credits|billing|"
    r"account.{0,20}(?:disabled|restricted)",
    re.I,
)


def _full_process_identity(value):
    """A retained native child identity must bind PID reuse, group and executable."""
    return (isinstance(value, dict)
            and all(isinstance(value.get(key), int) and not isinstance(value[key], bool)
                    and value[key] > 0 for key in ("pid", "pgid"))
            and isinstance(value.get("start"), list) and bool(value["start"])
            and all(isinstance(part, int) and not isinstance(part, bool)
                    for part in value["start"])
            and isinstance(value.get("executable"), str) and bool(value["executable"]))


def worktree_snapshot(cwd):
    """Fingerprint branch/head, Git status/diff, and non-ignored untracked files.

    This is not a backup or a complete partial-state capture: ignored contents,
    nested repository/submodule contents and intermediate index blobs are omitted.
    """
    root = Path(cwd).expanduser().resolve(strict=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              stdin=subprocess.DEVNULL, capture_output=True).stdout

    top_level = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
    if top_level != root:
        raise ValueError("Recovery path must be the root of its owned Git worktree")
    branch = git("branch", "--show-current").decode().strip()
    head = git("rev-parse", "HEAD").decode().strip()
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    diff = git("diff", "--binary", "HEAD", "--")
    digest = hashlib.sha256()
    for label, value in ((b"branch", branch.encode()), (b"head", head.encode()),
                         (b"status", status), (b"diff", diff)):
        digest.update(label + b"\0" + value + b"\0")
    for entry in status.split(b"\0"):
        if entry.startswith(b"?? "):
            raw_path = entry[3:]
            path = root / os.fsdecode(raw_path)
            digest.update(b"untracked\0" + raw_path + b"\0")
            if path.is_symlink():
                digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                digest.update(b"other\0")
            digest.update(b"\0")
    if not branch:
        raise ValueError("Recovery requires a named Git branch, not detached HEAD")
    return {"branch": branch, "head": head, "worktreeSha256": digest.hexdigest()}


def verify_failed_turn(session_id, failed_task_id, turn, cwd, model):
    """Verify the durable pointer, error receipt, cleanup and any recorded PID identity."""
    pointer = opencode.session_dir(session_id) / "current.json"
    if not pointer.is_file():
        raise ValueError("No managed OpenCode turn exists for this worker")
    launch = json.loads(pointer.read_text())
    if (launch.get("turn") != turn or launch.get("taskId") != failed_task_id
            or launch.get("sessionId") != session_id or launch.get("cwd") != cwd
            or launch.get("model") != model):
        raise ValueError("Current OpenCode turn identity differs from the requested failed task")
    state_path = opencode.turn_dir(session_id, turn) / "state.json"
    result_path = opencode.turn_dir(session_id, turn) / "result.json"
    if not state_path.is_file() or not result_path.is_file():
        raise ValueError("OpenCode turn state/result is incomplete; recovery is blocked")
    state = json.loads(state_path.read_text())
    result = json.loads(result_path.read_text())
    identity = (state.get("turn", turn) == turn
                and state.get("taskId") == failed_task_id
                and state.get("sessionId") == session_id and state.get("cwd") == cwd
                and state.get("model") == model and state.get("state") == "error"
                and state.get("cleanup") == "confirmed"
                and isinstance(state.get("finishedAt"), (int, float))
                and result.get("turn") == turn
                and result.get("taskId") == failed_task_id
                and result.get("sessionId") == session_id and result.get("cwd") == cwd
                and result.get("expectedModel") == model
                and result.get("state") == "error" and result.get("cleanup") == "confirmed"
                and result.get("finishedAt") == state.get("finishedAt"))
    if not identity:
        raise ValueError("OpenCode error result identity or cleanup is not verified")
    failures = result.get("toolFailures") or []
    if not isinstance(failures, list):
        raise ValueError("OpenCode error receipt has an invalid tool-failure list")
    failure_text = " ".join([str(result.get("error") or ""),
                             *(str(item.get("error") or "") for item in failures
                               if isinstance(item, dict))])
    if _RATE_LIMIT.search(failure_text):
        raise ValueError("Rate-limit/quota failures cannot be recovered by this operation")
    process_identity = result.get("managedProcessIdentity")
    if not _full_process_identity(process_identity):
        raise ValueError("OpenCode error receipt has no complete native process identity")
    if state.get("processIdentity") is not None \
            and state["processIdentity"] != process_identity:
        raise ValueError("OpenCode error state and result process identities differ")
    if (opencode.identity_matches(process_identity)
            or opencode._group_exists(process_identity["pgid"])):
        raise ValueError("OpenCode native process group is still live")
    return launch, state, result


def _queue_lock():
    Path(paths.QUEUE_LOCK).parent.mkdir(parents=True, exist_ok=True)
    handle = Path(paths.QUEUE_LOCK).open("a")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _queue_has_task(task_id):
    if not Path(paths.QUEUE).exists():
        return False
    queue = json.loads(Path(paths.QUEUE).read_text())
    if queue.get("author") != "root" or not isinstance(queue.get("tasks"), list):
        raise ValueError("Root task queue is invalid; recovery is blocked")
    return any(task.get("id") == task_id for task in queue["tasks"])


def _same_record(row, values):
    fields = ("id", "failed_sid", "failed_turn", "failed_task_id", "owner_sid",
              "owner_task_id", "ownership_task_id", "cwd", "owned_worktree",
              "branch", "head", "worktree_sha256", "adoption_id",
              "direct_turn_receipt", "direct_turn_sha256", "note")
    return all(row[key] == values[key] for key in fields)


def adoption_record(db, adoption_id):
    """The immutable ownership-adoption row, if this store records one."""
    try:
        row = db.execute("SELECT * FROM recovery_adoptions WHERE id=?",
                         (adoption_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    names = [item[0] for item in
             db.execute("SELECT * FROM recovery_adoptions LIMIT 0").description]
    return dict(zip(names, row))


def _prove_native_identity(slot, session_id, cwd):
    """Prove that the native session behind this worker is still the same one.

    The managed turn pointer, the turn receipts and the task ledger prove that
    a turn happened for this worker. They do not prove that the native session
    itself is still the session the roster names: a native identity that has
    gone stale (the session row is gone, or it points at another directory) is
    unreadable, and ownership evidence for it would be recorded against a
    session the Fleet cannot prove.

    The direct-turn receipt already proves the native session before it is
    accepted as an ownership source. The adoption is the other ownership source
    that substitutes for durable history, so it proves the same thing, and it
    proves it again when the adoption is consumed: a session that went stale
    between the adoption and the authorization reserves nothing.
    """
    try:
        return opencode.verify_native_session(session_id, cwd, slot.get("databasePath"))
    except (ValueError, OSError) as error:
        raise ValueError("The native session identity of this worker could not be verified: "
                         + str(error)) from error


def _verify_pointer_identity(session_id, failed_task_id, turn, cwd, model):
    pointer = opencode.session_dir(session_id) / "current.json"
    if not pointer.is_file():
        raise ValueError("No managed OpenCode turn exists for this worker")
    launch = json.loads(pointer.read_text())
    if (launch.get("turn") != turn or launch.get("taskId") != failed_task_id
            or launch.get("sessionId") != session_id or launch.get("cwd") != cwd
            or launch.get("model") != model):
        raise ValueError("Adoption failed turn identity differs from the current managed turn")


def _evidence_reference(value):
    """Normalize the locator for the external ownership record.

    A reference identifies where the operator should look; it is deliberately not
    accepted as proof by itself. The operator must inspect and verify that source.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Adoption requires an external evidence reference locator")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise ValueError("Adoption evidence reference must be a single locator")
    return text


def _substantive_ownership_evidence(value, reference):
    """Require an excerpt/statement, not merely a URL, checksum, or path locator."""
    if not isinstance(value, str):
        raise ValueError(
            "Adoption requires a substantive external ownership evidence excerpt or statement")
    text = value.strip()
    if len(text) < 24:
        raise ValueError(
            "Adoption requires a substantive external ownership evidence excerpt or statement")
    if _LOCATOR_ONLY.fullmatch(text):
        raise ValueError(
            "Ownership evidence cannot be only a URL, checksum, or path; put the locator in "
            "evidence_reference and quote the ownership statement in ownership_evidence")
    if text == reference:
        raise ValueError("Ownership evidence must be distinct from the evidence reference")
    if not re.search(r"[A-Za-z]", text) or not re.search(r"\s", text):
        raise ValueError(
            "Adoption requires a substantive external ownership evidence excerpt or statement")
    return text


def adopt_ownership(db, *, adoption_id, owner_task_id, session_id, failed_task_id, turn,
                    owned_worktree, branch, head, worktree_sha256, decision,
                    evidence_reference, ownership_evidence, note):
    """Append immutable ownership evidence for a recovery whose owner task predates the
    exact worktree/branch message convention.

    The original W4 task may legitimately have instructed the worker to create its branch
    and checkout, so that path/branch never appears in historical text. This records the
    verified structured ownership facts instead of rewriting the owner task, event, or
    turn history.

    ``evidence_reference`` is only a locator for the operator to inspect. ``ownership_evidence``
    is the substantive excerpt/statement; a URL or checksum alone never qualifies. The operator
    remains responsible for inspecting and verifying the referenced source.

    Adoption is evidence only. It never queues or dispatches a task, starts a model turn,
    changes/rehomes a session, or authorizes a successor by itself. Replaying an identical
    request is idempotent; reusing the same adoption ID with any altered field is rejected.
    """
    identities = (adoption_id, owner_task_id, session_id, failed_task_id, turn, note)
    if any(not isinstance(value, str) or not value.strip() for value in identities):
        raise ValueError("Adoption identifiers and note must be non-empty strings")
    if not isinstance(decision, str) or not decision.strip():
        raise ValueError("Adoption requires an explicit operator decision")
    reference = _evidence_reference(evidence_reference)
    ownership_evidence = _substantive_ownership_evidence(ownership_evidence, reference)
    if not _SHA256.fullmatch(worktree_sha256 or ""):
        raise ValueError("worktree_sha256 must be a lowercase SHA-256 digest")
    if not _HEAD.fullmatch(head or ""):
        raise ValueError("Adoption head must be the full commit SHA, not an abbreviation")
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("Adoption branch must be a non-empty string")
    branch = branch.strip()

    all_slots = paths.opencode_slots()
    slots = [s for s in all_slots if s.get("sessionId") == session_id]
    if len(slots) != 1:
        raise ValueError("Adoption target must be one existing OpenCode slot")
    # The persisted turn is proved against the model configured for this
    # session, not against a fleet-wide constant.
    configured_model = opencode.approved_free_model(slots[0])
    cwd = _resolve(slots[0].get("cwd") or "")
    if not cwd:
        raise ValueError("Adoption target has no configured native cwd")
    configured = _configured_cwds(all_slots)
    if configured.get(session_id) != cwd:
        raise ValueError("Adoption native CWD does not match the configured session roster")
    worktree = str(Path(owned_worktree).expanduser().resolve(strict=True))
    values = {"id": adoption_id, "owner_task_id": owner_task_id,
              "owner_sid": session_id, "failed_sid": session_id,
              "failed_turn": turn, "failed_task_id": failed_task_id, "cwd": cwd,
              "owned_worktree": worktree, "branch": branch, "head": head,
              "worktree_sha256": worktree_sha256, "decision": decision.strip(),
              "evidence_reference": reference, "ownership_evidence": ownership_evidence,
              "note": note}
    # Immutability is checked before live state so a reused ID with altered content
    # is always rejected as such, never masked by a later checkout check.
    existing = adoption_record(db, adoption_id)
    if existing:
        if any(existing[field] != values[field] for field in _ADOPTION_FIELDS):
            raise ValueError("Adoption ID was reused with different content")
        return {"adoptionId": adoption_id, "alreadyRecorded": True}

    # The inviter must be an existing finished owner task on the same session and CWD.
    _task_message(db, owner_task_id, session_id, cwd)
    _validate_recovery_mapping(db, {"ownership_task_id": owner_task_id,
                                    "owner_sid": session_id, "cwd": cwd}, configured)
    failed = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?",
                        (failed_task_id,)).fetchone()
    if not failed or failed[0] != session_id or _resolve(failed[1]) != cwd:
        raise ValueError("Failed task ownership/cwd does not match the configured worker")
    if failed[2] != "finished":
        raise ValueError("Failed task must remain terminal before adoption")
    _verify_pointer_identity(session_id, failed_task_id, turn, cwd, configured_model)
    # The adoption stands in for durable owner history, so it proves the native
    # session itself before the evidence is recorded. A stale native identity
    # records nothing.
    _prove_native_identity(slots[0], session_id, cwd)

    snapshot = worktree_snapshot(worktree)
    if (snapshot["branch"] != branch or snapshot["head"] != head
            or snapshot["worktreeSha256"] != worktree_sha256):
        raise ValueError("Adoption branch/head/worktree fingerprint does not match the checkout")
    conflict = _active_task_for_worktree(db, worktree, configured,
                                         exclude_task_id=failed_task_id)
    if conflict:
        raise ValueError(f"Another active task owns the explicit worktree: {conflict}")
    queued = _queued_task_for_worktree(db, worktree, configured,
                                       exclude_task_id=failed_task_id)
    if queued:
        raise ValueError(f"Another queued task owns the explicit worktree: {queued}")
    other = db.execute("SELECT id FROM recoveries WHERE owned_worktree=? AND status IN "
                       "('preparing','authorized','claimed') LIMIT 1",
                       (worktree,)).fetchone()
    if other:
        raise ValueError(f"Another recovery owns the explicit worktree: {other[0]}")

    now = time.time()
    try:
        db.execute("""INSERT INTO recovery_adoptions
            (id,owner_task_id,owner_sid,failed_sid,failed_turn,failed_task_id,cwd,
             owned_worktree,branch,head,worktree_sha256,decision,evidence_reference,
             ownership_evidence,note,created)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (adoption_id, owner_task_id, session_id, session_id, turn, failed_task_id,
                    cwd, worktree, branch, head, worktree_sha256, decision.strip(),
                    reference, ownership_evidence, note, now))
        db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                   ("recovery_ownership_adopted", json.dumps(values), now))
        db.commit()
    except sqlite3.IntegrityError:
        # A concurrent identical request may have won the primary-key race.
        db.rollback()
        existing = adoption_record(db, adoption_id)
        if existing and not any(existing[field] != values[field]
                                for field in _ADOPTION_FIELDS):
            return {"adoptionId": adoption_id, "alreadyRecorded": True}
        raise ValueError("Adoption ID was reused with different content")
    return {"adoptionId": adoption_id, "alreadyRecorded": False}


def _validate_adoption(db, record, adoption_id):
    adoption = adoption_record(db, adoption_id)
    if not adoption:
        raise ValueError("Recovery adoption record is missing")
    # Adoption rows name the inviter as ``owner_task_id``; recovery rows keep that
    # task in ``ownership_task_id`` and use ``owner_task_id`` for the successor.
    for adoption_field, record_field in (
            ("owner_task_id", "ownership_task_id"), ("owner_sid", "owner_sid"),
            ("failed_sid", "failed_sid"), ("failed_turn", "failed_turn"),
            ("failed_task_id", "failed_task_id"), ("cwd", "cwd"),
            ("owned_worktree", "owned_worktree"), ("branch", "branch"),
            ("head", "head"), ("worktree_sha256", "worktree_sha256")):
        if adoption[adoption_field] != record[record_field]:
            raise ValueError(f"Recovery adoption no longer matches the recorded {record_field}")
    # Revalidate the evidence split on every consumption. This keeps legacy rows that
    # lack a reference, or rows whose "evidence" is only a locator/checksum, fail closed.
    if not isinstance(adoption.get("decision"), str) or not adoption["decision"].strip():
        raise ValueError("Recovery adoption record has no operator decision")
    reference = _evidence_reference(adoption.get("evidence_reference"))
    _substantive_ownership_evidence(adoption.get("ownership_evidence"), reference)
    return adoption


DIRECT_TURN_RECEIPT_VERSION = "pi-direct-turn-ownership/v2"
_DIRECT_TURN_RECEIPT_FIELDS = ("schemaVersion", "originalSession", "ownedWorktree",
                               "branch", "initialHead", "startedAt",
                               "originalUserMessageId", "completionIdleMessageId", "note")


def _direct_turn_receipt(path):
    """Read and validate one direct-turn ownership receipt.

    A genuine native turn that predates the Fleet task ledger has no owner task
    row, so the operator records that turn's ownership in a durable receipt
    inside the fleet's own recovery directory. The receipt is the ownership
    source; it proves nothing by itself and is checked against the native
    session and the current checkout before any successor is authorized.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Direct-turn ownership receipt path is required")
    receipt_path = Path(path).expanduser()
    if not receipt_path.is_absolute():
        raise ValueError("Direct-turn ownership receipt path must be absolute")
    root = Path(paths.RECOVERY).expanduser().resolve()
    resolved = receipt_path.resolve()
    if root not in resolved.parents:
        raise ValueError("Direct-turn ownership receipt must live in the fleet recovery directory")
    if not resolved.is_file():
        raise ValueError("Direct-turn ownership receipt is missing")
    try:
        document = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Direct-turn ownership receipt is unreadable") from error
    if not isinstance(document, dict):
        raise ValueError("Direct-turn ownership receipt is not an object")
    if set(document) != set(_DIRECT_TURN_RECEIPT_FIELDS):
        raise ValueError("Direct-turn ownership receipt has unexpected fields")
    if document["schemaVersion"] != DIRECT_TURN_RECEIPT_VERSION:
        raise ValueError("Direct-turn ownership receipt version is not supported")
    if not isinstance(document["originalSession"], str) \
            or not opencode._SESSION_ID.fullmatch(document["originalSession"]):
        raise ValueError("Direct-turn ownership receipt names an invalid original session")
    if not isinstance(document["ownedWorktree"], str) or not document["ownedWorktree"].strip():
        raise ValueError("Direct-turn ownership receipt has no owned worktree")
    if not isinstance(document["branch"], str) or not document["branch"].strip():
        raise ValueError("Direct-turn ownership receipt has no branch")
    if not _HEAD.fullmatch(document["initialHead"] or ""):
        raise ValueError("Direct-turn ownership receipt initial head must be a full commit SHA")
    started = document["startedAt"]
    if isinstance(started, bool) or not isinstance(started, (int, float)) or started <= 0:
        raise ValueError("Direct-turn ownership receipt start time is invalid")
    for field in ("originalUserMessageId", "completionIdleMessageId"):
        if not isinstance(document[field], str) or not re.fullmatch(r"msg_[A-Za-z0-9]+", document[field]):
            raise ValueError("Direct-turn ownership receipt has an invalid native message ID")
    _substantive_ownership_evidence(document["note"], document["ownedWorktree"])
    return resolved, document, hashlib.sha256(resolved.read_bytes()).hexdigest()


def _head_descends_from(worktree, initial_head):
    """Does the checkout descend from the recorded initial head?

    A head that is not a commit of this checkout cannot be an ancestor of it,
    and the check fails closed in that case too.
    """
    result = subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor",
                             initial_head, "HEAD"], check=False, capture_output=True,
                            stdin=subprocess.DEVNULL)
    if result.returncode == 0:
        return True
    if result.returncode in (1, 128):
        return False
    raise ValueError("Owned worktree ancestry could not be verified")


def _direct_turn_native_evidence(receipt, session_id, worktree, model, database_path,
                                 failed_turn, failed_task_id):
    """Prove the recorded session ran a completed direct turn in the owned worktree.

    Bind one original user message to its own successful idle marker and final
    completed assistant, before the failed managed turn. A later turn's mutable
    session-wide idle outcome cannot prove this earlier turn's completion.
    """
    failed_path = opencode.turn_dir(session_id, failed_turn) / "state.json"
    try:
        failed = json.loads(failed_path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError("Failed managed turn start is unavailable") from error
    if (not isinstance(failed, dict) or failed.get("sessionId") != session_id
            or failed.get("taskId") != failed_task_id
            or not isinstance(failed.get("startedAt"), (int, float))):
        raise ValueError("Failed managed turn start does not match the direct-turn receipt")
    failed_started_ms = int(failed["startedAt"] * 1000)
    db_path = Path(database_path).expanduser().resolve()
    if not db_path.is_file():
        raise ValueError("Direct-turn native session database is unavailable")
    uri = "file:" + opencode.quote(str(db_path), safe="/") + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT directory,model,idle_outcome,time_created,time_updated "
                "FROM session_v2 WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise ValueError("Direct-turn receipt names a session with no native session row")
            directory, native_model, idle_outcome, created, updated = row
            messages = connection.execute(
                "SELECT id,type,seq,time_created,data FROM session_message "
                "WHERE session_id=? AND id IN (?,?)",
                (session_id, receipt["originalUserMessageId"],
                 receipt["completionIdleMessageId"])).fetchall()
            by_id = {item[0]: item for item in messages}
            user = by_id.get(receipt["originalUserMessageId"])
            idle = by_id.get(receipt["completionIdleMessageId"])
            if (not user or not idle or user[1] != "user" or idle[1] != "idle"
                    or not isinstance(user[2], int) or not isinstance(idle[2], int)
                    or user[2] >= idle[2]
                    or abs(user[3] - int(receipt["startedAt"] * 1000)) > 10_000
                    or not user[3] < idle[3] < failed_started_ms):
                raise ValueError("Direct-turn receipt does not identify an earlier native turn")
            boundaries = connection.execute(
                "SELECT type FROM session_message WHERE session_id=? AND seq>=? AND seq<=? "
                "AND type IN ('user','idle') ORDER BY seq", (session_id, user[2], idle[2])
            ).fetchall()
            final = connection.execute(
                "SELECT id,time_created,data FROM session_message WHERE session_id=? "
                "AND type='assistant' AND seq>? AND seq<? ORDER BY seq DESC LIMIT 1",
                (session_id, user[2], idle[2])).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError("Direct-turn native session database could not be read safely") from error
    native_directory = str(Path(str(directory)).expanduser().resolve())
    if native_directory != worktree:
        raise ValueError("Direct-turn receipt worktree differs from the native session directory")
    native_provider, native_name = opencode._native_model_parts(native_model)
    configured_provider, _, configured_name = str(model).partition("/")
    if not native_name or native_name != configured_name \
            or (native_provider and native_provider != configured_provider):
        raise ValueError("Direct-turn receipt session model differs from the configured model")
    if boundaries != [("user",), ("idle",)] or not final:
        raise ValueError("Direct-turn receipt spans another turn or has no assistant output")
    try:
        idle_data = json.loads(idle[4])
        final_data = json.loads(final[2])
    except (TypeError, ValueError) as error:
        raise ValueError("Direct-turn native completion metadata is unreadable") from error
    if not isinstance(idle_data, dict) or not isinstance(final_data, dict):
        raise ValueError("Direct-turn native completion metadata is invalid")
    final_model = final_data.get("model")
    final_time = final_data.get("time")
    if not isinstance(final_model, dict) or not isinstance(final_time, dict):
        raise ValueError("Direct-turn final assistant identity is invalid")
    completed_at = final_time.get("completed")
    if (idle_data.get("outcome") != "succeeded" or final_data.get("finish") != "stop"
            or not isinstance(completed_at, (int, float))
            or not user[3] < final[1] <= completed_at <= idle[3]
            or final_model.get("id") != configured_name
            or final_model.get("providerID") != configured_provider):
        raise ValueError("Native direct turn did not complete successfully")
    return {"nativeSession": session_id, "nativeDirectory": native_directory,
            "nativeModel": native_model, "nativeIdleOutcome": idle_data["outcome"],
            "nativeUserMessage": user[0], "nativeIdleMessage": idle[0],
            "nativeFinalAssistant": final[0], "nativeSessionCreated": created,
            "nativeSessionUpdated": updated}


def _validate_direct_turn(record, slots=None):
    """Re-prove a direct-turn ownership receipt before a successor dispatches."""
    sid = record["owner_sid"]
    roster = slots
    if roster is None:
        roster = paths.opencode_slots()
    if isinstance(roster, dict):
        roster = list(roster.values())
    slot = next((item for item in roster or () if item.get("sessionId") == sid), None)
    if slot is None:
        raise ValueError("Direct-turn recovery session is not a configured OpenCode worker")
    configured_model = opencode.approved_free_model(slot)
    verified = opencode.verify_slot(slot, expected_model=configured_model)
    receipt_path, receipt, digest = _direct_turn_receipt(record["direct_turn_receipt"])
    if digest != record["direct_turn_sha256"]:
        raise ValueError("Direct-turn ownership receipt changed after recovery authorization")
    if receipt["originalSession"] != sid:
        raise ValueError("Direct-turn receipt names a different original session")
    if _resolve(receipt["ownedWorktree"]) != record["owned_worktree"]:
        raise ValueError("Direct-turn receipt worktree differs from the recorded owned worktree")
    if receipt["branch"].strip() != record["branch"].strip():
        raise ValueError("Direct-turn receipt branch differs from the recorded branch")
    if not _head_descends_from(record["owned_worktree"], receipt["initialHead"]):
        raise ValueError("Direct-turn receipt initial head is not an ancestor of the current head")
    _direct_turn_native_evidence(receipt, sid, record["owned_worktree"], configured_model,
                                 verified["databasePath"], record["failed_turn"],
                                 record["failed_task_id"])
    return receipt


def _task_message(db, task_id, session_id, cwd):
    row = db.execute("SELECT sid,cwd,state,body FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row or row[0] != session_id or str(Path(row[1]).resolve()) != cwd:
        raise ValueError("Ownership task must belong to the same native worker and CWD")
    if row[2] != "finished":
        raise ValueError("Ownership task must be terminal before recovery")
    try:
        message = json.loads(row[3]).get("message", "")
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Ownership task record is invalid") from error
    if not isinstance(message, str):
        raise ValueError("Ownership task record has no message")
    return message


def _resolve(value):
    """Resolve a path for identity comparison; empty values stay empty."""
    return str(Path(value).expanduser().resolve()) if value else ""


def _configured_cwds(slots=None):
    """Map every configured session to its structured, resolved CWD.

    Accepts a slot list or a ``{sessionId: slot}`` mapping, as produced by
    ``paths.all_slots()`` and the manager roster. Falls back to the OpenCode
    roster so recovery callers that already read it stay consistent.
    """
    if slots is None:
        try:
            slots = paths.opencode_slots()
        except (FileNotFoundError, ValueError):
            slots = []
    if isinstance(slots, dict):
        slots = slots.values()
    result = {}
    for slot in slots or []:
        sid, cwd = slot.get("sessionId"), slot.get("cwd")
        if sid and cwd:
            result[sid] = _resolve(cwd)
    return result


def _claims_worktree(tree, *, cwd=None, sid=None, message="", configured=None):
    """Does this task claim ``tree`` through structured or supplemental evidence?

    Structured ownership wins: the task's durable ``tasks.cwd`` column and its
    target session's configured CWD. A path substring in the free-text message is
    kept only as conservative supplemental evidence, never as the sole basis.
    """
    if cwd and _resolve(cwd) == tree:
        return True
    if configured and sid and configured.get(sid) == tree:
        return True
    return bool(tree and message and tree in message)


def _active_task_for_worktree(db, owned_worktree, configured, exclude_task_id=None):
    tree = _resolve(owned_worktree)
    rows = db.execute("SELECT id,sid,cwd,body FROM tasks WHERE state IN "
                      "('queued','dispatching','sent','running','uncertain')").fetchall()
    for task_id, sid, cwd, body in rows:
        if task_id == exclude_task_id:
            continue
        try:
            message = json.loads(body).get("message", "")
        except (TypeError, json.JSONDecodeError):
            message = ""
        if not isinstance(message, str):
            message = ""
        if _claims_worktree(tree, cwd=cwd, sid=sid, message=message, configured=configured):
            return task_id
    return None


def _queued_task_for_worktree(db, owned_worktree, configured, exclude_task_id=None):
    if not Path(paths.QUEUE).exists():
        return None
    queue = json.loads(Path(paths.QUEUE).read_text())
    if queue.get("author") != "root" or not isinstance(queue.get("tasks"), list):
        raise ValueError("Root task queue is invalid; recovery is blocked")
    tree = _resolve(owned_worktree)
    for task in queue["tasks"]:
        task_id = task.get("id")
        if task_id == exclude_task_id:
            continue
        if not _claims_worktree(tree, sid=task.get("sessionId"),
                                message=str(task.get("message") or ""),
                                configured=configured):
            continue
        row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row[0] in {"queued", "dispatching", "sent", "running", "uncertain"}:
            return task_id or "unknown"
    return None


def _validate_recovery_mapping(db, record, configured):
    """Bind the recovery's native CWD/worker to structured task and roster fields."""
    row = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?",
                     (record["ownership_task_id"],)).fetchone()
    if not row or row[0] != record["owner_sid"] or _resolve(row[1]) != _resolve(record["cwd"]):
        raise ValueError("Recovery native CWD/worker does not match the ownership task")
    if row[2] != "finished":
        raise ValueError("Ownership task must be terminal before recovery")
    expected = configured.get(record["owner_sid"])
    if expected and expected != _resolve(record["cwd"]):
        raise ValueError("Recovery native CWD does not match the configured session cwd")


def validate_owned_worktree(db, record, slots=None):
    """Recheck task provenance and exclusivity before dispatching a successor."""
    worktree = record["owned_worktree"]
    configured = _configured_cwds(slots)
    adoption = None
    if record.get("direct_turn_receipt"):
        # The immutable direct-turn receipt, not free text and not a task row, is
        # the provenance for this checkout. It is re-proved against the native
        # session and the current checkout before the successor may dispatch.
        _validate_direct_turn(record, slots)
    else:
        _validate_recovery_mapping(db, record, configured)
        message = _task_message(db, record["ownership_task_id"], record["owner_sid"], record["cwd"])
        adoption_id = record.get("adoption_id") or ""
        if adoption_id:
            # The immutable adoption, not free text, is the provenance for this checkout.
            adoption = _validate_adoption(db, record, adoption_id)
        elif worktree not in message or record["branch"] not in message:
            raise ValueError("Owned worktree/branch no longer match the owner task record")
    snapshot = worktree_snapshot(worktree)
    if (snapshot["branch"] != record["branch"] or snapshot["head"] != record["head"]
            or snapshot["worktreeSha256"] != record["worktree_sha256"]):
        raise ValueError("Owned worktree changed after recovery authorization")
    if adoption and (snapshot["branch"] != adoption["branch"]
                     or snapshot["head"] != adoption["head"]
                     or snapshot["worktreeSha256"] != adoption["worktree_sha256"]):
        raise ValueError("Owned worktree changed after ownership adoption")
    conflict = _active_task_for_worktree(db, worktree, configured, record["owner_task_id"])
    if conflict:
        raise ValueError(f"Another active task owns the explicit worktree: {conflict}")
    conflict = _queued_task_for_worktree(db, worktree, configured, record["owner_task_id"])
    if conflict and conflict != record["owner_task_id"]:
        raise ValueError(f"Another queued task owns the explicit worktree: {conflict}")
    other = db.execute("SELECT id FROM recoveries WHERE owned_worktree=? AND id<>? "
                       "AND status IN ('preparing','authorized','claimed') LIMIT 1",
                       (worktree, record["id"])).fetchone()
    if other:
        raise ValueError(f"Another recovery owns the explicit worktree: {other[0]}")


def task_recovery_conflict(db, task, sid, slots=None):
    """Return a recovery reserving this session or a worktree the task claims.

    A task claims a worktree through its structured CWD and its target session's
    configured CWD, not only through a path string in its message.
    """
    valid = recovery_for_task(db, task.get("recoveryId"), task.get("id"), sid,
                              task.get("previousTaskId"),
                              statuses=("authorized", "claimed", "completed")) \
        if task.get("recoveryId") else None
    if valid:
        return None
    configured = _configured_cwds(slots)
    task_cwd = task.get("cwd")
    message = str(task.get("message") or "")
    rows = db.execute("SELECT id,owner_sid,owned_worktree,cwd FROM recoveries "
                      "WHERE status IN ('preparing','authorized','claimed')").fetchall()
    for recovery_id, owner_sid, worktree, native_cwd in rows:
        if sid == owner_sid:
            return recovery_id
        for reserved in (worktree, native_cwd):
            if reserved and _claims_worktree(_resolve(reserved), cwd=task_cwd, sid=sid,
                                             message=message, configured=configured):
                return recovery_id
    return None


def authorize_opencode(db, *, session_id, failed_task_id, turn, recovery_id,
                       successor_task_id, ownership_task_id, owned_worktree,
                       branch, head, worktree_sha256, note, adoption_id="",
                       direct_turn_receipt=""):
    """Record a verified error-turn handoff without editing task or turn history.

    The successor remains root-queued work. This operation only creates its
    single-use authorization; it never starts a model turn or changes a hold.

    Provenance is the owner task's free-text message unless ``adoption_id`` names an
    immutable ownership adoption, which substitutes structured evidence for history
    that predates the exact worktree/branch convention. A genuine native turn that
    predates the task ledger has no owner task row at all; ``direct_turn_receipt``
    names the durable receipt that records that turn's ownership, and the receipt is
    proved against the native session and the current checkout instead.
    """
    direct_turn = isinstance(direct_turn_receipt, str) and bool(direct_turn_receipt.strip())
    if direct_turn and (ownership_task_id.strip() or adoption_id.strip()):
        raise ValueError("Choose exactly one ownership source: owner task, adoption or "
                         "direct-turn receipt")
    fields = (session_id, failed_task_id, turn, recovery_id, successor_task_id,
              owned_worktree, branch, head, note)
    if not direct_turn:
        fields += (ownership_task_id,)
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise ValueError("Recovery identifiers, branch, and note must be non-empty strings")
    if not isinstance(adoption_id, str):
        raise ValueError("adoption_id must be a string")
    if not _SHA256.fullmatch(worktree_sha256 or ""):
        raise ValueError("worktree_sha256 must be a lowercase SHA-256 digest")
    all_slots = paths.opencode_slots()
    slots = [s for s in all_slots if s.get("sessionId") == session_id]
    if len(slots) != 1:
        raise ValueError("Recovery target must be one existing OpenCode slot")
    # The failed turn is proved against the model configured for this session
    # and its persisted launch, turn and error receipts, not against a
    # fleet-wide constant.
    configured_model = opencode.approved_free_model(slots[0])
    slot = opencode.verify_slot(slots[0], expected_model=configured_model)
    if not slot.get("dispatchEnabled", False):
        raise ValueError("OpenCode dispatch is disabled; recovery does not change that hold")

    # Structured ownership: bind the native CWD/worker to the roster and the
    # ownership task's durable row before trusting any free-text message.
    configured = _configured_cwds(all_slots)
    cwd = _resolve(slot["cwd"])
    if configured.get(session_id) != cwd:
        raise ValueError("Recovery native CWD does not match the configured session roster")
    adoption = None
    direct_turn_path = ""
    direct_turn_digest = ""
    direct_turn_evidence = {}
    if direct_turn:
        # A genuine direct turn has no owner task row. Its durable receipt is the
        # ownership source, proved against the native completed turn and the
        # current checkout before anything is recorded.
        worktree = str(Path(owned_worktree).expanduser().resolve(strict=True))
        receipt_path, receipt, receipt_digest = _direct_turn_receipt(direct_turn_receipt)
        if receipt["originalSession"] != session_id:
            raise ValueError("Direct-turn receipt names a different original session")
        if _resolve(receipt["ownedWorktree"]) != worktree:
            raise ValueError("Direct-turn receipt worktree differs from the recorded "
                             "owned worktree")
        if receipt["branch"].strip() != branch.strip():
            raise ValueError("Direct-turn receipt branch differs from the recorded branch")
        if not _head_descends_from(worktree, receipt["initialHead"]):
            raise ValueError("Direct-turn receipt initial head is not an ancestor of the "
                             "current head")
        direct_turn_evidence = _direct_turn_native_evidence(
            receipt, session_id, worktree, configured_model, slot["databasePath"],
            turn, failed_task_id)
        direct_turn_path = str(receipt_path)
        direct_turn_digest = receipt_digest
        ownership_task_id = ""
    else:
        _validate_recovery_mapping(db, {"ownership_task_id": ownership_task_id,
                                        "owner_sid": session_id, "cwd": cwd}, configured)
        worktree = str(Path(owned_worktree).expanduser().resolve(strict=True))
        ownership_message = _task_message(db, ownership_task_id, session_id, cwd)
        if adoption_id:
            adoption = _validate_adoption(
                db, {"ownership_task_id": ownership_task_id, "owner_sid": session_id,
                     "failed_sid": session_id, "failed_turn": turn,
                     "failed_task_id": failed_task_id, "cwd": cwd,
                     "owned_worktree": worktree, "branch": branch, "head": head,
                     "worktree_sha256": worktree_sha256}, adoption_id)
            # The adoption is the ownership source here, so the native session
            # behind it is proved again: a session that went stale after the
            # adoption reserves no worker, no worktree and no successor.
            _prove_native_identity(slot, session_id, cwd)
        elif worktree not in ownership_message or branch not in ownership_message:
            raise ValueError("Owned worktree and branch are not recorded by this worker's owner task")
    task = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?", (failed_task_id,)).fetchone()
    if not task or task[0] != session_id or str(Path(task[1]).resolve()) != cwd:
        raise ValueError("Failed task ownership/cwd does not match the configured worker")
    if task[2] != "finished":
        raise ValueError("Failed task must remain terminal before recovery")
    if db.execute("SELECT 1 FROM tasks WHERE cwd=? AND id<>? AND state IN "
                  "('queued','dispatching','sent','running','uncertain') LIMIT 1",
                  (cwd, failed_task_id)).fetchone():
        raise ValueError("Another task owns this worktree; recovery is blocked")
    if db.execute("SELECT 1 FROM tasks WHERE id=?", (successor_task_id,)).fetchone():
        raise ValueError("Successor task ID already exists; choose a new task ID")

    values = {"id": recovery_id, "failed_sid": session_id, "failed_turn": turn,
              "failed_task_id": failed_task_id, "owner_sid": session_id,
              "owner_task_id": successor_task_id, "ownership_task_id": ownership_task_id,
              "cwd": cwd, "owned_worktree": worktree, "branch": branch,
              "head": head, "worktree_sha256": worktree_sha256,
              "adoption_id": adoption_id,
              "direct_turn_receipt": direct_turn_path,
              "direct_turn_sha256": direct_turn_digest,
              "direct_turn_evidence": direct_turn_evidence,
              "note": note}

    # Hold both queue and session locks while establishing the recovery record.
    # Dispatch cannot see an authorized record until the verified old lock is released.
    session_lock = opencode._session_lock(opencode.session_dir(session_id))
    queue_lock = None
    try:
        queue_lock = _queue_lock()
        existing = db.execute("SELECT * FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
        if existing:
            columns = [column[0] for column in
                       db.execute("SELECT * FROM recoveries LIMIT 0").description]
            row = dict(zip(columns, existing))
            if not _same_record(row, values):
                raise ValueError("Recovery ID was reused with different content")
            if row["status"] != "preparing":
                return {"recoveryId": recovery_id, "status": row["status"], "alreadyRecorded": True}
        else:
            if db.execute("SELECT 1 FROM recoveries WHERE failed_sid=? AND failed_turn=? "
                          "AND status IN ('preparing','authorized','claimed')",
                          (session_id, turn)).fetchone():
                raise ValueError("This failed OpenCode turn already has an active recovery record")
            if _queue_has_task(successor_task_id):
                raise ValueError("Successor task ID already appears in the root queue")

        active_worktree_task = _active_task_for_worktree(db, worktree, configured, successor_task_id)
        if active_worktree_task:
            raise ValueError(f"Another active task owns the explicit worktree: {active_worktree_task}")
        queued_worktree_task = _queued_task_for_worktree(db, worktree, configured, successor_task_id)
        if queued_worktree_task:
            raise ValueError(f"Another queued task owns the explicit worktree: {queued_worktree_task}")

        verify_failed_turn(session_id, failed_task_id, turn, cwd, configured_model)

        snapshot = worktree_snapshot(worktree)
        if (snapshot["branch"] != branch or snapshot["head"] != head
                or snapshot["worktreeSha256"] != worktree_sha256):
            raise ValueError("Worktree branch/head/partial-state fingerprint changed")
        if adoption is not None and (
                snapshot["branch"] != adoption["branch"]
                or snapshot["head"] != adoption["head"]
                or snapshot["worktreeSha256"] != adoption["worktree_sha256"]):
            raise ValueError("Worktree changed after ownership adoption")

        if not existing:
            now = time.time()
            db.execute("""INSERT INTO recoveries
                (id,failed_sid,failed_turn,failed_task_id,owner_sid,owner_task_id,
                 ownership_task_id,cwd,owned_worktree,branch,head,worktree_sha256,
                 adoption_id,direct_turn_receipt,direct_turn_sha256,note,status,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'preparing',?,?)""",
                       (recovery_id, session_id, turn, failed_task_id, session_id,
                        successor_task_id, ownership_task_id, cwd, worktree, branch, head,
                        worktree_sha256, adoption_id, direct_turn_path, direct_turn_digest,
                        note, now, now))
            db.execute("INSERT INTO events(kind,body,created) VALUES (?,?,?)",
                       ("opencode_recovery_prepared", json.dumps(values), now))
            db.commit()
    except sqlite3.IntegrityError as error:
        db.rollback()
        raise ValueError("An active recovery already owns this worker or worktree") from error
    except Exception:
        db.rollback()
        raise
    finally:
        if queue_lock:
            fcntl.flock(queue_lock.fileno(), fcntl.LOCK_UN)
            queue_lock.close()
        fcntl.flock(session_lock.fileno(), fcntl.LOCK_UN)
        session_lock.close()

    changed = db.execute("UPDATE recoveries SET status='authorized',updated=? "
                          "WHERE id=? AND status='preparing'",
                          (time.time(), recovery_id))
    db.commit()
    if changed.rowcount == 1:
        return {"recoveryId": recovery_id, "status": "authorized", "alreadyRecorded": False}
    row = db.execute("SELECT status FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
    if row and row[0] == "authorized":
        return {"recoveryId": recovery_id, "status": row[0], "alreadyRecorded": True}
    raise ValueError("Recovery record could not be activated; replay the same request to resume")


def recovery_for_task(db, recovery_id, task_id, sid, previous_task_id,
                      statuses=("authorized",)):
    row = db.execute("SELECT * FROM recoveries WHERE id=?", (recovery_id,)).fetchone()
    if not row:
        return None
    names = [item[0] for item in db.execute("SELECT * FROM recoveries LIMIT 0").description]
    record = dict(zip(names, row))
    if (record["status"] not in statuses or record["owner_sid"] != sid
            or record["owner_task_id"] != task_id
            or record["failed_task_id"] != previous_task_id):
        return None
    return record


def claim_recovery(db, recovery_id, task_id, sid, previous_task_id):
    record = recovery_for_task(db, recovery_id, task_id, sid, previous_task_id)
    if not record:
        return None
    changed = db.execute("UPDATE recoveries SET status='claimed',updated=? "
                         "WHERE id=? AND status='authorized'",
                         (time.time(), recovery_id))
    if changed.rowcount != 1:
        return None
    record["status"] = "claimed"
    return record


# --- retirement / reconciliation -------------------------------------------

_TERMINAL_NATIVE = {"done", "completed", "stopped", "error", "blocked"}


def recovery_for_successor(db, task_id, statuses):
    """The durable recovery row owning this successor task, if any."""
    placeholders = ",".join("?" * len(statuses))
    try:
        row = db.execute(f"SELECT * FROM recoveries WHERE owner_task_id=? "
                         f"AND status IN ({placeholders})", (task_id, *statuses)).fetchone()
    except sqlite3.OperationalError:
        # A legacy store without a recoveries table has no successor reservations.
        return None
    if not row:
        return None
    names = [item[0] for item in db.execute("SELECT * FROM recoveries LIMIT 0").description]
    return dict(zip(names, row))


def claimed_successor(db, task_id):
    """The claimed (dispatched) recovery owning this successor task, if any."""
    return recovery_for_successor(db, task_id, ("claimed",))


def _slot_for_session(session_id):
    slots = [s for s in paths.opencode_slots() if s.get("sessionId") == session_id]
    if len(slots) != 1:
        raise ValueError("Recovery target must be one existing OpenCode slot")
    return slots[0]


def verify_stopped_native_execution(slot, successor_task_id):
    """Prove the worker cannot still be running a managed native turn.

    Returns ``(True, evidence)`` only when the current managed turn is terminal,
    its recorded process identity/group is gone, and its cleanup was confirmed.
    Otherwise ``(False, reason)``. Never starts, resumes, or replays a turn.
    """
    sid = slot.get("sessionId")
    pointer = opencode.session_dir(sid) / "current.json"
    if not pointer.exists():
        return False, "managed native turn pointer is unavailable for successor task"
    try:
        launch = json.loads(pointer.read_text())
    except (OSError, ValueError):
        return False, "managed native turn pointer is unreadable"
    turn = launch.get("turn")
    if launch.get("sessionId") != sid or launch.get("taskId") != successor_task_id:
        return False, "managed native turn pointer does not identify the successor task"
    if not isinstance(turn, str) or not turn:
        return False, "managed native turn identifier is unavailable"
    turn_root = opencode.turn_dir(sid, turn)
    state_path, result_path = turn_root / "state.json", turn_root / "result.json"
    if not state_path.is_file() or not result_path.is_file():
        return False, "managed native turn state or result is unavailable"
    try:
        current = json.loads(state_path.read_text())
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        return False, "managed native turn state or result is unreadable"
    if not isinstance(current, dict) or not isinstance(result, dict):
        return False, "managed native turn state or result is invalid"
    if (any(source.get("sessionId") != sid or source.get("taskId") != successor_task_id
            or source.get("turn") != turn or source.get("cwd") != launch.get("cwd")
            for source in (current, result))
            or current.get("model") != launch.get("model")
            or result.get("expectedModel") != launch.get("model")
            or result.get("state") != current.get("state")
            or result.get("finishedAt") != current.get("finishedAt")):
        return False, "managed native turn receipts do not match the successor launch"
    if current.get("state") not in _TERMINAL_NATIVE:
        return False, f"managed native turn is still {current.get('state')}"
    if (current.get("cleanup") != "confirmed" or result.get("cleanup") != "confirmed"
            or not isinstance(current.get("finishedAt"), (int, float))):
        return False, "managed native turn cleanup is not confirmed"
    identity = result.get("managedProcessIdentity")
    if not _full_process_identity(identity):
        return False, "managed native process identity is missing or invalid"
    retained = current.get("processIdentity")
    if retained is not None and retained != identity:
        return False, "managed native process identities disagree"
    if opencode.identity_matches(identity):
        return False, "managed native process is still live"
    if opencode._group_exists(identity["pgid"]):
        return False, "managed native process group is still present"
    return True, "managed native turn and recorded process group are stopped with confirmed cleanup"


def retire_queued_successor(db, task_id, note):
    """Retire a queued-but-unstarted recovery atomically with its cancellation.

    Only ``preparing``/``authorized`` reservations can be retired: those are the
    states a task still in ``queued`` can hold. The failed task and turn rows are
    left untouched and are never replayed. Runs in the caller's transaction.
    """
    rows = []
    try:
        rows = db.execute("SELECT id FROM recoveries WHERE owner_task_id=? "
                          "AND status IN ('preparing','authorized')", (task_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    retired = []
    for (recovery_id,) in rows:
        changed = db.execute("UPDATE recoveries SET status=?,updated=? WHERE id=? "
                             "AND status IN ('preparing','authorized')",
                             (RETIRED, time.time(), recovery_id))
        if changed.rowcount == 1:
            retired.append(recovery_id)
    if retired:
        emit(db, "recovery_retired", taskId=task_id, recoveryIds=retired, note=note,
             reason="queued successor cancelled before dispatch")
    return retired


def reconcile_successor(db, task_id, note, slot=None, outcome="cancelled"):
    """Release a claimed successor reservation after a verified native stop.

    The release is refused unless the claimed successor's native execution is
    proven stopped; this is the single proof used by every release path, including
    manual ``ops.resolve_task``. Returns ``None`` when no claimed recovery owns the
    task. The failed task and turn rows are never touched or replayed.
    """
    record = claimed_successor(db, task_id)
    if not record:
        return None
    slot = _slot_for_session(record["owner_sid"]) if slot is None else slot
    # Synchronize with OpenCode send/turn ownership, then inspect the task state
    # inside a serialized DB transaction. Dispatching means the durable claim
    # exists but the new turn may not have written current.json yet; an old
    # terminal pointer must never authorize retiring that live claim.
    session_lock = opencode._session_lock(opencode.session_dir(record["owner_sid"]))
    try:
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        task = db.execute("SELECT state,sid FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task[1] != record["owner_sid"]:
            raise ValueError("Recovery successor task ownership changed; inspect before reconciling")
        if task[0] == "dispatching":
            raise ValueError("Recovery successor is dispatching; wait for restart/uncertainty boundary")
        if task[0] not in {"sent", "running", "uncertain", "cancelled"}:
            raise ValueError(f"Recovery successor is {task[0]}; stopped proof is not applicable")
        stopped, evidence = verify_stopped_native_execution(slot, task_id)
        if not stopped:
            raise ValueError("Native execution is not verified stopped: " + evidence)
        changed = db.execute("UPDATE recoveries SET status=?,updated=? "
                             "WHERE owner_task_id=? AND status='claimed'",
                             (RECONCILED, time.time(), task_id))
        if changed.rowcount != 1:
            db.rollback()
            raise ValueError("Recovery state changed; inspect before reconciling")
        row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0] not in {"finished", "cancelled"}:
            db.execute("UPDATE tasks SET state=?,updated=? WHERE id=?",
                       (outcome, time.time(), task_id))
        emit(db, "recovery_reconciled", recoveryId=record["id"], taskId=task_id,
             evidence=evidence, note=note)
    except Exception:
        db.rollback()
        raise
    finally:
        fcntl.flock(session_lock.fileno(), fcntl.LOCK_UN)
        session_lock.close()
    return {"taskId": task_id, "recoveryId": record["id"], "reconciled": True,
            "evidence": evidence}


def reconcile_launch_failure(db, task_id, turn, note):
    """Audit a proven non-executed launch; preserve the old task and every hold."""
    if not isinstance(note, str) or not note.strip():
        raise ValueError("Launch reconciliation requires an operator note")
    if not isinstance(turn, str) or not opencode._TURN_ID.fullmatch(turn):
        raise ValueError("Launch reconciliation turn is invalid")
    if not paths.PAUSE.is_file():
        raise ValueError("Fleet must remain durably paused")
    # Hold the same manager lock throughout; a concurrent restart must fail closed.
    with paths.MANAGER_LOCK.open("rb") as manager_lock:
        fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        task = db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or task[2] not in {"finished", "uncertain"}:
            raise ValueError("Launch task must be finished or uncertain; history is never rewritten")
        slot = opencode.verify_slot(_slot_for_session(task[0]))
        if slot.get("dispatchEnabled", False):
            raise ValueError("Worker must remain dispatch-disabled")
        sid = slot["sessionId"]
        session_lock = opencode._session_lock(opencode.session_dir(sid))
        try:
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT sid,cwd,state FROM tasks WHERE id=?", (task_id,)).fetchone() != task:
                raise ValueError("Launch task changed during validation")
            if db.execute("SELECT 1 FROM tasks WHERE sid=? AND state IN "
                          "('dispatching','sent','running')", (sid,)).fetchone():
                raise ValueError("Worker has another task in flight")
            if db.execute("SELECT 1 FROM recoveries WHERE (owner_sid=? OR failed_sid=?) "
                          "AND status IN ('preparing','authorized','claimed')", (sid, sid)).fetchone():
                raise ValueError("Active recovery reservations require their own reconciliation")
            pointer = opencode.session_dir(sid) / "current.json"
            directory = opencode.turn_dir(sid, turn)
            request_path = directory / "request.json"
            launch = json.loads(pointer.read_text())
            request = json.loads(request_path.read_text())
            identity = {"taskId": task_id, "sessionId": sid, "turn": turn,
                        "cwd": slot["cwd"], "model": slot["model"]}
            if (task[1] != slot["cwd"] or any(
                    launch.get(k) != v or request.get(k) != v for k, v in identity.items())):
                raise ValueError("Launch task/session/cwd/model identity mismatch")
            if any(os.path.lexists(directory / name) for name in ("state.json", "result.json")):
                raise ValueError("Native turn state/result exists; prelaunch reconciliation refused")
            pid = launch.get("wrapperPid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise ValueError("Recorded wrapper PID is missing")
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError("Recorded wrapper PID is still present")
            if opencode._group_exists(pid):
                raise ValueError("Recorded wrapper process group is still present")
            if slot["model"] == opencode.MODEL:
                raise ValueError("Legacy model rejection cannot prove this launch failure")
            if (request.get("databasePath") != slot["databasePath"]
                    or request.get("sessionCwd") != slot["cwd"]):
                raise ValueError("Native account/database routing differs")
            opencode.verify_native_session(sid, slot["cwd"], slot["databasePath"])
            started = launch.get("startedAt")
            if (not isinstance(started, (int, float)) or isinstance(started, bool)
                    or not 0 < started <= time.time()):
                raise ValueError("Launch timestamp is invalid")
            # Inspect metadata only, never native message bodies or auth records.
            uri = "file:" + opencode.quote(slot["databasePath"], safe="/") + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=2) as native:
                native.execute("PRAGMA query_only=ON")
                count = native.execute("SELECT count(*) FROM session_message WHERE session_id=? "
                                       "AND (typeof(time_created) NOT IN ('integer','real') OR time_created>=?)",
                                       (sid, int(started * 1000) - 1000)).fetchone()[0]
            if count:
                raise ValueError("Native messages exist at/after launch; no-inference proof unavailable")
            log = paths.LOGS / "opencode" / sid / turn / "wrapper.log"
            with log.open("rb") as stream:
                body = stream.read(32769)
            suffix = ('raise ValueError("OpenCode request model is not the pinned free model")\n'
                      'ValueError: OpenCode request model is not the pinned free model')
            if (len(body) > 32768 or not body.decode().rstrip().endswith(suffix)
                    or b"Traceback (most recent call last):" not in body):
                raise ValueError("Wrapper log does not prove the known pre-provider model guard")
            evidence = dict(identity, launchSha256=hashlib.sha256(pointer.read_bytes()).hexdigest(),
                            requestSha256=hashlib.sha256(request_path.read_bytes()).hexdigest(),
                            wrapperLogSha256=hashlib.sha256(body).hexdigest(),
                            nativeMessagesSinceLaunch=count, wrapperStopped=True,
                            note=note.strip())
            # Commit the audit event before making the worker dispatchable. A file
            # write failure leaves it blocked; an idempotent retry repairs the marker.
            events = db.execute("SELECT id,body FROM events WHERE kind='opencode_launch_reconciled'").fetchall()
            existing = next(((i, json.loads(raw)) for i, raw in events
                             if json.loads(raw).get("taskId") == task_id
                             and json.loads(raw).get("turn") == turn), None)
            if existing:
                if any(existing[1].get(k) != v for k, v in evidence.items() if k != "note"):
                    raise ValueError("Previously audited launch evidence changed")
                record, evidence = existing  # Preserve the first operator note and evidence.
            else:
                record = emit(db, "opencode_launch_reconciled", **evidence)
            db.commit()
            atomic(directory / "launch-reconciled.json", dict(evidence, record=record))
            return {"taskId": task_id, "sessionId": sid, "turn": turn,
                    "reconciled": True, "taskStatePreserved": task[2], "record": record,
                    "evidence": evidence}
        except Exception:
            db.rollback()
            raise
        finally:
            fcntl.flock(session_lock.fileno(), fcntl.LOCK_UN)
            session_lock.close()
