# Staged activation and rollback plan — on-demand OpenCode sessions

This plan is **not executed**. It describes how the on-demand OpenCode session lifecycle is
activated on a live fleet and how it is withdrawn again, for root review.

Two facts about the live deployment shape the plan and are stated here rather than assumed:

- **The running manager is a pinned older build.** It does not know the `draining` flag, so a
  drained worker keeps receiving queued work from it. The drain is therefore *advisory* against
  that manager: the fleet has to be paused for the drain window, or the manager has to be
  upgraded to a build that honours the flag, before a drain can be relied on.
- **The live store predates the receipt tables.** `opencode_provisioning` and
  `opencode_sessions` are additive: the new code creates them on the first `database()` call, an
  older manager ignores unknown tables, and no existing row is rewritten. The migration is a
  one-command step and it must run **before** any provisioning request.

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
bin/fleet add-opencode --count 17 --model opencode-go/minimax-m3.1-flash-free \
  --cwd <worktree> --root <fleet root> --request-key <key> --dry-run
```

The dry run writes nothing and creates nothing. It confirms the account, the model, the working
directory, the number of sessions that would be added and the admission limit that would apply.

## Stage 3 — canary (one session, dispatch disabled)

```
bin/fleet add-opencode --count 1 --model opencode-go/minimax-m3.1-flash-free \
  --cwd <worktree> --root <fleet root> --request-key <canary-key>
```

The new worker is added with `dispatchEnabled: false`, so it takes no work. Verify:

- the native session exists in the account and its identity row names the intended directory and
  the approved model (`bin/fleet doctor` reports it);
- the roster grew by exactly one entry and the admission limit did not change;
- the manager, on its next cycle, reports the new worker as idle and dispatchable only after the
  flag is set.

## Stage 4 — dispatch enablement (explicit, manual)

No command owns the roster's `dispatchEnabled` flag. Enabling the canary is an explicit edit of
`config/fleet.local.json` for that one worker, after root confirms the canary's native identity
and model. This is the one activation step that is not a Fleet command, and it stays that way
until a dedicated command is authorized.

## Stage 5 — scale in one bounded request

```
bin/fleet add-opencode --count 17 --model opencode-go/minimax-m3.1-flash-free \
  --cwd <worktree> --root <fleet root> --request-key <scale-key> [--max-concurrent N]
```

One bounded request covers the whole scale-up (eight to twenty-five sessions is seventeen
additions, inside the bound of thirty-two per request). The request key is bound to the complete
request: reusing it with a different count, account, working directory, model, dispatch flag or
admission limit is refused, so a scale step is never silently reshaped. The admission limit is
changed only when it is asked for and only inside the validated limits.

## Stage 6 — drain and remove

```
bin/fleet pause                      # the pinned manager would ignore `draining`
bin/fleet drain-opencode --session <sid> --note "..."
# resolve the outstanding work the drain reports
bin/fleet remove-opencode --session <sid> --note "..."
bin/fleet resume
```

The drain reports the work the worker still owns; nothing is cancelled, released or dropped. The
removal refuses while a task or turn is queued, in flight or uncertain, while a recovery holds
the session, and while the native identity cannot be verified. A worker that is known to be
running is never removed. After the manager is upgraded to a build that honours `draining`, the
pause is no longer needed for the drain window.

## Rollback

```
bin/fleet rollback-opencode --request-key <scale-key> --note "..."
```

The rollback withdraws exactly the roster entries that request added, records the request as
rolled back and leaves every native session in the account with its history — a native session is
never deleted. A rolled-back request key never adds again, so a retry cannot resurrect the
sessions silently; a new request needs a new key. The receipts keep the history of what was
created, so the audit trail survives the rollback.

If the roster was changed by something else in the meantime, the write is refused: the roster is
compared with the copy the request started from, and a changed configuration is never
overwritten.

## What this plan never does

It creates no account, changes no credential, permission, provider or model, clears no hold,
cancels no task, deletes no native session, starts no second manager, and never deploys or
releases anything. Every stage is reversible except the native sessions, which are kept on
purpose.
