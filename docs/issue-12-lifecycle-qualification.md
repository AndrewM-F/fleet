# Issue #12 — lifecycle qualification record

Checked on 2026-10-24 against Fleet source at the start of this qualification,
`6c4195f96ea7a34f40228f0034aca3384c8d1cbb` (merged PR #20).

## Offline acceptance evidence

All fixtures below are disposable. The command-level test starts the current
source manager only in a temporary root with no workers. It does not use the
live Fleet root, OpenCode account, native database, queue, or manager process.

| Acceptance | Evidence |
| --- | --- |
| Start, status, pause, resume, and stop | `tests/test_issue_12_acceptance.py` runs the actual CLI against a temporary Fleet root and verifies pause state survives start/stop. |
| Drain prevents new work, including a stale pre-drain snapshot | `tests/test_opencode_lifecycle.py::DrainTests` exercises both admission paths. |
| Drain preserves queued work and task holds | `test_drain_never_cancels_drops_or_releases_a_queued_task`; awaiting-import work is separately reported. |
| Removal refuses active, uncertain, queued, held-by-recovery, or unverifiable work | `tests/test_opencode_lifecycle.py::RemovalRefusalTests` and `tests/test_lifecycle_convergence.py`. |
| Removal preserves native session history, task holds, and uncommitted worktree files | `test_removal_keeps_the_native_session_and_every_hold` now checks an untracked sentinel file before and after removal. |
| Recovery and stale identity handling | `tests/test_recovery.py`, `tests/test_recovery_direct_turn.py`, `tests/test_recovery_model_identity.py`, and `tests/test_stale_identity_adoption.py`. |
| Reversible activation plan | `docs/opencode-lifecycle-rollout.md`; it remains a plan and is not executed by tests. |

The current source dispatcher reads `draining` both during dispatch and again
inside the serialized claim path. The regression tests cover a drain racing a
previously read slot, so the old snapshot cannot admit a new task after the
drain.

## Live activation boundary

At this check, `bin/fleet status` reported the legacy manager stopped, the
supervisor disabled, and no configured sessions or tasks. No Fleet manager
process was present. The installed OpenCode executable was
`/Users/amf/.bun/install/global/node_modules/.bun/@opencode+cli@2.1.3/node_modules/@opencode/cli/bin/opencode.exe`,
version `2.1.3`. An isolated read-only OpenAPI probe confirmed the session
creation contract; it did not create a session or contact a provider.

The read-only request for current native session activity was refused with
`Authentication required`. The supported local OpenCode service must be
authenticated by its owner before a live lifecycle check can read session
state. No credentials were read or changed, and no manager was started. The
live native-turn evidence in Issue #12 therefore remains pending a separately
permitted activation and successful authenticated status check. Offline tests
are not represented as live acceptance evidence.
