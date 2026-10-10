# Architecture
- **manager** (`bin/fleet run`, single instance via `data/manager.lock`): every 25s observes all
  sessions, records snapshots/events, imports `data/queue.json` (author must be `root`, tasks
  `rootAuthorized`), dispatches when not paused, writes `data/status.json`.
- **dispatch**: a queued task is sent only if its worker is idle, no session in the same cwd is
  busy/blocked, no other task for that cwd is in flight/uncertain, and no session is `unknown`/
  `unavailable` for that cwd (or with no cwd). State goes `queued → dispatching` (committed before
  send) `→ sent → running → finished`; a send error or a restart during `dispatching` gives
  `uncertain`, which is never retried automatically. `sent` not consumed in 90s → `uncertain`.
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
