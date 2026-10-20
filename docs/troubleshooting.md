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
- **Held queued task** — an operator paused it with `bin/fleet hold TASK --note …`; it stays queued
  under the same ID and dispatch skips it. Inspect holds with `bin/fleet status` (`heldTasks`) and
  resume one with `bin/fleet release TASK --note …`. Both commands are idempotent and refuse a
  started or terminal task. The hold is durable, so a manager restart keeps it. `release` clears only
  that task-level hold — a global `bin/fleet pause`, a worker's `dispatchEnabled`, a capacity limit,
  or an active recovery reservation still gates dispatch, so a released task may stay queued until
  those clear too.
- **OpenCode error turn** — do not resolve it with the Claude-only `--worker` command or reset its
  task. Verify the failed task and cleanup receipt, then snapshot the separate owned checkout with
  `bin/fleet recovery-snapshot --cwd OWNED_WORKTREE`. The configured native session CWD stays as-is.
  Authorize a successor only when a finished task for the same worker proves the branch and exact
  owned-worktree path:
  ```sh
  bin/fleet recover-opencode --worker SESSION_ID --task FAILED_TASK_ID --turn TURN_ID \
    --recovery-id RECOVERY_ID --successor-task NEW_TASK_ID --ownership-task OWNER_TASK_ID \
    --owned-worktree OWNED_WORKTREE --branch BRANCH --head COMMIT_SHA \
    --worktree-sha256 DIGEST --note 'verified cleanup and preserved partial work'
  ```
  If the original owner task legitimately instructed the worker to create that branch and
  worktree, its historical message may not contain them. Record append-only ownership evidence
  once, then consume it instead of free text:
  ```sh
  bin/fleet adopt-ownership --id ADOPTION_ID --owner-task OWNER_TASK_ID --worker SESSION_ID \
    --failed-task FAILED_TASK_ID --turn TURN_ID --owned-worktree OWNED_WORKTREE \
    --branch BRANCH --head FULL_COMMIT_SHA --worktree-sha256 DIGEST \
    --decision 'explicit ownership decision' \
    --evidence-reference 'URL, path, message ID, or record ID to inspect' \
    --ownership-evidence 'quoted ownership excerpt or substantive statement' --note '...'
  bin/fleet recover-opencode ... --adoption-id ADOPTION_ID
  ```
  A genuine native turn that predates the Fleet task ledger has no owner task row at all, so
  neither an owner task nor an adoption can express its ownership. Record the turn's ownership in
  a durable receipt inside the fleet's own recovery directory, then use it as the ownership source:
  ```sh
  cat > "$FLEET_ROOT/data/recovery/direct-turn-<id>.json" <<'JSON'
  {
    "schemaVersion": "pi-direct-turn-ownership/v1",
    "originalSession": "ses_...",
    "ownedWorktree": "/absolute/path/to/the/owned/worktree",
    "branch": "branch-name",
    "initialHead": "full commit sha the direct turn started from",
    "startedAt": 1792480175,
    "note": "substantive operator statement about that direct turn"
  }
  JSON
  bin/fleet recover-opencode ... --direct-turn-receipt "$FLEET_ROOT/data/recovery/direct-turn-<id>.json"
  ```
  The receipt is proved before anything is authorized: its session must be the recovering worker,
  the native session row must show that session in the recorded worktree with the configured model,
  a succeeded idle outcome and completed assistant output, the recorded branch must be the
  checkout's branch, the recorded initial head must be an ancestor of the current head, and the
  current snapshot must be unchanged. The receipt's path and digest are recorded with the recovery
  and re-proved before the successor dispatches, so a receipt that is later edited is refused. A
  missing, foreign, tampered or contradictory receipt is a refusal; no Fleet task is created,
  edited or fabricated for the ownership. Exactly one ownership source is accepted: an owner task,
  an adoption, or a direct-turn receipt.

  Adoption requires a finished owner task on the same session and native CWD, the canonical
  checkout, branch, full HEAD SHA, current digest, an explicit decision, a distinct evidence
  reference, and substantive ownership evidence. The reference is only a locator: the operator
  must inspect and verify the referenced source before adopting. A URL, checksum, or path alone
  is not substantive evidence and is rejected; quote the ownership statement in
  `--ownership-evidence` and put the locator in `--evidence-reference`. Adoption alone never
  queues or dispatches a task, starts a turn, rehomes a session, or authorizes a successor, and
  it never rewrites task/event/turn history. Authorization and dispatch revalidate the same
  adoption row and pinned snapshot; a missing record or any checkout change fails closed.
  Reusing an adoption ID with an altered reference, evidence excerpt, decision, or any other
  field is rejected; an exact replay remains idempotent across restart.
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

  **Retire or reconcile a recovery.** Cancelling a still-queued successor with `bin/fleet cancel`
  atomically retires its authorization, releasing the reservation while preserving the failed task
  and turn verbatim. A fresh successor may then be authorized for the same failed turn. Once a
  successor has been dispatched (`claimed`), its reservation is released only after native
  execution is verified stopped: `bin/fleet reconcile-recovery --task SUCCESSOR --note "..."` for a
  `sent`/`running`/`cancelled` successor, or `bin/fleet resolve --task SUCCESSOR --as cancelled`
  for an `uncertain` one. Both use the same proof — the managed turn must be terminal with confirmed
  cleanup and no live process identity/group — and refuse a bare state flip. Retirement and
  reconciliation touch only that recovery row and task; global pause, per-worker `dispatchEnabled`,
  capacity/quota gates, and unrelated recovery reservations stay in force.
- **Moving `data/`** — `bin/fleet stop`, `scripts/db-check`, then move including no `-wal/-shm`.
