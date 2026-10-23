# Configuration
`config/fleet.local.json` (ignored) — see `config/fleet.example.json`.
- `worktreeRoot`: canonical worktrees (`~/Developer/.worktrees/<repo>/<name>`). Disposable
  per-worktree artifacts belong in `<worktree>/.fleet/` so pruning the worktree removes them.
- `executables`: names or absolute paths for `pi`, `claude`, and `opencode`; otherwise resolved via PATH.
- `logs`: `maxBytes`, `keep` (rotated files), `claudeTurnsPerSession` (turn log dirs kept).
- `claudeTurnTimeoutSeconds` (per worker: `turnTimeoutSeconds`): terminate a managed turn after
  this long; 0 disables.
- `maxConcurrent`: cap on tasks in flight across the fleet; 0 = unlimited.
- `pi[]`: `lane`, `sessionId`, `cwd`, `sessionFile`, `sessionCwd` (cwd stored in the session
  header, kept for identity checks after relocation), `provider`, `model`, `thinking`, optional
  `apiProvider` (live provider/model pin, no fallback) and `role`.
- `claude[]`: `sessionId`, `cwd`, optional `sessionCwd`, `model`, `effort`, `mode` (`print`),
  `resume`, `dispatchEnabled`.
- `opencode[]`: an existing native `sessionId` (`ses_…`), `cwd`, independently verified
  `sessionCwd` (must resolve to the same directory), `model` (exactly
  `opencode/deepseek-v4.1-flash-free`), and `dispatchEnabled` (defaults false). Dispatch also
  requires `nativeSessionVerified`, `sessionExclusiveVerified`, `auxiliaryModelsVerified`, and
  `freeCanaryVerified` all set to `true` after root review. Agent overrides are refused.
  OpenCode turns pass `--session` and the exact `--model`; Fleet does not create sessions or select
  a fallback model. `doctor` checks the configured identity fields and executable, but native
  session existence, its saved directory, idle/exclusive use, merged auxiliary model settings, and
  a successful exact-model canary must be verified before dispatch is enabled. The v2.0.5 JSON
  stream does not include actual provider/model IDs, so receipts leave them blank and record the
  pinned CLI model separately. Keep tasks within the verified worktree scope; do not run or open a
  rostered session outside Fleet.
Changing a running lane's model/provider/cwd makes the manager report it `unavailable` until the
controller is restarted (`bin/fleet stop` / `start`).
