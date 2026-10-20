# fleet

Local, repo-independent manager that dispatches explicit root-authorized tasks to existing
Pi (RPC), Claude (`--print`), and explicitly rostered OpenCode CLI worker sessions, relays
completions with receipts, and never calls a model in the control path. Agents: read
[AGENTS.md](AGENTS.md).

## Setup
Python 3.11+, `pi` and `claude` on PATH (or set `executables` in config).
```
cp config/fleet.example.json config/fleet.local.json   # then fill in real sessions
bin/fleet doctor
```
`FLEET_ROOT` / `FLEET_CONFIG` override the root/config location; everything else resolves from the root.

## Tests
```
python3 -m unittest discover -s tests -t .
```

## Commands
| command | effect |
|---|---|
| `bin/fleet start [--lane L] [--controllers-only]` | start missing lane controllers + manager (pause unchanged) |
| `bin/fleet pause` / `resume` | gate dispatch (`data/dispatch.paused`) |
| `bin/fleet stop [--force] [--manager-only]` | SIGTERM and verify exit; refuses if a Pi lane is busy |
| `bin/fleet status [--raw]` / `doctor` | state / validation |
| `bin/fleet enqueue JSON` / `control JSON` | queue tasks / enqueue+ack+wait (was `fleet-control.py`) |
| `bin/fleet completions collect\|wait\|ack` | relay events + receipts (was `fleet-events.py`) |
| `bin/fleet resolve …` / `cancel TASK` | operator decisions, abort a running turn |
| `bin/fleet hold TASK` / `release TASK` | durably pause/resume one unstarted queued task (never a cancel; never clears pause/dispatch/profile/recovery holds) |
| `bin/fleet reconcile-completed --task ID --turn ID --worker SID --receipt-root PATH --note "..."` | close a stale `running` row whose managed OpenCode turn provably completed |
| `bin/fleet recovery-snapshot --cwd PATH` / `recover-opencode …` | fingerprint a checkout and authorize one verified OpenCode successor |
| `bin/fleet adopt-ownership …` / `recover-opencode --adoption-id ID` | append immutable ownership evidence for an owner task whose history predates the exact checkout/branch convention; the reference locates a source the operator must verify and the evidence field must contain substantive prose, not a bare URL or checksum |
| `bin/fleet recover-opencode --direct-turn-receipt PATH` | ownership source for a genuine native turn that predates the task ledger: a durable receipt in `data/recovery/` recording the original session, canonical worktree, branch and initial head, proved against the native completed turn and the unchanged checkout |
| `bin/fleet reconcile-recovery --task ID` | release a claimed recovery reservation after verified native stop |
| `bin/fleet add-pi` / `add-claude` / `remove` | roster edits (validated) |
| `bin/fleet supervisor install\|uninstall` | launchd agent running `bin/fleet supervise` |
| `bin/fleet run` / `wait` / `ack --event-id N` | foreground manager / any-event wait (was `fleet.py`) |

## Layout
```
bin/fleet          CLI          src/fleet/   manager, dispatch, events, control, pi, claude, store, paths
config/            example (tracked) + fleet.local.json (ignored)
data/              fleet.sqlite (authoritative), queue.json, status.json, dispatch.paused,
                   receipts/, claude/<sid>/<turn>/ and opencode/<sid>/turns/<turn>/
                   (request/state/result), lanes/<lane>/
                   (commands.jsonl, controller.json, records/), records/ (relays, migration manifest)
logs/              manager/, lanes/<lane>/ (events, stderr, controller), claude/<sid>/<turn>/ — rotated
tests/ docs/ scripts/ (db-check, link-claude-transcripts, rehome-pi-session)
```

## Recovery
1. `bin/fleet pause`, `bin/fleet stop` (verify output shows nothing running).
2. `scripts/db-check` — checkpoint WAL + integrity.
3. `bin/fleet doctor`, then `bin/fleet start` (still paused); check `bin/fleet status`.
4. Review `uncertain` tasks (`fleet resolve --task`), failed workers (`--worker`) and unacked
   events (`--events`); only then `bin/fleet resume`.
After moving worktrees: update `cwd` in config, `scripts/rehome-pi-session --apply`,
`scripts/link-claude-transcripts --apply`, `bin/fleet doctor`.
A crash mid-dispatch marks that task `uncertain` on restart; it is never resent.
An errored OpenCode task remains terminal. After verifying cleanup and preserving its exact
worktree, `recover-opencode` records a one-use successor authorization. The manager dispatches a
separately root-authorized task; it never replays the failed task or changes model/dispatch settings.
See [OpenCode recovery](docs/troubleshooting.md#opencode-error-turn).
