"""Bound concurrent heavy validation jobs without changing test timeouts."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

SLOT_COUNT = 2


def try_acquire(directory: Path):
    """Return one locked file, or None. Stale files do not hold leases."""
    directory.mkdir(parents=True, exist_ok=True)
    for slot in range(SLOT_COUNT):
        lease = (directory / f"slot-{slot}.lock").open("a+")
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lease.close()
            continue
        except BaseException:
            lease.close()
            raise
        return lease
    return None


def run(command: list[str], directory: Path) -> int:
    if not command:
        print("Usage: fleet-heavy -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    child = None
    lease = None
    previous = {}

    def forward(signum, _frame):
        if child is None:
            raise SystemExit(128 + signum)
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, forward)
        last_notice = 0.0
        while lease is None:
            lease = try_acquire(directory)
            if lease is not None:
                break
            now = time.monotonic()
            if now - last_notice >= 30:
                print("[fleet-heavy] Waiting for a validation slot; the command has not started.",
                      file=sys.stderr, flush=True)
                last_notice = now
            time.sleep(1)
        # Diagnostic metadata excludes arguments, environment and credentials.
        lease.seek(0)
        lease.truncate()
        json.dump({"pid": os.getpid(), "program": Path(command[0]).name,
                   "startedAt": time.time()}, lease)
        lease.flush()
        # The inherited lease also survives an abrupt wrapper death while the
        # child is still running. Never unlink lock files while jobs may run.
        try:
            child = subprocess.Popen(command, start_new_session=True,
                                     pass_fds=(lease.fileno(),))
        except OSError as error:
            print(f"[fleet-heavy] Could not start command: {error.__class__.__name__}",
                  file=sys.stderr)
            return 127
        result = child.wait()
        return result if result >= 0 else 128 - result
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if lease is not None:
            # Closing, rather than explicit LOCK_UN, preserves an inherited
            # lease until any surviving child holding the descriptor exits.
            lease.close()


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--"]:
        args = args[1:]
    root = Path(__file__).resolve().parents[2]
    return run(args, root / "data" / "heavy-tests")


if __name__ == "__main__":
    raise SystemExit(main())
