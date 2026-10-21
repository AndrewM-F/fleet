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
  and it never rewrites task/event/turn history. The adoption pins the exact snapshot:
  authorization and dispatch revalidate the same record and fail closed if it is missing or the
  checkout changed. An adoption ID reused with an altered reference, evidence excerpt, decision,
  or any other field is rejected; an exact replay remains idempotent across restart.
  The adoption also proves the native session it names, exactly as the direct-turn receipt
  does: the session row is read from the native database and must still be the session the
  roster names, in the recorded working directory. A native identity that has gone stale (the
  session row is gone, or it points at another directory) is refused, and the adoption records
  nothing. The same proof runs again when the adoption is consumed by `recover-opencode`, so a
  session that went stale between the adoption and the authorization reserves no worker, no
  worktree and no successor.
  A genuine native turn that predates the Fleet task ledger has no owner task row at all. For
  that case `bin/fleet recover-opencode --direct-turn-receipt PATH` is the ownership source
  instead of `--ownership-task` or `--adoption-id`; exactly one ownership source is accepted. The
  receipt is a durable JSON file in the fleet's own `data/recovery/` directory that records the
  original session, the canonical owned worktree, the branch, the initial head, the turn's start
  time and an operator statement. It is proved before authorization: the receipt's session must be
  the recovering worker, the native session row must show that session in the recorded worktree
  with the configured model and the receipt's exact original user and successful idle message IDs,
  with a completed final assistant for that same turn before the failed managed turn, the
  recorded branch must be the checkout's branch, the recorded initial head must be an ancestor of
  the current head, and the current snapshot must be unchanged. The receipt's path and digest are
  recorded with the recovery, and the same proof is rechecked before a successor dispatches. A
  missing, tampered, foreign or contradictory receipt is a refusal, and no Fleet task is created,
  edited or fabricated for the ownership.
- `bin/fleet reconcile-completed --task ID --turn ID --worker SID --receipt-root PATH --note "..."`
  — close one stale `running`/`sent` row whose managed OpenCode turn provably completed. It
  requires the exact task, turn, worker, session and cwd identity, the retained native completion
  receipt (terminal `done`, exit 0, cleanup confirmed), a full recorded native process identity and
  a recorded wrapper PID that are both provably gone, a worker this fleet no longer manages, and no
  competing task for that cwd. An unknown is a refusal: a missing, invalid or unreadable roster, a
  missing, unreadable or corrupt other-root ledger, a receipt without a process identity, and a
  null identity beside a successful completion all refuse. Ownership in another root is matched by
  task id, by session and by canonical cwd, and that root's ledger is fenced with its own write
  lock from the verified query through the current-ledger commit, because a transaction on the
  current ledger does not fence a separate database. The session dispatch lock is held through the
  commit. It records one auditable event with the receipt
  digests, preserves the task body and the retained receipt, and never releases a hold, a pause, a
  dispatch flag, a model or an account. It is not a generic release: an `uncertain` row still needs
  `bin/fleet resolve`, a live turn still needs `bin/fleet cancel`, and dispatch exclusion is
  unchanged.
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
- `bin/fleet add-opencode (--count N | --target-total N) --model PROVIDER/MODEL --cwd PATH --root PATH
  [--database-path PATH] [--request-key KEY] [--enable-dispatch] [--max-concurrent N]
  [--reconciled-session SID] [--dry-run] [--status] [--note "..."]` — create and register OpenCode
  sessions on demand. `--target-total` states the size the roster should reach, so one request
  reaches it exactly; `--count` adds that many. The
  session is created by the supported OpenCode CLI (`session.create` on `POST /api/session`), which
  is the only path that may create a session; the Fleet never writes OpenCode's internal database
  and never invents a session id. Creating a session contacts no provider, so no model call and no
  paid resource is involved. The request is refused unless the root is this fleet's root, the
  account is the native account the roster already uses, the model is the approved free model that
  account's workers are configured with, the working directory is a git worktree under
  `worktreeRoot`, and the count is inside the validated bound. The roster is written once, after
  every created session was read back from the native identity row, so a partial failure leaves the
  roster untouched; the native sessions that were created before a failure are kept, recorded and
  registered by a retry of the same `--request-key` instead of being created twice. The same key
  never adds twice. New workers are dispatch-disabled unless `--enable-dispatch` is given.
  `--max-concurrent` is the only way to change the admission limit, and only inside the validated
  limits; the limit is never raised implicitly, and the heavy-job admission is a separate,
  unchanged bound. The command never starts, stops or replaces a manager.
  The request key is bound to the **complete** request: reusing it with a different count,
  account, working directory, model, dispatch flag or admission limit is refused, never silently
  reshaped. One bounded request covers a whole scale-up (a request may add up to thirty-two
  sessions, so eight to twenty-five is one request of seventeen additions). One writer lock
  serializes the roster, the queue enqueue, the queue import, the model selection and the dispatch
  claim, and every roster write is a compare-and-swap against the configuration the writer started
  from, so a writer that takes the lock is never overwritten; a writer that ignores it is not
  protected. The digest of that configuration is read before the configuration itself, so a change
  that lands between the two is detected at the write and kept, never overwritten. The enqueue and the import read the roster inside that lock, so a roster a concurrent
  removal has already changed is never used to accept a task for a worker that is gone. A creation whose answer never arrived stays **uncertain**: it is not an
  ordinary retryable failure, the intent is recorded before the first native call, and the
  request stops. The native inventory is never scanned and nothing is adopted: absence and
  unreadability are no proof that no session was created, so the operator names the session with
  `--reconciled-session` and that exact id is verified — id, working directory and model — before
  it is registered. A session whose receipt says it was never verified is never ignored: it is
  reconciled first, and an unresolved one refuses the request. An interrupted attempt is only
  recoverable when the exact identities it holds — the sessions it receipted, the ones it recorded
  before its roster write, and the ones the operator named — account for every session the
  request was going to create. A receipt for an earlier creation, or a partial reconciliation,
  leaves a native effect unaccounted, so the request stays uncertain until the exact identities
  are named. A dry run
  writes nothing at all, not even the receipt a reconciliation would have made. The decision for a
  request key is taken under the writer lock, against the row, its payload digest and every session
  receipt as they are there, and the number of sessions to create is counted there as well. A stale
  caller that planned from an earlier snapshot therefore returns idempotently or refuses: it never
  creates an extra session or a subset, never takes over a live creator because that creator has
  receipts, and never overwrites a request another caller completed or left uncertain. An uncertain
  request stays blocked until the exact identities are named and verified. The creation call is proved against
  the installed CLI's own OpenAPI schema before it is made, and a CLI that does not declare the
  resolved shape — the model as a `Model.Ref` object with a required `id` and `providerID`, the
  location as a `Location.PublicRef` object with a required `directory`, and no other property —
  creates nothing. Once the command has launched, a nonzero exit or an unusable returned session id
  is an **ambiguous committed effect**: the request becomes uncertain and is not retried without a
  verified reconciliation. Only a failure **before** the command starts is a definite, retryable
  failure. A retry takes the request's ownership atomically, and a session receipt only ever
  moves forward, in the order `unverified` → `created` → `registered` → `draining` → `removed`:
  a stale write never moves a receipt backwards, and a valid transition — registered to draining,
  draining to removed — is never refused. A registered request is terminal as well: no later write
  moves it back to an earlier state. A command reports what the durable state holds: an idempotent
  result counts exactly the sessions it lists, it never lists a worker the roster does not
  hold, and it names the sessions of the request that are draining, removed or gone. A request
  converges: an attempt that was interrupted — between two of its own receipt writes, or between
  the roster write and the request row — is finished by the next run with the same key, which
  creates no second set of sessions. The admission limit a result reports is the one in effect
  once the request is done, and the roster size it reports is the size the roster ends at. A
  failure that left a native session whose identity was never verified poisons the key, and the
  result says the request is not retryable instead of advertising a retry the next run refuses.
- `bin/fleet drain-opencode --session SID --note "..."` — stop dispatching to one worker and report
  the work it still owns. Nothing is cancelled, released, re-routed or dropped: a queued task that
  is bound to the worker is reported as outstanding, and the worker takes no new work until the
  operator resolves it. A manager that predates the draining flag keeps dispatching to the
  worker, so the drain is advisory against it: pause the fleet or upgrade the manager.
- `bin/fleet remove-opencode --session SID --note "..."` — remove one worker from the roster. It
  refuses while a task or turn is queued, in flight or uncertain, while a task is still waiting to
  be imported from the root-authored queue, while a recovery holds the session, and while the
  native identity cannot be verified. A root-authored queue file that is missing is no queue, but a
  queue file that exists and is empty, whitespace, truncated, malformed, without a task list or
  without the root author is not evidence of no work: the removal refuses, because the work in it
  is unknown. A queue entry that is already in the task ledger was imported: it is history, and it
  does not block a removal. A worker whose state explicitly says it is running is never
  removed, and `--allow-unverified-native` does not override that: it only covers a native identity
  that cannot be read at all. The native session and its history are kept, and no hold, task, queue
  entry, worktree or credential is touched. There is no second removal path: a receipt-owned worker
  leaves the roster only through this command, with these barriers. The removal also converges: if
  it was interrupted between the roster write and the receipt write, the record and the roster
  disagree, and repeating the command finishes the receipt with the same barriers, read from the
  identity the receipt recorded, so the record and the roster agree again.
- The staged activation and rollback plan for a live fleet, including the pinned older manager and
  the durable state migration, is [docs/opencode-lifecycle-rollout.md](docs/opencode-lifecycle-rollout.md).
  It is not executed by this command. The pinned manager reloads the roster and the admission limit on
  every cycle, which `tests/test_pinned_manager.py` proves against that exact code, but it does not
  honour `draining`: the fleet is paused for the drain window, or the manager is upgraded first.
- `bin/fleet opencode-model --inspect` / `--model PROVIDER/MODEL --worker SID|--all [--dry-run]` —
  report or set the approved OpenCode provider/model for managed workers. The model must
  be one of the explicitly approved free models; the legacy default is unchanged. The
  configuration is the desired model for the next productive turn: the native session row
  keeps its own historical model and is never written, and no provider is contacted. A
  worker that is active or uncertain keeps the model its in-flight turn was launched with;
  that expectation is immutable, so repeated selections cannot strand the turn and a
  cancellation binds to it rather than to the next-turn model.
  A selection and a dispatch claim share one lock, so a task is never claimed under one
  model and launched under another; the dispatcher re-reads the configuration inside that
  lock, so a selection that lands after the roster was loaded is still the model the
  claim and the launch use. That reload is fail-closed: an unreadable or malformed
  configuration, a removed worker and an ambiguous configuration are refusals, so a
  retired worker is never launched from a cached roster and the task stays queued.
  The selection clears no hold, dispatch flag, queue,
  account, session id or working directory.

## Rules
1. Never create, replace or duplicate worker sessions outside `bin/fleet add-opencode`; identities
   live in `config/fleet.local.json` (one entry per session), and a session id always comes from
   the native OpenCode server, never from a guess.
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
