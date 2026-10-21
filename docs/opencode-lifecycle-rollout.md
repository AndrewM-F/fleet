# Staged activation and rollback plan — on-demand OpenCode sessions

This plan is **not executed**. It describes how the on-demand OpenCode session lifecycle is
activated on a live fleet and how it is withdrawn again, for root review.

Three facts about the live deployment shape the plan and are stated here rather than assumed:

- **The running manager is a pinned older build.** It does not know the `draining` flag, so a
  drained worker keeps receiving queued work from it. The drain is therefore *advisory* against
  that manager: the fleet has to be paused for the drain window, or the manager has to be
  upgraded to a build that honours the flag, before a drain can be relied on. Nothing else in this
  plan depends on the manager: the roster and the admission limit are reloaded by the pinned
  manager on every cycle, which `tests/test_pinned_manager.py` proves by running that exact code
  in a temporary root.
- **The live store predates the receipt tables.** `opencode_provisioning` and
  `opencode_sessions` are additive: the new code creates them on the first `database()` call, an
  older manager ignores unknown tables, and no existing row is rewritten. The migration is a
  one-command step and it must run **before** any provisioning request.
- **There is one removal path.** A worker leaves the roster only through
  `bin/fleet remove-opencode`, with its barriers: no queued, in-flight, uncertain,
  awaiting-import or recovery-owned work, and a verifiable native identity. There is no second,
  faster path that skips those barriers, so there is nothing to roll back around: a withdrawn
  request is withdrawn by draining and removing its workers, one by one, with the same proof.

## Stage 0 — root review

Review the exact head, the diff and this plan. Nothing below happens before that.

## Stage 1 — durable state migration (read-only apart from two empty tables)

```
bin/fleet add-opencode --status
```

This creates the two receipt tables in the live `fleet.sqlite` if they are missing, reports the
account, the roster, the admission limit and the receipts, and writes nothing else: no roster
entry, no task, no hold, no session, no native database row. The pinned manager keeps running
against the same file; the extra tables are invisible to it. Confirm afterwards that
`bin/fleet status` still reports the same eight sessions and the same task counts.

## Stage 2 — read-only plan against the live root

```
bin/fleet add-opencode --target-total 25 --model opencode-go/minimax-m3.1-flash-free \
  --cwd <worktree> --root <fleet root> --request-key <key> --dry-run
```

`--target-total` states the size the roster should reach, so one command reaches it: from eight
workers it adds exactly seventeen and the roster ends at twenty-five, not twenty-six. The dry
run writes nothing at all — no receipt, no roster entry, no native state. It verifies the native
identity of a session it would reconcile, and it does **not** consult the installed CLI's own
schema: that qualification belongs to the creation path, which proves the call before the request
is sent. The dry run's report therefore says nothing about whether the creation call is proved
provider-free, and this stage's later account of the dry run says the same thing.

## Stage 3 — the creation path is proved against the installed CLI

The request the Fleet sends is the one the installed CLI's own schema declares: the model as a
`Model.Ref` object with a required `id` and `providerID`, the location as a `Location.PublicRef`
object with a required `directory`, and nothing else. The evidence, the correction it forced and
the limits of that evidence are in
[opencode-native-api-evidence.md](opencode-native-api-evidence.md); the precise remaining
precondition is root's first live request confirming the provider-free property on the account.

The command consults the OpenAPI document the installed OpenCode CLI serves itself
(`opencode api --standalone GET /openapi.json`) before it creates anything. The evidence is
quoted in [opencode-native-api-evidence.md](opencode-native-api-evidence.md): `POST /api/session`
(`session.create`), an all-optional body with no prompt-shaped field and no security scheme, so
the call records the model and the directory and reaches no provider. If the installed CLI does
not declare that operation, **no session is created** and the command refuses.

## Stage 4 — the one bounded request

```
bin/fleet add-opencode --target-total 25 --model opencode-go/minimax-m3.1-flash-free \
  --cwd <worktree> --root <fleet root> --request-key <key> [--max-concurrent N]
```

One request reaches the target size. The request key is bound to the complete request — the
total, the account, the working directory, the model, the dispatch flag and the admission
limit — so a key reused with any of them changed is refused rather than silently reshaped. The
admission limit changes only when it is asked for and only inside the validated limits. The new
workers are dispatch-disabled unless `--enable-dispatch` is given, so a new session can be
canaried before it takes work.

## Stage 5 — an uncertain attempt is reconciled explicitly

If a creation does not answer, the request stops with an **uncertain** state and says so. The
native inventory is not scanned and nothing is adopted: absence and unreadability are never proof
that no session was created. The operator names the native session that was created:

```
bin/fleet add-opencode --target-total 25 … --request-key <key> --reconciled-session ses_…
```

That exact id is verified — id, working directory and model — before it is registered. An
unverified receipt is never ignored: it is reconciled first, and an unresolved one refuses the
request.

## Stage 6 — drain and remove

```
bin/fleet pause                      # the pinned manager would ignore `draining`
bin/fleet drain-opencode --session <sid> --note "..."
# resolve the outstanding work the drain reports, including tasks awaiting import
bin/fleet remove-opencode --session <sid> --note "..."
bin/fleet resume
```

The drain reports the work the worker still owns, including the tasks that are still waiting in
the root-authored queue file; nothing is cancelled, released or dropped. The removal refuses
while a task or turn is queued, in flight or uncertain, while a task is still awaiting import,
while a recovery holds the session, and while the native identity cannot be verified. A worker
that is known to be running is never removed. After the manager is upgraded to a build that
honours `draining`, the pause is no longer needed for the drain window.

## Withdrawing a request

There is no rollback command, because a fast path around the removal barriers is a way to lose
work. A request is withdrawn by draining and removing its workers, one by one, with the same
proof, and the receipts keep the record of what was created.

## What this plan never does

It creates no account, changes no credential, permission, provider or model, clears no hold,
cancels no task, deletes no native session, starts no second manager, and never deploys or
releases anything.

### Follow-up repair qualification

Target-total replays resolve the complete intent key before testing the current
roster size. They retain the durable original requested count and finish an
interrupted receipt set instead of declaring success from the roster alone.
The generic `remove` command routes OpenCode identities, including receipt-owned
identities absent from the roster, through the lifecycle removal barriers.
Failed and uncertain add results retain their JSON recovery evidence and exit
nonzero. An uncertain exit is never permission to retry creation blindly.

CLI dry-run planning upgrades only an in-memory snapshot. A missing data/root
path remains missing, and legacy durable tables are not migrated. Planning
refuses a store with WAL present, including an empty WAL file, rather than
creating/modifying SQLite sidecars or ignoring committed WAL data. It performs
configuration/receipt planning; it does not start the installed CLI to qualify
its native schema. Native API compatibility remains checked before actual
creation. No live native lifecycle or manager activation is qualified by the
synthetic regression suite.
