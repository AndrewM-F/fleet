# Configuration
`config/fleet.local.json` (ignored) — see `config/fleet.example.json`.
- `worktreeRoot`: canonical worktrees (`~/Developer/.worktrees/<repo>/<name>`). Disposable
  per-worktree artifacts belong in `<worktree>/.fleet/` so pruning the worktree removes them.
- `executables`: names or absolute paths for `pi`, `claude`, and `opencode`; otherwise resolved via PATH.
- `logs`: `maxBytes`, `keep` (rotated files), `claudeTurnsPerSession` (turn log dirs kept).
- `claudeTurnTimeoutSeconds` (per worker: `turnTimeoutSeconds`): terminate a managed turn after
  this long; 0 disables.
- `maxConcurrent`: cap on tasks in flight across the fleet; 0 = unlimited.
- `claudePermissionMode`: defaults to `bypassPermissions` as explicitly requested by the fleet owner.
  It is passed as `--permission-mode` on new and resumed print/native turns. New Claude roster
  entries persist that default; per-worker `permissionMode` can explicitly override it.
  Existing running turns retain their launch mode until their next turn. This skips Claude's
  ordinary tool approvals, not outer environment network or execution controls. Use only in
  the authorized isolated environment; task scope and credential/production restrictions remain.
- `pi[]`: `lane`, `sessionId`, `cwd`, `sessionFile`, `sessionCwd` (cwd stored in the session
  header, kept for identity checks after relocation), `provider`, `model`, `thinking`, optional
  `apiProvider` (live provider/model pin, no fallback) and `role`.
- `claude[]`: `sessionId`, `cwd`, optional `sessionCwd`, `model`, `effort`, `mode` (`print`),
  `resume`, `dispatchEnabled`.
- `opencode[]`: an existing native `sessionId` (`ses_…`), absolute `databasePath` for its native
  OpenCode database, `cwd`, `sessionCwd` (must resolve to the same directory), `model` (one of
  `opencode/deepseek-v4.1-flash-free`, `opencode/minimax-m3.1-flash-free`, or
  `opencode-go/minimax-m3.1-flash-free`), and `dispatchEnabled`
  (defaults false). There is no automatic model switch or fallback. Set `databasePath`
  from `opencode debug paths db`; this read-only command does not start a server or open the DB.
  Before each `--session` launch, Fleet reads only that exact `session_v2` identity row in SQLite
  read-only mode and requires its directory to match `cwd`. Unknown IDs fail before the CLI can
  create a session. The configured model must match the native session model unless the operator
  explicitly selected another approved free model for the next turn; task text cannot select it.
  Dispatch uses a per-session lock and an isolated runtime config with `model`,
  `small_model`, and the enabled provider pinned to that model and its provider, so no other
  provider and no paid model can be reached. `provider: "opencode"` remains the Fleet transport
  tag even for the `opencode-go` API provider. Agent overrides are refused. A turn request must
  name its own turn directory, binding the turn to its session, task and CWD. The v2.0.5 JSON
  stream does not include actual provider/model IDs in its normal step
  events, so receipts leave actual IDs blank unless the transport emits direct model metadata and
  record the pinned CLI model separately. Direct stream metadata, when available, and persisted
  native completion metadata must match the requested model and provider. Quota and rate-limit
  errors never select another provider or model.
  The cancellation and recovery paths compare the persisted launch and turn receipts against the
  model configured for that target session, so a roster entry that names a different model is
  validated on its own identity and on its own persisted receipts rather than refused by a
  fleet-wide constant. Every other session, task, turn, CWD, process and cleanup check, and the
  refusal of rate-limit and quota failures, is unchanged.
  A roster entry may also carry `draining: true` (set by `bin/fleet drain-opencode`). A draining
  worker takes no new work: the dispatcher skips it and the queue-reserve alert ignores it, while
  the tasks already bound to it stay queued, held and untouched until an operator resolves them.

## On-demand OpenCode sessions

A native OpenCode session is normally created by the operator and then rostered. When root
authorizes on-demand sessions, `bin/fleet add-opencode` creates them through the supported
OpenCode CLI (`session.create` on `POST /api/session`) and registers them in `opencode[]`. It is
the only path that may create a session: the Fleet never writes OpenCode's internal database and
never invents a session id, so the id in a receipt is always the id the native server returned.

- The request is refused unless the root is this fleet's root, the native account
  (`databasePath`) is the account the roster already uses, the model is the approved free model
  that account's workers are configured with, the working directory is a git worktree under
  `worktreeRoot`, and the count is a whole number inside the validated bound.
- Creating a session contacts no provider: the request carries the pinned model and the
  directory, and the native server records them. No model call, no paid resource and no
  credential access is involved.
- The roster is written once, after every created session was read back from the native identity
  row. A partial failure therefore leaves the roster exactly as it was; the native sessions that
  were created before the failure are kept, recorded in `opencode_sessions` and registered by a
  retry of the same `--request-key` instead of being created a second time. The same request key
  never adds twice.
- New workers are dispatch-disabled unless `--enable-dispatch` is given, so a new session can be
  canaried before it takes work.
- `--max-concurrent` is the only way to change the admission limit, and only inside the
  validated limits: it is never lowered, never raised implicitly, never above twice the logical
  session count and never above the absolute ceiling. The logical session count, the admission
  limit and the heavy-job admission (two slots) are three separate numbers, and this command
  changes only the first two, the second only when it is asked for.
- `bin/fleet drain-opencode` stops dispatching to a worker and reports the work it still owns.
  `bin/fleet remove-opencode` removes it from the roster and refuses while a task or turn is
  queued, in flight or uncertain, while a recovery holds the session, and while the native
  identity cannot be verified. The native session and its history are kept, and no hold, task,
  queue entry, worktree or credential is touched.
- Both the provisioning request and every session carry a durable receipt in
  `opencode_provisioning` and `opencode_sessions` inside `fleet.sqlite`, so ownership and
  idempotency survive a manager restart. The receipt keeps the digest of the complete request, so
  a key reused with a different count, account, working directory, model, dispatch flag or
  admission limit is refused instead of silently doing something else.
- The request key is bound to the request, the roster write is compared with the configuration the
  request started from, and provisioning and roster writes are serialized across processes. A
  concurrent writer is never overwritten and two requests for the same key cannot both create.
- One request may add up to thirty-two sessions, so a whole scale-up (for example eight to
  twenty-five sessions) is one bounded request rather than several manual calls. `--target-total`
  states the size the roster should reach, so that request adds exactly the difference and the
  roster ends at the target instead of overshooting it.
- An uncertain outcome stays uncertain. A creation whose answer never arrived is recorded as
  uncertain, not as an ordinary retryable failure; the intent is recorded before the first native
  call, and the next run reconciles the native identity before it creates anything. A session
  whose receipt says its identity was never verified is never ignored: it is reconciled first, and
  an unresolved one refuses the request.
- There is one removal path. A worker leaves the roster only through
  `bin/fleet remove-opencode`, with its barriers: no queued, in-flight, uncertain or
  awaiting-import work, no recovery claim, and a verifiable native identity. A request is
  withdrawn by draining and removing its workers, one by one; there is no faster path that
  skips the barriers.
- The staged activation and rollback plan is
  [opencode-lifecycle-rollout.md](opencode-lifecycle-rollout.md). It covers the pinned older
  manager, which does not honour `draining`, and the durable state migration that adds the
  receipt tables before any request.

The configured `model` is the desired model for the next productive turn. The native
session row keeps its own historical model: it is read, reported and never written, and a
turn may only run a different approved model when the operator explicitly selected it.
The persisted native completion must still report the model the turn asked for, so a
fallback is refused loudly rather than accepted silently.

`bin/fleet opencode-model` reports or changes that configured model for one worker or
for all of them. It accepts only the explicitly approved free models listed above, and
the legacy default is unchanged. `--inspect` reports the configured model, the native
identity, the working directory and whether the worker is active or uncertain; `--dry-run`
reports the exact change without writing it. A worker that is active or uncertain keeps
the model its in-flight turn was launched with, and the selection becomes the expected
model for its next turn. That in-flight expectation is immutable: it is read from the
turn's own launch record, so repeated selections cannot strand a turn that was launched
before them, and a cancellation binds to it rather than to the next-turn model, so
switching an active worker never makes its running turn uncancellable. A selection and a
dispatch claim share one lock, so a task is never claimed under one model and launched
under another, and the dispatcher re-reads the configuration inside that lock, so a
selection that lands after the roster was loaded is still the model the claim and the
launch use. That reload is fail-closed: the configuration must be readable and must
describe the worker exactly once. An unreadable or malformed configuration, a worker
that was removed from the roster, and an ambiguous configuration are all refusals, so a
retired worker is never launched from a cached roster, an outdated model is never
launched from a configuration that could not be read, and the task stays queued with
the refusal recorded. The selection clears no hold,
dispatch flag, queue, account, session id or working directory, and it never contacts a
provider.

Changing a running lane's model/provider/cwd makes the manager report it `unavailable` until the
controller is restarted (`bin/fleet stop` / `start`).
# Fresh and single-harness installations

Fleet-owned Claude launches set `ENABLE_CLAUDEAI_MCP_SERVERS=false` in the child
environment, for both managed print turns and native resumes. This disables automatic
discovery of account-level claude.ai connectors, which coding tasks do not require.
It does not disconnect the user's account, edit global Claude settings, or affect
ordinary Claude sessions. Explicit project or CLI-supplied MCP servers are not covered
by this opt-out. Existing running processes need a new turn to pick up this setting.
See https://code.claude.com/docs/en/mcp#disable-claude-ai-connectors.

Only configured harnesses are required by `doctor`: a Claude-only roster does not
need Pi installed. The first authorized enqueue creates an empty root-owned queue
when none exists. A missing queue is idle; malformed or non-root queues still fail
closed. Existing state is never replaced during bootstrap.

Managed Claude workers may set `allowedTools` to an explicit array of Claude CLI
tool permission rules. These rules apply to that worker's print turns, including
resumes. Allow rules do not restrict bypass mode; choose an explicit more restrictive
`permissionMode` when relying on an allowlist. This setting is not a sandbox escape
or permission to change credentials or deploy. See https://code.claude.com/docs/en/permission-modes.

Linux wrapper liveness checks verify the turn's request path in the process
arguments, rather than accepting any process that happens to reuse the PID.
After a verified wrapper failure, `resolve --worker` preserves native identity
and allows the next task to resume it.


## Root-reviewed MiniMax Go migration (three sessions)

These are operator steps after root reviews the complete source patch; they are not an activation
script. Keep `data/dispatch.paused` present throughout preparation. Do not edit the live database,
credentials, account routing, permission settings, dispatch exclusions, or existing quota holds.

1. Use the actual OpenCode fleet's `FLEET_ROOT` / `FLEET_CONFIG`, not another manager's config.
   Preserve all existing roster entries and task IDs. Do not repin an in-flight, failed, or
   quota-held DeepSeek session to bypass its hold: turn receipts and cancellation/recovery checks
   deliberately retain their original model identity. Existing task transfers remain excluded.
2. In that config, roster exactly the three separately authorized Go sessions using their verified
   existing native `ses_…` IDs, original `cwd` / `sessionCwd`, and original `databasePath` values.
   Fleet never creates them. If they do not yet exist, creation remains a separate authorized
   controller action after root review. For each of these three entries set
   `model: "opencode-go/minimax-m3.1-flash-free"` and retain `dispatchEnabled: false` during
   preparation. Do not substitute `provider: "opencode-go"` for Fleet's `opencode[]` transport.
3. Set the fleet-level `maxConcurrent` to `3` (it caps all tasks in that fleet, not three per
   provider). Do not enable or release any old worker, provider/model quota hold, task hold, or
   recovery reservation. The two approved MiniMax provider IDs are independent exact pins;
   availability of one never authorizes moving a held task from the other.
4. Use read-only identity/configuration validation (`bin/fleet doctor` in the intended fleet)
   before activation. It does not prove that inference will succeed or that quota is available.
   Verify three distinct native identities, the exact Go pin, unchanged account/database routing,
   no conflicting checkout ownership, and the durable pause still present. Do not read auth data.
5. Return the resulting config diff to root. Only after root's exact patch/config review may the
   authorized controller enable the three approved slots and carry out the separately authorized
   start/dispatch plan. Resume remains a human decision; this repair performs no activation,
   inference, quota probe, hold release, or permission change.
