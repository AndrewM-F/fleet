# Disposable OpenCode jobs

The new source entry point is `bin/fleet disposable-opencode`. Its queue exists
only for the lifetime of that command. It accepts new root-authorized jobs, caps
admission at three, creates a new native session for each admitted job, records
one terminal result, and releases the slot only after verified execution stop
and native session retirement. It never loads the old queue, roster, task ledger,
session history or recovery state. No sessions or results are archived by this
route. The old manager does not consume these jobs.

**Live operation is blocked.** The source currently ships no enforcing production
launcher (`credential_isolation._BACKENDS` is empty), no qualified disposable
native driver, and no verified current account/model quota eligibility. The
default executor refuses before accepting jobs or making any native call. JSON
configuration cannot assert these proofs, load a plugin or supply a shell command
to bypass them. The offline engine is qualified; a working native fleet has not
been established.

## Inspect without launching anything

Supply a separate configuration file with exactly these fields. Replace the
account placeholder with the verified opaque, non-secret account binding and the
cwd with its canonical path. The actual account binding remains unverified; no
credential-file path or account database is inferred. Do not change the signed-in
account. Resolve the executable
symlink to its canonical target before putting it in this file.

```json
{
  "executable": "/canonical/path/to/current/opencode.exe",
  "account": "verified-existing-account-binding",
  "cwd": "/canonical/path/to/owned/worktree",
  "model": "opencode/mimo-v2.6-flash-free",
  "maxConcurrent": 3
}
```

```sh
bin/fleet disposable-opencode --config /absolute/path/to/disposable.json --inspect
```

This command reads only the supplied file and returns `ready: false` with the
specific activation gaps. It does not create `data/`, open an account, run the
native CLI, probe quota, start a server, or create a native session. The cap and
model come from explicit authorization, not the legacy `maxConcurrent: 10`, a
model listing, or the old eight-session MiniMax roster.

## New-job command protocol

Once the missing source-reviewed native driver and proofs are established, the
same supported command without `--inspect` consumes one JSON request per input
line and writes JSON responses:

```json
{"enqueue":{"id":"new-job-1","message":"Implement the authorized task","rootAuthorized":true}}
{"status":true}
{"cancel":"new-job-1"}
```

It currently refuses with exit 2 before reading those requests. The lifecycle
supports at most 32 waiting jobs. IDs are idempotent within this process; changing
the message for an existing ID is refused. A duplicate terminal ID never starts
again. `sessionId`, `previousTaskId`, `recoveryId`, lane and resume fields are
refused. Normal EOF drains all accepted jobs through completion and retirement,
including jobs waiting for a slot. Explicit cancellation or keyboard interrupt
cancels queued jobs and requests cancellation of active jobs before retirement.
After draining, the command emits one `{"terminal": ...}` response per accepted
job and its final queue status. There is no restart
recovery: after process exit, callers must not replay jobs whose outcomes they
do not know.

A creation whose result is ambiguous, an unverified execution stop, a returned
session identity that was already used, or a retirement failure publishes one
failed result and closes further admission. Waiting jobs receive terminal
admission failures without being started, so a retirement failure does not strand
the remaining input.
A failed retirement retains the
slot and exact native session ID in the process-local result. It never silently
retries deletion, resumes execution, clears a hold, or treats CLI exit as proof
that native execution and descendants stopped. A known failed job whose native
execution is verified stopped still retires normally and releases its slot.

## Native evidence and remaining handoff

Read-only help inspected on 2026-10-24 from
`/Users/amf/.bun/bin/opencode` (`--version` reports 2.1.3) declares:

- `run --standalone --format json --model PROVIDER/MODEL`.
- `run --session SID` continues that session or creates it if absent. This flag
  alone cannot prove a new session and must not become a resume shortcut.
- `session delete --standalone SESSION_ID` deletes the session and child sessions.

Read-only inspection of the installed v2.1.3 executable's embedded route source
also identifies `session.create` at `/api/session`, `session.remove` at
`/api/session/:sessionID`, and a separate `session.prompt` route that durably
admits input and schedules execution. The noninteractive runner consumes
`session.execution.succeeded`, `session.execution.failed` and
`session.execution.interrupted`; it treats a disconnected event stream during
execution as an error. These are contract evidence, not live completion or
retirement qualification. In particular, a `step_finish` output or CLI exit alone
does not prove containment cleanup.

Help and embedded route source do not establish stop/descendant proof,
deletion absence proof, a fully resolved current create schema, or eligibility. No
native session, private server, scratch account, inference or deletion was run
for this qualification. Older create-API evidence in
`opencode-native-api-evidence.md` describes v2.0.29 and is not current proof for
v2.1.3.

### Exact enforcing-launcher dependency

`src/fleet/credential_isolation.py` defines contract version 2. Its production
backend registry is an empty immutable mapping. The contract does not accept a
configuration Boolean or worker assertion as proof. A supported backend must
provide all of these methods:

| Method | Required result |
| --- | --- |
| `attest(binding, stage)` | Authenticated `Evidence` for `pre-prompt` or `launch` |
| `verify(evidence, binding, stage, pid)` | Independently authenticate the backend proof against its trust anchor |
| `launch(binding, args, **kwargs)` | Exact bound process, stable containment-domain ID, authenticated `process` evidence before prompt delivery |
| `abort(process, binding, domain)` | Stop/reap the entire domain and return authenticated `cleanup` evidence proving it empty |

Every proof binds contract/backend/policy versions, policy and binding digests,
stage, PID and domain where applicable, and an issue/expiry interval no longer
than 30 seconds. Required denials cover host credential stores, keychain,
credential-helper IPC, inherited credential descriptors, credential environment
and descendant escape. Its binding also requires an explicit canonical
`databasePath` and a native `sessionId`, alongside executable measurement,
cwd/sessionCwd, runtime/log allowlists and task/turn/launch identities. An opaque
account binding is not a database path; a fresh-session creation does not yet
have a native session ID. Those mappings cannot be guessed to satisfy this API.

The existing host has `sandbox-exec`, whose installed manual marks it deprecated
and documents sandbox profile execution. Its presence does not establish any of
the authenticated domain proof methods above. A plain process group cannot
certify domain-wide cleanup, as the current contract explicitly states. No
supported attestation broker or enforcing native driver is registered. This is
a specific missing implementation and qualification, not proof that macOS could
never support one.

A usable native route therefore needs a source-reviewed enforcing launcher or
broker that implements this contract, plus a verified mapping to the preserved
signed-in account and current eligibility. Native CLI API support alone cannot
provide that enforcement. No backend, trust proof, account path, credential or
policy exception was fabricated during this work.

The source-reviewed native driver must bind every create and productive call to
the exact existing account, executable, cwd, model and authorized cap; obtain
current authenticated eligibility without quota probing; preserve the enforcing
launcher gate; prove the current native create/completion/delete contract;
own a single account-wide admission lease across route processes; return only
fresh native IDs; prove stop before deletion; and confirm deletion of only that
job's session before releasing its slot. Configuration assertions and worker
stdout cannot supply these proofs. Live activation requires root review of the
complete resulting source head and the separately authorized runtime handoff.

The OpenCode Control task owns old runtime, queue and history cleanup separately.
This change does not install source into the archived installation, edit runtime
metadata, stop workers, switch credentials, clear quota/dispatch holds or restart
the native application.

## Offline qualification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_disposable -v
```

The tests use an in-memory executor with deterministic create/run/retire
boundaries. They establish lifecycle ownership and safe refusals, not native
provider or account behavior.
