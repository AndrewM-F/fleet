# Architecture
- **manager** (`bin/fleet run`, single instance via `data/manager.lock`): every 25s observes all
  sessions, records snapshots/events, imports `data/queue.json` (author must be `root`, tasks
  `rootAuthorized`), dispatches when not paused, writes `data/status.json`.
- **dispatch**: a queued task is sent only if its worker is idle, no session in the same cwd is
  busy/blocked, no other task for that cwd is in flight/uncertain, and no session is `unknown`/
  `unavailable` for that cwd (or with no cwd). State goes `queued → dispatching` (committed before
  send) `→ sent → running → finished`; a send error or a restart during `dispatching` gives
  `uncertain`, which is never retried automatically. `sent` not consumed in 90s → `uncertain`.
- **queued holds**: an operator may hold one unstarted queued task without cancelling it. The
  current status/reason live in `fleet.sqlite` (`task_holds`) with an event audit trail, so a manager
  restart preserves the hold and dispatch skips the task. Hold/release are idempotent and refuse a
  started or terminal task. The dispatch claim re-checks the hold atomically, so a hold that races a
  send wins. Release clears only this task-level hold; pause, `dispatchEnabled`, capacity/quota gates,
  and recovery reservations are untouched.
- **OpenCode recovery**: a failed task and native turn stay terminal. An operator may authorize one
  new task only after the exact error/cleanup receipt, free session lock, and an explicit owned
  worktree path proven by a finished task for the same session (its free-text message, an
  immutable `recovery_adoptions` record when history predates the exact path/branch convention, or
  a durable direct-turn ownership receipt in `data/recovery/` when the original native turn
  predates the task ledger entirely and no task row exists). A direct-turn receipt records the
  original session, the canonical owned worktree, the branch, the initial head, and the exact
  original user and successful idle message IDs. Those IDs bind a completed assistant to one
  native turn before the failed managed turn; the mutable session-wide idle outcome cannot
  substitute for that proof. The native session directory/model and unchanged checkout are also
  checked before authorization; its path and digest
  are stored with the recovery and re-proved before the successor dispatches. It never creates,
  edits or fabricates a Fleet task.
  Native session CWD stays
  unchanged; branch/head and tracked/untracked fingerprint are checked at authorization, dispatch,
  and send. SQLite records the immutable failure-to-successor/worktree owner link before it becomes
  dispatchable. Dispatch atomically consumes that authorization with the successor's
  `dispatching` claim; restart turns an ambiguous claim into `uncertain`, never a resend. The old
  task ID is never reset or requeued, and `fleet.sqlite` is never reconstructed from the queue.
  Cancelling a queued, unstarted successor retires its authorization in the same transaction, so
  the reservation cannot outlive its successor; the failed task/turn are preserved and a fresh
  successor may be authorized for the same turn. A `claimed` successor reservation is released only
  through verified stopped-native-execution reconciliation (`reconcile-recovery`, or `resolve
  --task` for an `uncertain` successor), never by a bare state flip. Retirement/reconciliation touch
  only that recovery row and task: pause, `dispatchEnabled`, capacity/quota gates, and unrelated
  recovery reservations stay in force. Ownership adoption is append-only evidence: it never queues,
  dispatches, starts a turn, or authorizes a successor by itself, never rewrites task/event/turn
  history, and authorization plus dispatch revalidate the same adoption row and pinned snapshot.
  The adoption separates a locator (`evidence_reference`) from substantive ownership prose
  (`ownership_evidence`); a URL, checksum, or path alone is rejected as evidence, and the operator
  must inspect and verify the referenced source. The locator is not proof by itself.
- **completed-turn reconciliation**: a managed turn can finish while the manager is not observing
  its worker — after a fleet replacement, or during a manager outage — and the ledger row then stays
  `running` forever, because only that worker could close it. The surviving row keeps the same-cwd
  dispatch exclusivity, so nothing else can use that working directory. `reconcile-completed` closes
  exactly that row: it requires the exact task/turn/worker/session/cwd identity, the retained native
  receipt (terminal `done`, exit 0, cleanup confirmed), a full recorded native process identity and
  a recorded wrapper PID that are both provably gone, a worker this fleet no longer manages, and no
  competing task for the same cwd. A completed turn ran a process, so a receipt that records no
  identity, or a null identity beside a successful completion, fails closed. An unknown is never
  read as "nothing there": a missing, invalid or unreadable roster refuses, a missing, unreadable
  or corrupt other-root ledger refuses, and ownership in another root is matched by task id, by
  session and by canonical working directory. That root's ledger is fenced with its own write lock
  from the verified ownership query through the current-ledger commit, because a transaction on the
  current ledger does not fence a separate database: a claim written into the retired root in
  between is blocked instead of slipping through. The session's dispatch lock is held from the
  first validation through the ledger commit, and the roster and the competing ownership are
  rechecked inside the transaction that closes the row, so a new native launch or a task that
  claims the working directory in between is refused instead of raced. It writes one audit event carrying the receipt digests and moves the row
  to `finished`; the task body, the retained receipt, every hold, the pause, the dispatch flags, the
  model and the account stay untouched, and the same-cwd dispatch exclusion rule is unchanged. A
  missing, uncertain or inconsistent receipt is a refusal, an `uncertain` row still needs `resolve`,
  and the dispatch exclusion rule itself is unchanged.
- **events**: `review_ready`, `task_uncertain`, `task_unconsumed`, `runner_error`,
  `needs_attention`, `possibly_stalled` (20 min no progress) are relayed; `review_ready` captures
  a receipt in `data/receipts/<event>.json` from the Claude `result.json` or Pi session transcript.
  Events persist until acked.
- **Pi transport**: one detached controller per lane (`flock` on `data/lanes/<lane>/controller.lock`
  prevents duplicate writers) runs `pi --mode rpc --session <file>` and forwards commands over a
  unix socket in `data/run/`. Startup checks the session header id (and cwd/sessionCwd).
- **Claude transport**: `mode: print` launches `bin/fleet claude-turn`, which runs one
  `claude --resume <id> --print` turn and verifies the session id/model from the stream.
  Native `--resume --bg` remains available but opt-in.
