# Disposable OpenCode jobs

`bin/fleet disposable-opencode` runs a process-local queue and creates a fresh
native OpenCode session for every accepted job. It admits at most three jobs,
publishes one terminal result, and deletes each session before releasing its
slot. A per-account OS lock prevents a second manager process from exceeding
the cap. The queue disappears at exit; this route never loads legacy roster,
queue, task-ledger, recovery or session-history state.

## Local execution boundary

The disposable route uses the signed-in local OpenCode service through the
supported `opencode api` CLI. Fleet does not read, copy or store OpenCode
credentials. It pins each session to the configured provider and exact model,
uses OpenCode's existing permission behavior, interrupts through the native API,
and confirms that a session and its native child sessions are inactive before deleting them. Fleet never passes
`--auto` or changes account/model after an error.

This route relies on the local service's normal same-user trust boundary. It is
not an OS credential sandbox: OpenCode tools retain the filesystem and process
permissions of the signed-in local service, including access beyond the job's
worktree. Use only root-authorized task text and keep OpenCode's normal
permission controls enabled. The legacy persistent-session manager retains its
separate credential-isolation gate.

Inspection checks that the executable and local service are available, that the
configured provider is present, and that the exact model appears in the local
model list. It does not make an inference or check quota. A provider-side quota
refusal becomes that job's terminal failure; Fleet never probes quota in advance
or retries on another account or model.

## Configuration

Supply a separate configuration file with exactly these fields. `account` is
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
confirmed inactive failed job is retired normally and releases its slot.

## Native API surface

The installed OpenCode v2.1.3 API declares `session.create` at
`POST /api/session`, `session.prompt`, `session.interrupt`, `session.active`,
`session.message.list`, `session.remove`, and `session.get`. Fleet uses the
active-session endpoint to wait for inactivity and the get endpoint to confirm
that deletion removed the exact native session. This is service lifecycle
evidence; it is not OS descendant-containment evidence.

No live model request or quota probe is part of configuration inspection. The
offline lifecycle tests use an in-memory fake executor and do not establish
provider availability or quota.
