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
- **OpenCode error turn** — do not resolve it with the Claude-only `--worker` command or reset its
  task. Verify the failed task and cleanup receipt, then snapshot the separate owned checkout with
  `bin/fleet recovery-snapshot --cwd OWNED_WORKTREE`. The configured native session CWD stays as-is.
  Authorize a successor only when a finished task for the same worker records the branch and exact
  owned-worktree path:
  ```sh
  bin/fleet recover-opencode --worker SESSION_ID --task FAILED_TASK_ID --turn TURN_ID \
    --recovery-id RECOVERY_ID --successor-task NEW_TASK_ID --ownership-task OWNER_TASK_ID \
    --owned-worktree OWNED_WORKTREE --branch BRANCH --head COMMIT_SHA \
    --worktree-sha256 DIGEST --note 'verified cleanup and preserved partial work'
  ```
  Enqueue only the new task, linked to both the failed task and returned recovery ID:
  `{"id":"NEW_TASK_ID","previousTaskId":"FAILED_TASK_ID","recoveryId":"RECOVERY_ID","rootAuthorized":true,"message":"..."}`.
  Fleet prepends the recorded owned checkout and branch to every recovery turn's prompt,
  including a message such as `continue`, and records them in the turn request. Native
  session CWD remains unchanged. Missing/unreadable checkouts and Git validation errors
  leave the successor queued with one `needs_attention` event; unrelated queued tasks
  can still dispatch. Inspect and correct the checkout before attempting recovery again.

  The fingerprint covers branch/HEAD, Git porcelain status, `git diff --binary HEAD`,
  and non-ignored untracked file contents (symlink targets are hashed without following
  them). It is a limited change detector, not a backup or complete partial-state capture:
  ignored files, nested repository/submodule file contents, and intermediate staged
  versions distinct from the current worktree are not captured. Git may report submodule
  dirtiness without fingerprinting its contents; repository ignore settings may hide it.
  Do not infer preservation of these omitted states from a matching digest. Review their
  relevance separately without reading secrets or recursively hashing dependency trees.

  Recovery refuses active locks, incomplete cleanup, changed worktree state, duplicate owners, and
  rate-limit/quota errors. It never changes pause state, `dispatchEnabled`, model, accounts, or the
  failed turn. It retains ownership on the same configured OpenCode session while binding the
  separate owned Git worktree. Cross-session transfer is not supported. Never rebuild
  `fleet.sqlite` from `queue.json`; preserve its task/event/receipt history. A successor send becomes
  single-use at its durable dispatch claim; inspect an `uncertain` successor instead of replaying it.
- **Moving `data/`** — `bin/fleet stop`, `scripts/db-check`, then move including no `-wal/-shm`.
