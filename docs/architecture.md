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
