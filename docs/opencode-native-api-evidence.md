# Native session creation — evidence from the installed OpenCode CLI, and its limits

The Fleet creates a native OpenCode session only through the supported CLI API. This note is the
evidence that the call the Fleet makes exists, what shape it has, and what is still unproven.

## Current installed CLI verification (2026-10-24)

The current binary was checked with an isolated temporary home and database. The request was only
`GET /openapi.json`; the command wrote its output to a regular temporary file so the CLI's
large-response pipe truncation cannot produce misleading evidence. No native session or provider
request was made. The temporary database contained zero rows in `session_v2`.

| Item | Value |
| --- | --- |
| Executable | `/Users/amf/.bun/install/global/node_modules/.bun/@opencode+cli@2.1.3/node_modules/@opencode/cli/bin/opencode.exe` |
| Version | `opencode v2.1.3` |
| OpenAPI bytes | 284,874 |
| OpenAPI SHA-256 | `4554c5dbd2ee9333ca23456833e8c52f14854b2fc39f4c5d332b386ce360adc0` |
| Session create operation | `POST /api/session`, operationId `session.create`, OpenAPI 3.1.0 |
| Create body | Optional `id`, `parentID`, `title`, `agent`, `model`, `location`, `metadata`, `permissions`; no prompt/message/content field |
| Create-operation security | `[]` |

`Model.Ref` remains a closed object with required `id` and `providerID` (optional `variant`);
`Location.PublicRef` remains a closed object with required `directory`. The implementation's
schema gate therefore still matches the installed CLI contract. This is contract evidence only;
it does not prove live account behavior.

## Historical CLI verification (v2.0.29)

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

## Manager lifecycle boundary

The source checkout qualified by the current tests is Fleet `main` at the revision recorded in the
Issue #12 qualification note. The pinned-manager fixture in `tests/test_pinned_manager.py` locates
an explicitly configured or currently running manager and runs it in a temporary root. It is not
evidence for a manager that is not running. At the current check there was no Fleet manager process;
the disposable-root command test and source tests are the applicable offline evidence. The current
source dispatcher honors `draining`; the older pinned dispatcher's behavior remains a historical
compatibility condition, not a claim about a running manager.

| Question | Answer | Evidence |
| --- | --- | --- |
| Is a new roster entry picked up without stopping or replacing the source manager? | **Yes, offline** | `tests/test_pinned_manager.py` exercises roster reload in a disposable root when the exact manager source is available |
| Is the admission limit re-read? | **Yes, offline** | the same fixture changes the limit between cycles and observes the next cycle's value |
| Does current source honor `draining`? | **Yes, offline** | dispatch and stale-snapshot regressions in `tests/test_opencode_lifecycle.py` prove a drained worker is not claimed |
| Do additive receipt tables disturb an older source manager? | **Yes, source-checked** | the pinned store fixture verifies the old store ignores the additive tables |
| Is heavy-job admission the agent count? | **No** | `heavy.py` has its own fixed two-slot limit, independent of `maxConcurrent` |

These results do not establish lifecycle behavior against a live pinned manager. The reversible
activation plan in [opencode-lifecycle-rollout.md](opencode-lifecycle-rollout.md) remains separate
from offline qualification. A live manager must be identified and explicitly activated before
recording native-turn evidence or claiming the live acceptance complete.
