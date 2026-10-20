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
- **Stale `running` row after a fleet replacement** — a managed turn can finish while no manager is
  observing its worker, so the ledger row stays `running` and keeps the same-cwd dispatch
  exclusivity. `bin/fleet resolve` only accepts `uncertain` rows and `bin/fleet cancel` aborts a live
  turn, so neither can close it. Verify the retained native receipt and use
  `bin/fleet reconcile-completed` (see below).
- **Moving `data/`** — `bin/fleet stop`, `scripts/db-check`, then move including no `-wal/-shm`.

## A managed turn completed but its ledger row is still running

A fleet replacement, or a manager outage while the turn ended, leaves the task row in
`running` even though the native turn finished. The manager closes such a row only while it
observes that worker, so a worker that left the roster leaves the row behind, and the row keeps
the same-cwd dispatch exclusivity for every other task in that working directory.

One supported operation closes exactly that row. It is not a release and not a state flip: it
requires the exact task, turn, worker, session and working-directory identity, the retained native
completion receipt, and proof that the native execution stopped.

```sh
bin/fleet reconcile-completed --task 20261020-fleet-replacement-cutover-w5-01 \
  --turn 86f3236e-6327-4862-ace1-b23c0da84896 \
  --worker ses_eaa694010ffebFoPIycvHTD3uB \
  --receipt-root /tmp/opencode-three-fleet-20261018 \
  --note 'retained receipt proves a terminal done turn with exit 0 and confirmed cleanup'
```

`--receipt-root` is the Fleet root that still holds the turn, which after a replacement is the
retired root: the command reads `<root>/data/opencode/<session>/turns/<turn>/` and never the live
root's other data. It requires the retained `request.json`, `state.json`, `result.json` and the
session's `current.json` pointer to identify the same task, turn, session and cwd; the native state
and result to be terminal `done` with exit code 0 and confirmed cleanup; a full recorded native
process identity and a recorded wrapper PID that are provably gone; the worker to be one this fleet
no longer manages; and no other task to own that working directory. A completed turn ran a process,
so a receipt that records no process identity, or records a null identity beside a successful
completion, fails closed. An unknown is never read as "nothing there": a missing, invalid or
unreadable roster refuses, a missing, unreadable or corrupt other-root ledger refuses, and
ownership in another root is matched by task id, by session and by canonical working directory.
That root's ledger is fenced with its own write lock from the verified ownership query through the
current-ledger commit, because a transaction on the current ledger does not fence a separate
database: a claim written into the retired root in between is blocked instead of slipping through.
The session's dispatch lock is held from the first validation through the ledger commit, and the
roster and the competing ownership are rechecked inside that transaction, so a native launch or a
task that claims the working directory in between is refused instead of raced. A missing, unreadable, uncertain or
inconsistent receipt is a refusal, and the ledger is left unchanged.

The command records one `opencode_completed_reconciled` event with the receipt digests and moves
the row to `finished`. The task body, the retained receipt, every hold, the global pause, the
per-worker `dispatchEnabled`, the model and the account are untouched, and the same-cwd dispatch
exclusion rule is unchanged — only this one stale row stops blocking that directory. A task that is
still `uncertain` is refused here and still needs `bin/fleet resolve`; a turn that is still running
is refused and still needs `bin/fleet cancel`. Never edit SQLite or delete history to close a
stale row.

## OpenCode wrapper selected stale source before native execution

`FLEET_ROOT` selects durable data/config/logs; `paths.SOURCE_ROOT` and `paths.BIN`
select the installed source that imported Fleet. Child wrappers use that source's
entrypoint, even when the data root contains an old `bin/fleet` or source snapshot.
A missing native `state.json` after the launch grace period is `launchFailure`;
the manager preserves the terminal `finished` ledger convention and emits
`review_ready` with `state: blocked`, explicit `outcome: launch_failed`, and the
launch error. This is a failed wrapper attempt, not successful audit completion.
A claimed recovery reservation is not marked completed by this launch failure.

For the known legacy model-guard failure, after root reviews and applies this fix,
an operator may run the reviewed source entrypoint with the intended `FLEET_ROOT`:

```
FLEET_ROOT=/tmp/opencode-three-fleet-20261018 FLEET_CONFIG=/tmp/opencode-three-fleet-20261018/config/fleet.local.json python3 /Users/amf/Developer/.codex/fleet/bin/fleet reconcile-opencode-launch --task 20261019-go-adea-ci-migration-inventory-e7f19faa --turn 5e62374a-d022-4256-8b69-bee5c0837f68 --note "Root reviewed stale-source rejection before native execution; audit remains incomplete"
```

This requires durable pause, a stopped manager, a disabled exact worker, a free
session lock, no active recovery reservation or task in flight, matching task /
turn / model / native database identity, no native state or result, a dead wrapper
PID and process group, zero native message metadata at/after launch (including a
one-second margin), and the bounded traceback ending at the known legacy guard.
An unknown error, quota failure, existing native messages/state, or uncertain
process proof blocks reconciliation. It reads no auth data or native message text.

The command commits an audit event before writing an identity-bound
`launch-reconciled.json`. The worker then reports `stopped`; the original request,
launch pointer, wrapper log and task row remain unchanged. The old `finished`
state is preserved as historical evidence, not an audit success. A task already
`uncertain` stays uncertain and requires an explicit supported operator resolution
before its checkout can dispatch again. No dispatch/quota/task hold is released.

Never reuse the failed task ID. Root must authorize a new same-session task ID
linked by `previousTaskId`, preserving its scoped checkout and transfer exclusions.
Do not use native error-turn recovery for this missing-state case. Session creation,
roster enablement and manager resume are separate controller actions after review;
the reconciliation command does none of them. Do not edit SQLite or delete history.
