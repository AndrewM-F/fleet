# Troubleshooting
- **Lane `unavailable: Pi controller/config mismatch`** — controller not running or started with
  other settings. `bin/fleet start --lane L`.
- **`Existing socket` / duplicate controller** — `start` skips lanes whose lock is held; a stale
  socket with a free lock is removed automatically.
- **Claude transcript not discoverable** (`doctor` warning) — Claude Code files transcripts by the
  cwd they were created in. After relocating a worktree, `scripts/link-claude-transcripts` shows the
  symlinks it would add (original transcript untouched); `--apply` creates them. Without it a
  resumed turn fails closed (`blocked: identity unconfirmed`), never creating a new session.
- **Claude session `error` / `blocked`** — inspect `data/claude/<sid>/<turn>/state.json` and
  `logs/claude/<sid>/<turn>/stderr.log`. The worker stays non-idle (no dispatch) until a later
  successful turn. After review, `bin/fleet resolve --worker <sid> --note …` makes it dispatchable;
  its next turn still resumes the same session.
- **Pi lane exits with "Stored session working directory does not exist"** — the session header
  cwd is gone (worktree moved). `bin/fleet stop --lane L`, `scripts/rehome-pi-session --lane L
  --apply` (backs up the original header), `bin/fleet start --lane L`.
- **Supervisor gave up** (`needs_attention` "supervisor stopped retrying") — a component crashed 3x in
  10 min. Read `logs/lanes/<lane>/stderr.log` / `logs/manager/fleet.log`, fix, `bin/fleet start`.
- **Hung turn** — Claude turns stop after `claudeTurnTimeoutSeconds` (config, 0 = off); otherwise
  `bin/fleet cancel <task> --note …`.
- **`uncertain` task** — inspect the worker's transcript; enqueue a new task ID if work is needed.
- **Moving `data/`** — `bin/fleet stop`, `scripts/db-check`, then move including no `-wal/-shm`.
