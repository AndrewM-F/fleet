# Disposable OpenCode jobs

`bin/fleet disposable-opencode` runs a process-local queue and creates a fresh
native OpenCode session for every accepted job. It admits at most three jobs,
publishes one terminal result, and deletes each successfully completed session
before releasing its slot. Quota-stopped or ambiguous sessions are preserved and
close admission for operator review. A per-account OS lock prevents a second manager process from exceeding
the cap. The queue disappears at exit; this route never loads legacy roster,
queue, task-ledger, recovery or session-history state.

## Local execution boundary

The disposable route uses the signed-in local OpenCode service through the
supported `opencode api` CLI. Fleet does not read, copy or store OpenCode
credentials. It pins each session to the configured provider and exact model,
uses OpenCode's existing permission behavior, interrupts through the native API,
and confirms that a session and its native child sessions are inactive before deleting them. Fleet never passes `--auto`, changes account identity or permission settings, or
switches an unowned session. The only authorized fallback is free MiMo Flash to
the already available Go MiMo Flash in the same owned native session.

This route relies on the local service's normal same-user trust boundary. It is
not an OS credential sandbox: OpenCode tools retain the filesystem and process
permissions of the signed-in local service, including access beyond the job's
worktree. Use only root-authorized task text and keep OpenCode's normal
permission controls enabled. The legacy persistent-session manager retains its
separate credential-isolation gate.

Inspection checks that the executable and local service are available, that the
configured provider is present, and that the exact model appears in the local
model list. It does not make an inference or check quota. A provider-side quota
error is not automatically proof of quota exhaustion. Fleet never probes quota
in advance. Only separate verified quota evidence permits the approved fallback;
generic 429, authentication and network failures do not.

## Configuration

Supply a separate configuration file with these required fields. `account` is
the OpenCode provider ID, not a credential. `cwd` is the canonical root that
contains the job worktrees. Each enqueue names its own canonical Git worktree
under that root. Resolve the executable symlink to its canonical target.

```json
{
  "executable": "/canonical/path/to/current/opencode",
  "account": "opencode",
  "cwd": "/Users/amf/Developer/.worktrees",
  "model": "opencode/mimo-v2.6-flash-free",
  "maxConcurrent": 3
}
```

## Authorized free-to-Go fallback

Opt in with `"fallbackModel": "opencode-go/mimo-v2.6-flash"` and a canonical
`"quotaEvidencePath": "/absolute/private/quota.json"`. Both exact live catalog
models must be present. The existing Go provider has ID `opencode-go` but
integration ID `opencode`; it is provider selection on the existing integration,
not a new credential/account. All other models and caps above three are refused.
A direct Go config (`account: opencode-go`, `model: opencode-go/mimo-v2.6-flash`,
no fallbackModel) also requires the verified free hold before dispatch.

The evidence file is separate from job/session state and contains no credentials
or raw provider bodies. For the current root-verified free hold, its shape is:

```json
{
  "version": 1,
  "holds": {
    "opencode/mimo-v2.6-flash-free": {
      "verified": true,
      "evidence": "Root verified free usage exhaustion; exact receipt locator and statement"
    }
  },
  "signatures": []
}
```

Do not fabricate an observation or clear a hold to test availability. Keep the
actual receipt and its provenance until separately verified reset/expiry
justifies an operator update. The wrapper sends no free prompt while held: a new owned session selects Go
before its single original productive prompt. No quota
probe or failed free request is needed for this admission.

For exhaustion observed during a productive free turn, an operator-verified
native signature may be retained in `signatures`: `model`, `verified: true`,
substantive `evidence`, exact native `type`, `status` (402 or 429), and literal
`messagePrefix` (12–512 characters). Native v2 has no quota enum, so this patch
ships **no guessed provider signatures**. A generic 429, retry delay, quota-sounding
message without a verified matching signature, auth or network error cannot
select paid. Unknown failures retain the owned session and stop for review.

On a verified match, the wrapper records a durable free hold, confirms the whole
native session tree is inactive, uses `session.switchModel` at
`POST /api/session/{sessionID}/model`, then reads back the exact ID, directory and
model. Native `Model.Ref` may omit `variant` or materialize `variant: "default"`
for that same unqualified model selection; switch readback accepts only those two
shapes, with exact provider/model IDs and no extra model keys. Other variants,
session IDs and directories remain refused. The installed v2 `/prompt` schema
has no model field; model selection is explicit before the paid turn through
that supported route. It sends one
stably identified continuation in the **same** session, asking the worker
to inspect existing work rather than replaying the original task. The original
prompt is never resent. The failed free assistant ID is fenced out while waiting
for the new paid result. No fork, rehome, new session or credential change occurs.

Any paid failure stops without another provider request. Verified paid exhaustion
is recorded separately from the free hold; an unclassified paid failure is marked
`paidFailure`, not mislabeled verified quota. The native session and partial work
are retained, admission closes, and future paid admission refuses until operator
review. Paid deadline expiry records `paidFailure.kind: timeout` before awaiting
native interruption; context is retained after confirmed inactivity and also
when stop remains unverified. Timeout is not mislabeled verified quota. Operator
cancellation remains distinct and follows normal verified-cancellation retirement
without creating a paid-failure hold. Malformed evidence, uncertain prompt/model-update outcomes, cancellation,
changed session identity and failed retirement all fail closed. Never restart the
manager to bypass a hold or its leftover-session guard. Controllers remain
status/relay only; implementation runs only as the explicitly authorized job.

The offline fake-backend tests prove lifecycle and no-replay controls; they do not
claim a live free-to-paid quota transition or certify a provider signature.

```sh
bin/fleet disposable-opencode --config /absolute/path/to/disposable.json --inspect
```

Inspection uses `opencode models`, the read-only provider API, and a title-only
search for leftover disposable sessions. It does not read prompts, start a
server, create a session, or probe quota. Leftover disposable sessions after a
manager crash block restart until an operator reviews them; Fleet never adopts
or deletes an uncertain session automatically.

## Foreground manager protocol

Start the command in a local terminal; it stays attached to its JSON-lines input
until stopped. Each enqueue must use a distinct worktree while another job is
queued or active:

```json
{"enqueue":{"id":"job-1","message":"Implement the authorized task","rootAuthorized":true,"cwd":"/Users/amf/Developer/.worktrees/job-one"}}
{"status":true}
{"cancel":"job-1"}
{"stop":true}
```

EOF drains accepted work. `stop` or keyboard interrupt cancels queued jobs and
interrupts active sessions before retirement. The command emits one terminal
result per accepted job, then final queue status. IDs are idempotent within the
process; changing the message or worktree for an existing ID is refused. Session,
recovery, lane and resume fields are not accepted. There is no restart recovery;
do not replay jobs whose outcomes are unknown.

An ambiguous create, unverified inactive state, reused session ID, or failed
retirement closes further admission. The failed result retains the exact session
ID when known. Fleet never retries a create/delete or resumes a session. A
confirmed inactive ordinary failed job is retired normally. Paid/quota-policy
stops preserve the session and block admission; they do not release a false clean
slot or authorize replay.

## Native API surface

The installed OpenCode v2.1.3 API declares `session.create` at
`POST /api/session`, `session.prompt`, `session.interrupt`, `session.active`,
`session.message.list`, `session.remove`, `session.get`, and `session.switchModel`. Fleet uses the
active-session endpoint to wait for inactivity and the get endpoint to confirm
that deletion removed the exact native session. This is service lifecycle
evidence; it is not OS descendant-containment evidence.

No live model request or quota probe is part of configuration inspection. The
offline lifecycle tests use an in-memory fake executor and do not establish
provider availability or quota.

## Review-gated Adea #536 one-use exception

`scripts/continue-adea536-once.py` is a separately reviewed one-use helper for
`ses_e8c720ce7ffe00SZeLu9ixDNM8`, whose earlier admission stopped before prompting.
It is not a generic adoption/resume route and has no session or path arguments.
Do not execute it before root approves its exact source and fresh reconciliation.
The normal new-only queue continues to reject preserved sessions.

The helper pins the owned job, clean branch/head, task/config/runtime bytes and
owner record; takes the same account lock; and requires the exact audited
model-switch event with no user/assistant work, empty inbox, zero usage, no
children, no active native work, no other disposable session and no pending
permission. Permission and inbox reads must return exactly empty data; malformed,
pending or unread responses refuse before any prompt. It selects the already
authorized paid model only with the verified
free hold. Exclusive durable attempt and prompt-intent records prevent replay,
including after a crash or ambiguous response. A prompt-accepted receipt records
the actual native inbox ID and implementation owner; intent is not execution.
The reviewed NativeExecutor handles the productive turn and paid stops. Success,
failure, uncertainty and cancellation all preserve this session for review; the
helper never creates, deletes or retires a session, and never clears its latch.


## Reviewed terminal bookkeeping for the completed #536 source slice

The default local checkout may still predate merged Fleet #21. That older
`Config.parse` rejects `fallbackModel` and `quotaEvidencePath`; removing those
fields is not a repair. The verified main implementation accepts the authorized
free-to-existing-Go policy. After root reviews the new exact repair head, advance
the installed tracked checkout through the normal owned fast-forward path,
preserving its untracked configuration and run records. No schedule change is
needed, and the controller remains status-only.

The preserved #536 native turn ended without delivering source; its bounded
source task was subsequently completed in merged Adea #1637. It must not be
resent, resumed, deleted, or treated as a native implementation success.
`scripts/reconcile-adea536-terminal.py` is a narrowly pinned operator review,
not generic old-session adoption. With no argument it only reports the plan;
`--apply` appends one private `terminal-reviewed-no-replay.json` receipt only
**after root personally reviews the exact source and authorizes activation**.
Do not execute the old continuation helper or clear any original latch.

The review uses the same per-account manager lock, exact current configuration
and wrapper hashes, original receipt hashes and reviewed source completion. It
requires the original worktree's clean published head at review time. Native
checks require the exact session/project/directory/model, complete immutable
history and terminal IDs, no children/activity, and exactly empty permissions
and inbox. It performs GET requests only: no prompt, model switch, archive,
rename, DELETE, session creation, quota probe or manager dispatch. Native context
and original latches are preserved. Quota evidence is read and hash-checked,
never cleared or rewritten.

Clean start recognizes only that exact receipt and revalidates the unchanged
native history and identity every time. A missing/malformed/partial receipt,
new native work or any other leftover still blocks admission. The completed
original job ID is refused before dispatch. Receipt creation is exclusive and
fsynced; an interrupted partial file fails closed and is never overwritten.
An exact repeat verifies the existing receipt without changing it.

Offline tests do not establish live route readiness. After reviewed activation,
perform the normal `--inspect` capability/hold/clean-start check. Run a productive
turn only when root has a genuinely eligible singly owned new scope; do not
rerun #536, probe quota or fill slots with busywork. Free remains first, the
verified free hold permits only the authorized Go fallback, paid failure still
stops admission, and the existing maximum of three remains unchanged.
