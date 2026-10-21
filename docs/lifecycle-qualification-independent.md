# Independent lifecycle qualification — Fleet OpenCode session lifecycle

**Author:** W8 (independent qualification; not the runtime owner)
**Runtime owner:** W5 — this document qualifies their implementation and changes none of it
**Qualification suite:** `tests/test_lifecycle_qualification_independent.py` (one owned file)
**Status:** **not qualified.** Against the owner's final reviewed head `9d2bd8db`, four invariants
still do not pass (three assertion failures and one error); a fifth, found at the admission base,
was repaired by the owner and is kept as a passing regression. Each failure is recorded below with
its exact test name, command, observed behaviour and the smallest owned-path patch suggestion for
the source owner.

## 1. The exact source base

| Field | Value |
| --- | --- |
| Repository | `/Users/amf/Developer/.codex/fleet` (remote `0xPlayerOne/fleet`) |
| **Admission base** | **`f4f26de14359d2f3b220fe5195f65c91a82c7b9f`** — "feat(fleet): one command to a total roster size, with a single writer lock" — the head W5 published at admission, treated as a provisional immutable test base |
| **Owner final head** | **`9d2bd8db9432fe51c3848c8e143c50f3272be662`** — "feat(fleet): match the resolved session-create contract, and prove the reload" — published as `origin/feat/opencode-session-lifecycle-add-remove`, submitted for review by W5 at 2026-10-21T09:23:56Z (`review_ready`, task `20261021-w5-fleet-lifecycle-final-add-only-and-native-contract-proof-08`, state `done`) |
| Owner branch | `feat/opencode-session-lifecycle-add-remove` (published; the local owner worktree `/Users/amf/Developer/.worktrees/fleet-opencode-session-lifecycle` is read-only for this qualification) |
| **Qualification branch** | `codex/20261021-w8-independent-lifecycle-qualification`, head **`f7eca207d2c8b554dc16265163f77d54f3d1eaee`** (two commits on the owner final head) |
| Qualification worktree | `/Users/amf/Developer/.worktrees/fleet-lifecycle-qualification-w8` |
| Branch base | the two qualification commits were first built on `f4f26de` and then **rebased onto the owner final head** `9d2bd8db` once the owner's task reported terminal. The rebase replays **only W8's two commits**; no owner path was changed, and the diff against the owner branch is still exactly the two new paths |
| Base provenance | W5's task was `working` when the suite was first built, so the qualification did not wait: it was built, checked and run against `f4f26de`, and the defects were routed back before the owner's task finished |

The admission base and the owner final head are both reported because the evidence has two parts:
what the published base did (§3, D1–D5) and what the owner's final reviewed head still does (§3,
final-head status). **Qualification is not declared: the same suite does not pass on the final
head** (§3, §6).

## 2. What the suite covers, and what it deliberately does not

The suite owns one file and imports nothing from the source owner's own suite. It builds its own
fixtures:

* a **temporary fleet root** — `paths.ROOT` and every derived path point into a disposable
  directory, so no live fleet, manager, session, configuration or database is reachable from it;
* a **synthetic native identity inventory** — a temporary SQLite file with the `session_v2` shape the
  runtime reads;
* a **fake native session server** — a temporary executable that serves the OpenAPI schema the
  runtime consults, records every call, and writes the sessions it is asked to create into the
  synthetic inventory;
* a **real but local git worktree** — a committed file, a dirty tracked file, an untracked file and
  an unpublished commit, so "dirty and unpublished" is a fact and not a mock.

No provider is contacted, no real native database is written, no native session is created outside
the sandbox, no live manager is started, no registered worktree is touched, and nothing is removed
from the host. The fake server accepts either model shape on purpose, so that **one** conformance
defect does not mask the other twenty-eight invariants; the strict shape is asserted by exactly one
dedicated test (§3, D1).

### Coverage matrix

| Invariant | Test | Race / fault | Result at `f4f26de` |
| --- | --- | --- | --- |
| Native request carries the model and the directory and nothing else | `test_the_native_call_carries_only_the_model_and_the_directory` | — | pass |
| **The request body conforms to the schema the runtime itself proved** | `test_the_native_request_body_conforms_to_the_schema_the_runtime_proved` | — | **FAIL (D1)** |
| The created session is read back from the native identity row | `test_the_created_session_is_read_back_from_the_synthetic_identity_row` | — | pass |
| Duplicate native identity is refused; the roster stays exact and distinct | `test_a_duplicate_native_identity_is_refused_and_the_roster_stays_exact` | — | pass |
| Distinct count and registration proof are exact | `test_the_registration_proof_counts_distinct_sessions_exactly` | — | pass |
| Dry run: no native call, no receipt, no event, no roster change | `test_a_dry_run_makes_no_native_call_and_writes_no_receipt` | — | pass |
| Dry run of a target total writes nothing | `test_a_dry_run_of_a_target_total_writes_nothing` | — | pass |
| The same key with a different payload is refused before any native call | `test_the_same_key_with_a_different_payload_is_refused_before_any_native_call` | — | pass |
| The same key with the same payload is idempotent | `test_the_same_key_with_the_same_payload_is_idempotent` | — | pass |
| **Crash between the native effect and the receipt creates no second session** | `test_a_crash_between_the_native_effect_and_its_receipt_creates_no_second_session` | fault injection | pass |
| **A crashed creator leaves the request uncertain and not retryable** | `test_a_crashed_creator_leaves_the_request_uncertain_and_not_retryable` | fault injection | pass |
| An explicitly named session is registered without a second creation | `test_an_explicitly_named_session_is_registered_without_a_second_creation` | fault injection | pass |
| A native effect that cannot be read back stays uncertain | `test_a_native_effect_that_cannot_be_read_back_stays_uncertain` | fault injection | pass |
| **A malformed queue file does not let a removal pass unseen** | `test_a_malformed_queue_file_does_not_let_a_removal_pass_unseen` | — | **FAIL (D2)** |
| An unreadable task ledger refuses the removal and keeps the roster | `test_an_unreadable_task_ledger_refuses_the_removal_and_keeps_the_roster` | fault injection | pass |
| **A terminal task in the queue history does not block the removal** | `test_a_terminal_task_in_the_queue_history_does_not_block_the_removal` | — | **FAIL (D3)** |
| A queue entry that names the worker is never lost by a drain | `test_a_queue_entry_that_names_the_worker_is_never_lost_by_a_drain` | — | pass |
| A running worker is never removed | `test_a_running_worker_is_never_removed` | — | pass |
| A removal keeps the native session and every hold | `test_a_removal_keeps_the_native_session_and_every_hold` | — | pass |
| **A removal leaves a dirty, unpublished worktree byte-identical** | `test_a_removal_leaves_a_dirty_unpublished_worktree_byte_identical` | — | pass |
| An active worker with unpublished work is never removed | `test_an_active_worker_with_unpublished_work_is_never_removed` | — | pass |
| **Concurrent same-key creation makes exactly one native call** | `test_two_barrier_aligned_requests_with_one_key_make_exactly_one_native_call` | barrier | pass |
| **Concurrent same-key requests with different payloads conflict** | `test_two_barrier_aligned_requests_with_one_key_and_different_payloads_conflict` | barrier | pass |
| A drain and a removal of one worker cannot both take effect | `test_a_drain_and_a_removal_of_one_worker_cannot_both_take_effect` | barrier | pass |
| **A stale import never binds a task to a removed worker** | `test_an_import_with_a_stale_roster_snapshot_never_binds_a_task_to_a_removed_worker` | barrier | **FAIL (D4)** |
| **A model-selection write never loses a roster entry** | `test_a_model_selection_write_never_loses_a_roster_entry` | barrier | **FAIL (D5)** |
| The status report counts the roster and the receipts truthfully | `test_the_status_report_counts_the_roster_and_the_receipts_truthfully` | — | pass |
| **A recovery receipt refuses a turn whose cleanup is not confirmed** | `test_a_recovery_receipt_refuses_a_turn_whose_cleanup_is_not_confirmed` | fault injection | pass |
| **A recovery receipt refuses a turn whose process is still live** | `test_a_recovery_receipt_refuses_a_turn_whose_process_is_still_live` | fault injection | pass |

Barrier cases use `threading.Barrier(2)` so the two writers meet at a known point instead of
depending on scheduling; the fault injections replace the native call, the identity row and the
ledger with controlled failures. The suite asserts, in every case, that **no duplicate native
call** was made, that **no queue entry was lost**, that **no unsafe registration or removal**
happened, and that the reported status matches what actually occurred.

## 3. The five published defects (retained as failing acceptance evidence)

Each item is a failing test in the committed suite. **No production fix is implemented or copied
here**; the suggestion is the smallest change in the source owner's own paths, offered for the
owner to accept, reject or re-shape.

**Status against the owner final head `9d2bd8db`:** D1 is **repaired** by the owner's commit
"match the resolved session-create contract, and prove the reload" — its test now passes. **D2, D3,
D4 and D5 still fail**, unchanged, and they are retained here as the open regressions.

### D1 — the session-create body does not conform to the schema the runtime proved

* **Test:** `test_the_native_request_body_conforms_to_the_schema_the_runtime_proved`
  (`tests/test_lifecycle_qualification_independent.py`, class `NativeEffectTests`)
* **Command:** `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"`
* **Observed:** the body the runtime sends is
  `{"location": {"directory": "<cwd>"}, "model": "opencode-go/minimax-m3.1-flash-free"}` — the
  model is a **plain string**. The schema the runtime itself served and proved declares `model` as
  a `$ref` to `Model.Ref`, an object with a required `id` and `providerID`. The assertion failure
  is `model must be sent as the declared object, not str`.
* **Why it matters:** the runtime's own contract is that the call is made only against a schema it
  proved. A body that does not satisfy that schema is refused by a conforming server, so the
  creation can fail against the real CLI while the local proof says the call is supported.
* **Smallest owned-path suggestion** (source owner's path, `src/fleet/opencode_lifecycle.py`):
  build the body from the resolved schema — a `Model.Ref` object `{id, providerID}` and a
  `Location.PublicRef` object `{directory}` — and have `verify_native_api_support` assert the
  resolved types it depends on, so a schema that stops declaring them is refused before the call.

### D2 — an unreadable queue file makes the removal barrier blind

* **Test:** `test_a_malformed_queue_file_does_not_let_a_removal_pass_unseen`
  (class `QueueAndLedgerIntegrityTests`)
* **Observed:** with `data/queue.json` containing invalid JSON, `remove_session` **did not refuse** —
  the assertion `ValueError not raised` is the failure. The worker left the roster while the queue
  that names its pending work could not be read at all.
* **Why it matters:** the queue is one of the two places a worker can still own work that is not
  in the ledger. A queue that cannot be parsed is not evidence that no task is waiting; the safe
  reading is the opposite, and the removal barrier must fail closed on it.
* **Smallest owned-path suggestion** (`src/fleet/roster.py`): let `queued_tasks_for` distinguish
  "read and empty" from "could not be read" (raise on an unreadable or malformed queue file), and
  let `_remove_session` in `src/fleet/opencode_lifecycle.py` refuse the removal when the queue
  could not be read.

### D3 — terminal queue history blocks the removal permanently

* **Test:** `test_a_terminal_task_in_the_queue_history_does_not_block_the_removal`
  (class `QueueAndLedgerIntegrityTests`)
* **Observed:** the removal raised
  `ValueError: The worker still has tasks waiting to be imported: task-finished(awaiting-import)`
  for a task that was already imported and has state `finished` in the ledger. The queue file
  keeps every task it ever held, so the barrier never clears.
* **Why it matters:** this is the live shape of the fleet: the deployed queue file holds every
  historical task with its `sessionId`. Every worker that ever received a queue task can therefore
  never be removed through this command, so the removal path is unreachable in practice while
  reporting a *pending* work state that does not exist.
* **Smallest owned-path suggestion** (`src/fleet/roster.py`): report only the queue entries that
  are genuinely awaiting import — an entry whose task id is absent from the ledger, or present and
  not yet imported — and report the already-imported ones separately (as history) instead of as
  outstanding work.

### D4 — an import with a stale roster snapshot binds a task to a removed worker

* **Test:** `test_an_import_with_a_stale_roster_snapshot_never_binds_a_task_to_a_removed_worker`
  (class `BarrierRaceTests`)
* **Observed:** the removal succeeded first; a task for the removed worker was then written into
  the queue file; an import that still carried the **pre-removal** roster snapshot imported it.
  The assertion failure is `'ses_EXISTING000000000001' unexpectedly found in
  ['ses_EXISTING000000000001']` — the task was bound to a worker that had left the roster. The
  queue entry itself was not lost; it is now attached to a worker that can never run it.
* **Why it matters:** the manager loads its roster snapshot outside the writer lock and imports
  under it, so the snapshot can be stale by the time the import runs. A task bound to a removed
  worker is silently undeliverable.
* **Smallest owned-path suggestion** (`src/fleet/manager.py`): re-read the roster inside the
  writer lock at import time and refuse any task whose `sessionId` is no longer configured
  (`Unapproved or unknown task target`), rather than trusting the caller's snapshot.

### D5 — a model-selection write loses a concurrently added worker

* **Test:** `test_a_model_selection_write_never_loses_a_roster_entry` (class `BarrierRaceTests`)
* **Observed:** the selection read the roster, an addition registered a second worker, and the
  selection then wrote its **stale copy** back. The assertion failure is
  `1 not greater than or equal to 2`: the added worker is gone from the roster, and only the one
  worker the selection had read survives. The native session was created and the receipt says it
  was registered, so the receipt and the roster now disagree.
* **Why it matters:** the roster is the identity source of the fleet. A lost entry is an unowned
  worker: its native session exists, its receipt says it is registered, and nothing dispatches it.
  The roster writer itself is protected by a compare-and-swap; this writer is not.
* **Smallest owned-path suggestion** (`src/fleet/model_selection.py`): take the roster writer lock
  (in the documented order queue → roster → model) and write through
  `roster.write_config_cas`, so a configuration that changed underneath is refused rather than
  overwritten — the same protection the roster writer already has.

## 4. Candidate cases accepted and refused

| Candidate | Accepted | Refused |
| --- | --- | --- |
| Native create | a body with exactly the declared properties, a model reference the schema declares, and a directory under the worktree root | a model that is not the account's approved model; a count that is not a positive whole number or exceeds the bound; a cwd outside the worktree root; a wrong root; a different native account; a schema without `session.create` |
| Idempotency | the same key with the same payload — nothing is created a second time | the same key with a different count, total, account, working directory, model, dispatch flag or admission limit |
| Dry run | the plan is reported with no native call, no receipt, no event and an unchanged configuration | — (a dry run writes nothing, so nothing can be refused) |
| Reconciliation | an explicitly named native session that verifies is registered, without a second creation | a named session that cannot be read (absence is no proof); a named session that does not match the request |
| Drain | the worker is marked draining and the work it still owns is reported; the queue file is untouched | — (a drain never cancels, releases or drops a task) |
| Removal | a worker with no outstanding work, no pending import, no active recovery and a verifiable native identity is removed; the native session and every hold survive | a worker with an outstanding task; a worker with a task waiting to be imported; a worker a recovery holds; a worker whose native turn state says it is running — the operator override never applies to it; a worker whose native identity cannot be read, unless the operator overrides it |
| Recovery | a terminal turn whose process group is gone and whose cleanup is confirmed | a turn whose cleanup is not confirmed; a turn whose recorded process is still live; a receipt that does not match the launch |

## 5. Untested native and production limits

These are **not** qualified by this suite and are stated as limits, not as passes:

* **No real native CLI, no real native database, no real session.** The native server is a fake.
  Nothing here proves that the installed OpenCode CLI accepts the body the runtime sends, that a
  real session row is written the way the fake writes it, or that the real schema matches the one
  the fixture serves. D1 in particular is a statement about the runtime's own request against its
  own proof, not a live call.
* **No provider, no model call, no turn.** No model is ever invoked; nothing is dispatched.
* **No live manager, no live fleet, no live configuration, no live registry.** The queue, the
  ledger, the events, the receipts and the roster are all synthetic.
* **No real worktree.** The worktree fixture is a temporary repository; the invariant proven is that
  the lifecycle does not write to the working directory it names, not that it behaves correctly
  against a repository with real objects, submodules or a linked worktree.
* **The dispatcher and the controller are not exercised end to end.** `import_queue` is called
  directly with a synthetic roster snapshot; the manager loop, the dispatch claim and the
  `check_queue`/`pr_queue` interactions are out of the suite's scope.
* **The pinned older manager** (`_manager_honours_draining`) is reported by the drain but not
  exercised against a running manager, so "the drain is effective" is not proven here.
* **Timing and crash windows are injected, not real.** The crash between the native effect and the
  receipt is simulated by raising out of the native call in-process; the real crash window also
  includes the process dying between the roster write and the receipt, which is not reproduced.
* **Single-process concurrency.** The race cases use threads inside one process; the file locks are
  advisory and would behave the same across processes, but that is not exercised.
* **The recovery receipts are checked as evidence, not as a recovery.** `verify_stopped_native_execution`
  is called with synthetic receipts; the surrounding recovery flow (authorization, adoption,
  reconciliation) is out of scope.

## 6. Commands and results

| Command | Scope | Result |
| --- | --- | --- |
| `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"` | this suite, at the **admission base** `f4f26de` | **29 tests: 24 pass, 4 assertion failures, 1 error** — 5 not passing (D1–D5) |
| `python3 -m unittest discover -s tests -t .` | the whole repository at `f4f26de`, including this suite | **492 tests: 11 failures, 1 error** — 5 of them are D1–D5, **7 are pre-existing** (below) |
| `python3 -m unittest discover -s tests -t . -p "test_[!l]*.py"` | the same repository at `f4f26de` **without** this file (it is the only `test_l*` module) | **463 tests: 7 failures** — the pre-existing baseline |
| `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"` | **the same suite at the owner final head `9d2bd8db`** | **29 tests: 25 pass, 3 assertion failures, 1 error** — 4 not passing (D2–D5); D1 now passes |
| `python3 -m unittest discover -s tests -t .` | the whole repository at `9d2bd8db`, including this suite | **504 tests: 10 failures, 1 error** — 4 of them are D2–D5, 7 pre-existing (unchanged) |
| `python3 -m py_compile tests/test_lifecycle_qualification_independent.py` | the owned test file | pass |
| `git diff --stat 9d2bd8db...codex/20261021-w8-independent-lifecycle-qualification` | the owned delta against the owner branch | two new paths only |

**This suite adds no failure to the repository's own tests.** The 7 failures in the baseline run are
present with or without this file, all in `tests/test_queue_runway.py`
(`QueueRunwayTests`): `test_active_is_not_reserve`,
`test_cycle_returns_early_when_runway_is_low`,
`test_default_remains_nonblocking_snapshot_with_timeout_zero`,
`test_pending_import_is_distinct_and_deduplicated`, `test_queued_successors_cover_both_workers`,
`test_unapproved_or_unknown_targets_do_not_cover_workers` and
`test_uncertain_and_finished_never_count`. They belong to the runtime owner's area and are
**pre-existing at the published base**; the observed symptom is a *session id* appearing in a
report's task list where a task id was expected
(`['b', 'ses_EXISTING000000000001'] != ['b']`). That is the same queue-history area as D3, and it
is reported here as context for the owner, not as a finding of this qualification.

The repository has no separate lint or format tool (no CI workflow, no packaging configuration);
the documented check is the unittest discovery above, plus a byte-compile of the owned file. The
owned file follows the repository's style (module docstring, `unittest.TestCase` classes, no
third-party imports, the same source-layout path bootstrap as the repository's own tests) and stays
within 100 columns.

## 7. Exact remaining gaps

1. **The suite does not pass on the owner final head either.** Four invariants still do not pass at
   `9d2bd8db` — D2, D3, D4 and D5 (§3). Until the source owner repairs them — or records why the
   invariant is wrong — this branch cannot be qualified.
2. **The owner final head is not green in the repository's own suite either.** 7 tests in
   `tests/test_queue_runway.py` fail at both `f4f26de` and `9d2bd8db` (§6). They are the runtime
   owner's, and they are in the same queue-history area as D3.
3. **D1 is repaired and kept as a regression.** Its test stays in the suite and now passes on
   `9d2bd8db`; it is not deleted, so the repair remains covered.
4. **D1–D5 are observations, not verdicts.** The owner may show that an invariant is wrong; this
   document then records that, exactly as the earlier Fleet audits record withdrawn claims
   instead of editing them away.
5. **The native and production limits in §5 are not closed by this suite.** Closing them needs a
   disposable native CLI and a disposable fleet, which are outside this outcome's scope and outside
   its authority.
6. **The two owned paths are the whole delta.** No Fleet runtime, CLI, roster, existing test or
   documentation file was modified, and no live manager, session, configuration, registered
   worktree or resource was touched.
