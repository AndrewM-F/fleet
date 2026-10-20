# Fleet operating contract (for any agent: Claude, Codex, Pi, scripts)

This directory is a small local manager that sends **root-authorized** tasks to existing
Pi and Claude worker sessions. It never calls a model itself. Use `bin/fleet` for lifecycle and queue control; every
command prints JSON on stdout and exits non-zero on failure.

## Safe, read-only
- `bin/fleet status` — pause flag, process liveness, per-session state, task counts.
- `bin/fleet doctor` — validates config, worktrees, session identities, executables, DB.
- `bin/fleet completions collect` — pending relay events with completion receipts.

## State-changing (only when your principal asked for it)
- `bin/fleet enqueue '<task-or-list>'` — task: `{"id","message","rootAuthorized":true}` plus
  exactly one of `sessionId`, `lane`, `previousTaskId`. IDs are idempotent; reuse with different
  content is rejected.
- `bin/fleet control '<request>'` — enqueue + ack + wait in one call:
  `{"tasks":[...],"ack":[eventIds],"after":0,"limit":1-4,"timeout":0-3600}`.
- `bin/fleet completions wait|ack --ids 1,2` — block for / acknowledge relay events.
- `bin/fleet pause` / `bin/fleet resume` — gate new dispatches. **Resume is a human decision.**
- `bin/fleet start` / `bin/fleet stop [--lane L]` — never changes the pause flag; `stop` refuses
  while any Pi lane is busy unless `--force`. A launchd supervisor restarts crashed components
  every 30s; `stop` (whole fleet or `--lane`) disables that until the next `start`.
- `bin/fleet resolve --events 1,2 | --task ID --as finished|cancelled | --worker SID --note "..."`
  — record an operator decision (ack events, close an uncertain task, mark a failed Claude turn
  reviewed so the worker can take work again). Notes are stored as events.
- `bin/fleet recovery-snapshot --cwd PATH` / `bin/fleet recover-opencode ...` — fingerprint and
  authorize a one-use successor for an errored OpenCode turn. Recovery requires exact failed task,
  turn, owner task, explicit owned worktree, branch, commit, and worktree digest; verified cleanup;
  and a free session lock. It keeps the native session CWD unchanged and records the separately
  verified owned worktree. It preserves the failed task/turn and database history; never rebuild
  `fleet.sqlite` from the queue. It does not clear pause/dispatch holds or alter the pinned model.
  The successor must be a new root-authorized task linked by `previousTaskId` and the returned
  `recoveryId`. Never retry the old task ID. Rate-limit/quota errors are not recoverable through
  this command. Recovery is same-session only; do not infer or implement cross-session transfer.
  `bin/fleet cancel` of a queued, unstarted successor atomically retires its authorization, so the
  reservation never outlives a cancelled successor; the failed task/turn stay preserved and a fresh
  successor may then be authorized for the same failed turn. A dispatched (`claimed`) successor's
  reservation is released only by `bin/fleet reconcile-recovery --task ID --note "..."` (or
  `resolve --task` for an `uncertain` successor) after native execution is verified stopped.
  When the original owner task predates the exact worktree/branch convention (for example, it
  instructed the worker to create them), `bin/fleet adopt-ownership --id ID --owner-task TASK
  --worker SID --failed-task TASK --turn TURN --owned-worktree PATH --branch BRANCH --head SHA
  --worktree-sha256 DIGEST --decision "..." --evidence-reference "LOCATOR"
  --ownership-evidence "EXCERPT/STATEMENT" --note "..."` appends immutable ownership evidence,
  and `recover-opencode --adoption-id ID` consumes it instead of free text. `--evidence-reference`
  is only a locator (URL, path, message ID, or record ID) that the operator must inspect and
  verify; it is not proof by itself. `--ownership-evidence` must be the substantive ownership
  excerpt or statement, not the same locator and not a bare URL, checksum, or path. Adoption alone
  never queues or dispatches a task, starts a turn, rehomes a session, or authorizes a successor,
  A genuine native turn that predates the Fleet task ledger has no owner task row at all. For
  that case `bin/fleet recover-opencode --direct-turn-receipt PATH` is the ownership source
  instead of `--ownership-task` or `--adoption-id`; exactly one ownership source is accepted. The
  receipt is a durable JSON file in the fleet's own `data/recovery/` directory that records the
  original session, the canonical owned worktree, the branch, the initial head, the turn's start
  time and an operator statement. It is proved before authorization: the receipt's session must be
  the recovering worker, the native session row must show that session in the recorded worktree
  with the configured model and a succeeded idle outcome and completed assistant output, the
  recorded branch must be the checkout's branch, the recorded initial head must be an ancestor of
  the current head, and the current snapshot must be unchanged. The receipt's path and digest are
  recorded with the recovery, and the same proof is rechecked before a successor dispatches. A
  missing, tampered, foreign or contradictory receipt is a refusal, and no Fleet task is created,
  edited or fabricated for the ownership.
  and it never rewrites task/event/turn history. The adoption pins the exact snapshot:
  authorization and dispatch revalidate the same record and fail closed if it is missing or the
  checkout changed. An adoption ID reused with an altered reference, evidence excerpt, decision,
  or any other field is rejected; an exact replay remains idempotent across restart.
- `bin/fleet cancel TASK --note "..."` — abort a sent/running turn (Pi `abort`, Claude process
  group SIGTERM); the task becomes `cancelled` and is never resent.
- `bin/fleet hold TASK --note "..."` / `bin/fleet release TASK --note "..."` — pause and resume one
  **unstarted queued** task without cancelling it or changing its ID. The hold, its reason, and the
  audit trail are durable in `fleet.sqlite`, so a manager restart preserves them and dispatch never
  consumes a held task. Repeat operations are idempotent. Holding or releasing a started
  (`dispatching`/`sent`/`running`) or terminal task is refused. `release` clears only this
  task-level hold: it never clears the global pause, a per-provider/worker `dispatchEnabled` hold,
  a capacity/quota gate, or a durable single-owner/recovery reservation.
- `bin/fleet add-pi ...` / `add-claude ...` / `remove SID` — roster edits, validated (cwd must be a
  git worktree under `worktreeRoot`, IDs unique, nothing in flight on removal).

## Rules
1. Never create, replace or duplicate worker sessions; identities live in
   `config/fleet.local.json` (one entry per session).
2. Never resend a task in state `uncertain`; inspect the worker and enqueue a new task ID.
3. Acknowledge an event only after you have acted on it; unacked events persist across restarts.
4. Don't edit `data/` by hand. Stop the fleet and run `scripts/db-check` before copying or moving it.
5. Don't touch credentials, repository history, or `~/Developer/.codex/archive` (historical only).

See README.md for layout and docs/ for architecture, configuration and troubleshooting.

## Pull request lifecycle
- Open every PR in draft. Do not mark it ready until its agreed implementation scope is complete and root has reviewed the exact resulting code head.
- Do not mark a PR ready merely to trigger CI. Use supported draft CI or an explicit workflow dispatch where available; do not alter workflow gates or branch protections.
- After root review approves the exact head, wait for every applicable test, build, lint, typecheck and integration check on that head to pass before enabling squash auto-merge. A relevant failing check blocks our merge even when GitHub does not require it. A skipped check is acceptable only when its documented applicability condition excludes this change; cancelled or missing applicable validation is not a pass.
- Preserve all existing branch protections and review rules. If auto-merge is already enabled and a relevant non-required check fails, disable auto-merge promptly, report the failed job, and obtain root review of the repair before restoring eligibility.
- If CI fails, assign the repair immediately to the current owner or an existing fleet worker, keep auto-merge enabled, and push the fix to the same PR branch. If the head changes after root review, return it to root for review before it remains eligible to merge.
- Keep task handoffs consistent with these rules. Do not create sessions for repairs unless separately authorized.

## Heavy validation scheduling
- Keep existing sessions and issue ownership. Before starting a full monorepo build, full unit/integration/coverage suite, browser suite, or another CPU-heavy validation command, run it through `python3 /Users/amf/Developer/.codex/fleet/bin/fleet-heavy -- COMMAND ARGS...` from the intended worktree. The wrapper allows two heavy jobs at once across repositories and preserves command exit status, cwd and existing test timeouts.
- A waiting slot is scheduling, not a test failure. Do not raise assertion/test timeouts, bypass the wrapper, or repeatedly rerun unchanged suites. Focused small tests and source editing may continue normally.
- Do not nest wrappers. Do not delete lock files or terminate another worker's processes. Existing pre-wrapper tests are allowed to finish. A child inherits its lease so a crashed wrapper cannot silently free a still-running job's slot.

For a root-reviewed PR with auto-merge enabled, gh --match-head-commit verifies the head when the command runs; it is not a permanent pin on future commits. Do not push an unreviewed source repair to such a PR. Prepare and test the repair locally, return the immutable commit SHA plus patch to root for review, then push only after root approves that commit. Keep auto-merge enabled and required checks intact. Do not self-approve, weaken checks or rely on an earlier head approval. Unrelated draft implementation branches may continue their authorized normal pushes.
