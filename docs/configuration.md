# Configuration
`config/fleet.local.json` (ignored) — see `config/fleet.example.json`.
- `worktreeRoot`: canonical worktrees (`~/Developer/.worktrees/<repo>/<name>`). Disposable
  per-worktree artifacts belong in `<worktree>/.fleet/` so pruning the worktree removes them.
- `executables`: names or absolute paths for `pi`, `claude`; otherwise resolved via PATH.
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

