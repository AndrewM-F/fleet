"""The one writer lock for the roster, the queue and the dispatch claim.

The roster is the identity source of the fleet, and three writers touch it: an
operator command that adds or removes a worker, the queue that brings a
root-authored task in, and the dispatcher that claims a task for a worker. They
share one lock, so a claim, an enqueue, an import and a removal cannot observe
each other half-applied.

The lock order is fixed and never inverted:

    queue lock  →  roster lock  →  model lock

A writer that needs more than one takes them in that order, so two writers can
never wait for each other. The roster write itself is a compare-and-swap: the
configuration is compared with the copy the writer started from, and a
configuration somebody else changed is never overwritten.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from . import paths


@contextmanager
def queue_lock():
    """The lock that guards the root-authored queue file."""
    paths.DATA.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(paths.QUEUE_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def roster_lock():
    """The shared writer lock for the roster, the queue import and the claim.

    A worker that is drained while a claim is in flight must not launch a
    stale pre-drain slot afterwards, so the drain and the claim take the same
    lock: the claim either happens before the drain, and the drain sees the
    task, or after it, and the claim sees the draining flag.
    """
    paths.DATA.mkdir(parents=True, exist_ok=True)
    path = Path(paths.DATA) / "fleet.roster.lock"
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def config_digest():
    """The digest of the configuration as it is right now."""
    try:
        return hashlib.sha256(paths.CONFIG.read_bytes()).hexdigest()
    except OSError:
        return ""


def write_config_cas(cfg, expected_digest):
    """Write the configuration only if nobody else changed it meanwhile.

    A writer that started from one configuration and finds another one has
    lost the race: the change is refused rather than merged blindly, because
    the roster is the identity source and a lost entry is an unowned worker.
    """
    if config_digest() != expected_digest:
        raise ValueError("The configuration changed while the request was running; nothing was "
                         "written. Inspect the roster and retry.")
    paths.write_config(cfg)


class QueueUnreadable(RuntimeError):
    """The root-authored queue could not be read, so the work in it is unknown."""


def queued_tasks_for(session_id, db=None):
    """The root-authored tasks that are still waiting to be imported for a worker.

    A task in the queue file is not in the task ledger yet, so it is easy to
    miss: a task that names this worker and is not in the ledger is reported,
    so a removal never drops work that has not been imported yet. A task that
    **is** in the ledger was already imported: it is history, and it does not
    block anything.

    An unreadable or malformed queue is **not** an empty queue. The work in it
    is unknown, so this refuses instead of reporting nothing.

    The caller holds the queue lock: this reads the file under it and never
    takes the lock itself, because a second lock on the same file in one
    process waits for itself.
    """
    try:
        present = paths.QUEUE.exists()
        raw = paths.QUEUE.read_text() if present else ""
    except OSError as error:
        raise QueueUnreadable("the root-authored queue could not be read: "
                              + error.__class__.__name__) from error
    if not present:
        # A genuinely missing queue file is no queue: there is nothing waiting.
        return []
    if not raw.strip():
        # The file exists but carries no document. That is invalid evidence, not
        # an empty queue, so it refuses.
        raise QueueUnreadable("the root-authored queue is empty")
    try:
        queue = json.loads(raw)
    except ValueError as error:
        raise QueueUnreadable("the root-authored queue is malformed") from error
    if not isinstance(queue, dict) or not isinstance(queue.get("tasks"), list):
        raise QueueUnreadable("the root-authored queue is malformed")
    if queue.get("author") != "root":
        raise QueueUnreadable("the root-authored queue has no root author")
    found = []
    for task in queue.get("tasks", []):
        if not isinstance(task, dict) or task.get("sessionId") != session_id:
            continue
        task_id = task.get("id")
        if db is not None and task_id and db.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            # Already imported: terminal history, not work waiting to arrive.
            continue
        found.append({"taskId": task_id, "state": "awaiting-import"})
    return found
