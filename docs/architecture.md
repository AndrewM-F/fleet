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
  worktree path recorded by a finished task for the same session. Native session CWD stays
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
  recovery reservations stay in force.
- **completed-turn reconciliation**: a managed turn can finish while the manager is not observing
  its worker — after a fleet replacement, or during a manager outage — and the ledger row then stays
  `running` forever, because only that worker could close it. The surviving row keeps the same-cwd
  dispatch exclusivity, so nothing else can use that working directory. `reconcile-completed` closes
  exactly that row: it requires the exact task/turn/worker/session/cwd identity, the retained native
  receipt (terminal `done`, exit 0, cleanup confirmed), a recorded native process identity and
  wrapper PID that are provably gone, a worker this fleet no longer manages, and no competing task
  for the same cwd. An unknown is never read as "nothing there": a missing, invalid or unreadable
  roster refuses, a missing, unreadable or corrupt other-root ledger refuses, and a receipt without a
  process identity refuses unless it explicitly records that no process ever started. The session's
  dispatch lock is held from the first validation through the ledger commit, and the roster and the
  competing ownership are rechecked inside the transaction that closes the row, so a new native
  launch or a task that claims the working directory in between is refused instead of raced. It
  writes one audit event carrying the receipt digests and moves the row to `finished`; the task body,
  the retained receipt, every hold, the pause, the dispatch flags, the model and the account stay
  untouched, and the same-cwd dispatch exclusion rule is unchanged. A missing, uncertain or
  inconsistent receipt is a refusal, an `uncertain` row still needs `resolve`, and the dispatch
  exclusion rule itself is unchanged.
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
