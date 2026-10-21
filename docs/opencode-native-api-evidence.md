# Native session creation — evidence from the installed OpenCode CLI

The Fleet creates a native OpenCode session only through the supported CLI API. This note is the
evidence that the call the Fleet makes exists, is provider-free, and takes the payload the Fleet
sends. The synthetic tests prove the Fleet's behaviour; this note proves the API.

## The installed CLI

| Item | Value |
| --- | --- |
| Executable | `/Users/amf/.local/bin/opencode` (`@opencode/cli`, `node_modules/@opencode/cli-darwin-arm64`) |
| Version | `opencode v2.0.29` (`opencode --version`) |
| Request surface | `opencode api --help`: *"Make a request to the running server … OpenAPI operation ID, or an HTTP method followed by a path"*, with `--standalone` (*"Run with a private server instead of the background service"*), `--data` and `--header` |
| Session subcommands | `opencode session --help`: `list`, `delete`, `export`, `import` — **there is no CLI session-create command**, so the API is the only supported creation path |
| The CLI's own schema | `opencode api --standalone GET /openapi.json` serves the CLI's **OpenAPI 3.1.0** document, title `opencode HttpApi`, 276,536 bytes, **sha256 `49c38e1c0a6c0e8c91ab5fbeaddd068effc543bdecf96484caf4eb956218a1c7`** |

The probe is read-only and isolated: it runs with a scratch `HOME` and a scratch `OPENCODE_DB`, so the
real native account is never opened and **no session is created** (the scratch database ends with
`session_v2: 0 rows`).

## The creation operation, verbatim from that schema

```
POST /api/session        operationId: session.create
summary:     Create session
description: Create a session at the requested location. A parentID creates a linked child session at its parent's location.
parameters:  []
security:    []
requestBody: application/json
  type: object
  properties:
    id         : Session.ID | null
    parentID   : Session.ID | null
    title      : string | null
    agent      : Agent.ID | null
    model      : Model.Ref | null
    location   : Location.Ref | null
    metadata   : ... | null
    permissions: ... | null
  required: []                       # every field is optional
response 200: { data: Session.Info } (required: ["data"])
```

The referenced schemas:

```
Model.Ref  = { id: string, providerID: Provider.ID, variant?: string }   required: ["id","providerID"]
Session.ID = { type: "string", pattern: "^ses" }
```

## What the Fleet sends

```json
{"location": {"directory": "<the worktree>"}, "model": "<provider>/<model>"}
```

Both fields are declared by the schema, and the session id pattern `^ses` matches the ids the Fleet
accepts (`ses_[A-Za-z0-9_-]+`).

## Why the call is provider-free

- The request body declares **no prompt-shaped field**: there is no `prompt`, `message`, `text`,
  `content` or `input` property, and the Fleet sends none.
- The operation declares **no security scheme** (`security: []`), and the payload carries no
  credential, provider token or account reference.
- The schema's summary and description describe a session record at a location, not a model
  invocation; `model` is a `Model.Ref` that the native server *records* with the session.
- The managed turn path, which is the only path that actually talks to a provider, is a different
  operation (`POST /api/session/{sessionID}/prompt`) and is never used by the provisioning path.

The creation therefore records the model and the directory and contacts no provider: no model call,
no paid resource, no credential access.

## What the Fleet verifies afterwards

The session id the native server returned is read back from the native `session_v2` identity row in
SQLite **read-only** (`PRAGMA query_only=ON`) and three facts are checked: the id exists, its
directory is the requested working directory, and **the model it records is the requested model**.
A returned id that is already in the roster, already owned by another request, or already created
earlier in this request is rejected instead of being registered again.

## Reproducing the probe

```
env HOME=<scratch>/home OPENCODE_DB=<scratch>/data/opencode.db \
  /Users/amf/.local/bin/opencode api --standalone GET /openapi.json > opencode-openapi-2.0.29.json
shasum -a 256 opencode-openapi-2.0.29.json     # 49c38e1c0a6c0e8c91ab5fbeaddd068effc543bdecf96484caf4eb956218a1c7
```

The full schema and the probe script are kept with the operator handoff for this outcome; this note
quotes the parts the provisioning path depends on.
