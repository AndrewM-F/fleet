# Lifecycle qualification — provenance of `tests/test_lifecycle_qualification.py`

This file is a **historical record**. It says where the cases in
`tests/test_lifecycle_qualification.py` came from. It is not evidence that any
later head passes, and the pass and fail counts below are copied from the
source documents, not re-run.

## What was kept, and why

An independent qualifier (W8) wrote a 61-test suite,
`tests/test_lifecycle_qualification_independent.py`, against the
OpenCode lifecycle owner's earlier heads. Six unmerged snapshots of it were
published. Run against current `main`, the largest snapshot has 61 tests: 59
pass and 2 fail. Both failures are contracts that later merged work
deliberately changed (see below), not regressions.

Statement coverage is not enough to decide what is worth keeping, so each test
was run with per-test branch coverage against the current suite. 54 of the 61
tests only reach branches the current suite already reaches, and have a named
analogue in `test_lifecycle_findings.py`, `test_lifecycle_repairs.py`,
`test_lifecycle_convergence.py`, `test_opencode_lifecycle.py` or
`test_model_selection.py`. Five tests reach branches that nothing on `main`
reaches, and those five are kept here unchanged in behaviour:

| Test | What it pins |
| --- | --- |
| `InitialIntentRaceTests.test_a_stale_caller_that_resumes_after_an_uncertain_attempt_fails_closed` | a caller that snapshotted before a concurrent attempt cannot create a second native session |
| `CrashAndUncertaintyTests.test_a_native_effect_that_cannot_be_read_back_stays_uncertain` | an unreadable native result is uncertain, never success |
| `QueueAndLedgerIntegrityTests.test_an_unreadable_task_ledger_refuses_the_removal_and_keeps_the_roster` | a removal refuses when the task ledger cannot be read, and the roster is kept |
| `BarrierRaceTests.test_two_barrier_aligned_requests_with_one_key_and_different_payloads_conflict` | one idempotency key with two payloads conflicts |
| `ReceiptTruthfulnessTests.test_the_status_report_counts_the_roster_and_the_receipts_truthfully` | the status report counts the roster and receipts as they are |

The two stale contracts are kept as `LifecycleReplayTests`, restated to the
current behaviour:

* A second removal of the same worker is idempotent and reports
  `alreadyRemoved` (the snapshots expected an error; `main` pins the new
  behaviour in `tests/test_lifecycle_convergence.py`).
* Removing a worker that has no receipt needs a native identity row first (the
  snapshots removed a worker with no native identity; the adoption work
  requires the identity proof).

No assertion was weakened: each restated test asserts the same invariant
(nothing changes on replay; a trail is left) against the current contract.

## Model

The sandbox uses the approved model `opencode-go/minimax-m3.1-flash-free`. No
other model is named, and no provider is contacted: the native CLI is a fake
executable in a temporary directory.

## Source snapshots (historical)

| Branch | Full SHA | Tests in snapshot | Result recorded in its own history |
| --- | --- | --- | --- |
| `codex/20261021-w8-independent-lifecycle-qualification` | `6e3454ffd632bb1177821d5b4a08d93d82c5de0b` | 32 | not qualified against owner head `9d2bd8db`: 25 pass, 6 fail, 1 error |
| `codex/20261021-w8-independent-lifecycle-qualification-at-0cca1b35` | `37a47e1bd02239e53f1feb31720cdbd2b03b2023` | 40 | not qualified against `0cca1b35`: 36 pass, 4 fail |
| `codex/20261021-w8-independent-lifecycle-qualification-at-709dfcb` | `e75e6ce3e8f52a38d729fae57241916ba414ca02` | 43 | recorded against `709dfcb`: 42 pass, 1 fail |
| `recovery/worktree-prune-20261023-fafdf5b` | `fafdf5bd902ccddf471fcdd0f398d7abc880e68a` | 43 | recorded green at `20670971` |
| `recovery/worktree-prune-20261023-c60b99e` | `c60b99e2125da085b41249bec372fc4d26752ebb` | 50 | the receipt-transition guard at `8ef01717` |
| `recovery/worktree-prune-20261023-9ed5c02` | `9ed5c02f24e478ba07cbad7ddcdd53bb5cef7c22` | 61 | seven new lifecycle and recovery cases at `17890950` |
| `codex/20261021-w8-independent-lifecycle-qualification-on-main` | `6ec9d23d3948fffb161b6d925a4cc5a38f8aea7d` | 61 | the same suite carried onto the merged owner head `c25f8fd7`; its test blob `9488b52eced3471a2e68a2989c55a54fb158dfe0` and doc blob `0cc826eb51088831b8b1fd331eaf9393eab4447c` equal `9ed5c02`'s |

The earlier counts belong to the heads they were run on. None of them is
evidence for any later head, including this one.
