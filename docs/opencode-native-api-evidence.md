# Native session creation — evidence from the installed OpenCode CLI, and its limits

The Fleet creates a native OpenCode session only through the supported CLI API. This note is the
evidence that the call the Fleet makes exists, what shape it has, and what is still unproven.

## The installed CLI

| Item | Value |
| --- | --- |
| Executable | `/Users/amf/.local/bin/opencode` (`@opencode/cli`, `@opencode/cli-darwin-arm64`) |
| Version | `opencode v2.0.29` |
| Request surface | `opencode api --help`: *"Make a request to the running server … OpenAPI operation ID, or an HTTP method followed by a path"*, with `--standalone`, `--data` and `--header` |
| Session subcommands | `opencode session --help`: `list`, `delete`, `export`, `import` — **no CLI session-create command**, so the API is the only supported creation path |
| The CLI's own schema | `opencode api --standalone GET /openapi.json` serves the **OpenAPI 3.1.0** document, title `opencode HttpApi`, 276,536 bytes, **sha256 `49c38e1c0a6c0e8c91ab5fbeaddd068effc543bdecf96484caf4eb956218a1c7`** |

The probe is read-only and isolated: an isolated `HOME` and a scratch `OPENCODE_DB`, so the real native
account is never opened and **no session is created** (the scratch database ends with
`session_v2: 0 rows`).

## The resolved request contract (and the correction it forced)

From the schema the installed CLI serves itself, resolved:

```
POST /api/session        operationId: session.create
summary:     Create session
description: Create a session at the requested location.
security:    []
body: type object, required: [], additionalProperties: false
  id        : Session.ID | null
  parentID  : Session.ID | null
  title     : string | null
  agent     : Agent.ID | null
  model     : Model.Ref | null
  location  : Location.PublicRef | null
  metadata  : Session.Metadata | null
  permissions: Permission.Ruleset | null

Model.Ref          = { type: object, properties: { id: string, providerID: Provider.ID,
                       variant?: string }, required: [id, providerID], additionalProperties: false }
Location.PublicRef = { type: object, properties: { directory: AbsolutePath },
                       required: [directory], additionalProperties: false }
```

**The model is an object, not a `"provider/model"` string.** The first implementation sent the string
form, which the resolved schema does not accept. The request is now:

```json
{"location": {"directory": "<the worktree>"},
 "model": {"id": "minimax-m3.1-flash-free", "providerID": "opencode-go"}}
```

The command's schema gate refuses to create anything unless the installed CLI declares exactly this
shape: the operation, the closed body, the `Model.Ref` object with a required `id` and `providerID`, and
the `Location.PublicRef` object with a required `directory`. The synthetic CLI in the test suite serves
the same resolved schema and **validates the request against it**, so a payload the schema does not
declare is refused there as well.

## What the native code shows about inference

- The native route table in the installed binary declares the create payload as
  `{id, parentID, title, agent, model, location, metadata, permissions}` — **no prompt, text, message or
  content field** — and no security scheme.
- The native `session.created` event records `{sessionID, projectID, location, subpath, parentID, slug,
  title, agent, model, metadata, permissions, version}`: the created session holds a **model reference**,
  not content, and the native insert path stores that reference.
- The operation that performs inference is a **different route**: `POST /api/session/{sessionID}/prompt`
  ("Send message"), whose body carries `text`, `files`, `agents` and `skills`. The Fleet never calls it.

**The precise limitation.** Endpoint existence, `security: []` and the absence of a prompt field are
*contract* evidence, not proof that the compiled handler performs no inference. The handler body is
minified inside the installed binary and the upstream v2 server route is not exposed in the public tree
at this version, so the handler could not be read line by line, and **no session was created to test it
behaviourally** (root's boundary for this task). What is established is that the request the Fleet
sends carries a directory and a model reference and nothing else, so there is nothing in it that could
carry an inference, and the inference operation is a separate route. **The remaining activation
precondition is explicit: root's first live request confirms the property on the account**, and the
command's schema gate is what keeps the request inside the documented contract until then.

## The pinned live manager

The live fleet runs the pinned installation
`~/.codex/archive/2026-10-20/task-2/installations/fleet-3cd7c49efdee`. `tests/test_pinned_manager.py`
runs that exact code in a temporary root with a synthetic configuration and establishes:

| Question | Answer | Evidence |
| --- | --- | --- |
| Is a new roster entry picked up without stopping or replacing the manager? | **Yes** | the pinned manager is started in a temporary root, a second OpenCode entry is added to the configuration, and the pinned manager's own status file lists it — the same process, no restart |
| Is the admission limit re-read? | **Yes** | the pinned manager passes `int(paths.config().get("maxConcurrent", 0))` on every cycle; the test changes the limit between two cycles and observes the change |
| Does the pinned manager honour the `draining` flag? | **No** | the pinned dispatch has no `draining` handling: a drain is **advisory** against it, so the fleet must be paused for the drain window, or the manager upgraded |
| Do the receipt tables disturb the pinned manager? | **No** | the pinned store does not mention them; they are created with `CREATE TABLE IF NOT EXISTS` and are additive, so the pinned manager keeps running against the same file |
| Is the heavy-job admission the agent count? | **No** | the pinned `heavy.py` has `SLOT_COUNT = 2` and never reads `maxConcurrent`: heavy-job admission, the logical session count and the admission limit are three separate numbers |

So the roster and the admission limit can be reloaded safely under the pinned manager, and only the
*drain* needs the fleet paused (or the manager upgraded). The plan in
[opencode-lifecycle-rollout.md](opencode-lifecycle-rollout.md) says exactly that, and does not claim
drain or removal support the pinned manager does not have.
