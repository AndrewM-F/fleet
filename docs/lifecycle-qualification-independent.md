# Independent lifecycle qualification — Fleet OpenCode session lifecycle

**Author:** W8 (independent qualification; not the runtime owner)
**Runtime owner:** W5 — this document qualifies their implementation and changes none of it
**Qualification suite:** `tests/test_lifecycle_qualification_independent.py` (one owned file)
**Status:** **not qualified.** Against the owner's published PR #8 head `0cca1b35`, **40 tests:
36 pass, 4 fail** — four invariants do not pass. The owner repaired the seven findings this
qualification had reported; they are now covered by passing regressions, and **four cases remain
open** (D9–D12). Every failure is recorded below with its exact test name, command, observed
behaviour and the smallest owned-path patch suggestion for the source owner.

## 1. The exact source base

| Field | Value |
| --- | --- |
| **Target head** | **`8ef01717d9810196a5d84416be8c54386812814e`** — "fix(fleet): treat an ambiguous native effect as uncertain, and keep the receipt monotonic" |
| **Live PR head** | the same commit: `origin/feat/opencode-session-lifecycle-add-remove` = `8ef01717d9810196a5d84416be8c54386812814e` and the local owner branch names it. The remote itself could **not** be queried — `git ls-remote https://github.com/0xPlayerOne/fleet.git` fails with `fatal: could not read Username for 'https://github.com': Device not configured` — so the commit object is the reference |
| **PR draft / auto-merge state** | **unknown from any local record.** No local registry, event, receipt or handoff carries this pull request's draft or auto-merge state, and the GitHub API is not reachable from this worker (no authorization, nothing probed or changed). The state is therefore reported as unknown rather than assumed |
| **Isolated exact-head checkout** | `/Users/amf/Developer/.worktrees/fleet-lifecycle-qualification-w8-at-8ef01717`, branch `codex/20261021-w8-independent-lifecycle-qualification-at-8ef01717`, created clean from that commit. The owner's implementation and existing runtime tests were not edited |
| Owned delta against the target | exactly two new paths: `tests/test_lifecycle_qualification_independent.py`, `docs/lifecycle-qualification-independent.md`; **0** owner paths touched |
| Earlier heads, retained as historical evidence | `0cca1b35` (35 pass / 5 fail), `709dfcb` (42 pass / 1 fail), `20670971` (43 pass / 0 fail) — each on its own branch with its own patch and logs. **None of them is evidence for this head**; every row of the matrix below was re-run against `8ef01717` |
| Operations not performed | no fetch, no pull, no reset, no stash, no prune, no mutation of the owner's branch, no publication |

The owner's delta from the last qualified head (`20670971`) to this one is `src/fleet/opencode_lifecycle.py`
(+74/−18) and a new `tests/test_ambiguous_effects.py` (344 lines): an empty or malformed native
answer and a native create that fails **after it started** are now an ambiguous effect (uncertain,
not retryable) instead of a plain failure; a receipt's transition is monotonic, so a `registered`
receipt is never downgraded; and the provisioning intent's creator is refreshed on every write, so
a safe retry takes the request over.

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

### The full candidate matrix, at `8ef01717`

Every row was re-run against this exact head. "Result" is the outcome of that run.

| # | Invariant (root's wording) | Test | Class | Result |
| --- | --- | --- | --- | --- |
| 1 | The native request carries the model and the directory and nothing else | `test_the_native_call_carries_only_the_model_and_the_directory` | `NativeEffectTests` | pass |
| 2 | The created session is read back from the native identity row | `test_the_created_session_is_read_back_from_the_synthetic_identity_row` | `NativeEffectTests` | pass |
| 3 | The request body conforms to the schema the runtime itself proved | `test_the_native_request_body_conforms_to_the_schema_the_runtime_proved` | `NativeEffectTests` | pass |
| 4 | A duplicate native identity is refused; the roster stays exact and distinct | `test_a_duplicate_native_identity_is_refused_and_the_roster_stays_exact` | `NativeEffectTests` | pass |
| 5 | The registration proof counts the sessions exactly | `test_the_registration_proof_counts_distinct_sessions_exactly` | `NativeEffectTests` | pass |
| 6 | A dry run makes no native call and writes no receipt, event or roster change | `test_a_dry_run_makes_no_native_call_and_writes_no_receipt` | `DryRunTests` | pass |
| 7 | A dry run of a target total writes nothing | `test_a_dry_run_of_a_target_total_writes_nothing` | `DryRunTests` | pass |
| 8 | A dry run does not mutate a receipt | `test_a_dry_run_does_not_mutate_a_receipt` | `ReviewFindingReproductionTests` | pass |
| 9 | The same key with a different payload is refused before any native call | `test_the_same_key_with_a_different_payload_is_refused_before_any_native_call` | `RequestKeyBindingTests` | pass |
| 10 | The same key with the same payload is idempotent | `test_the_same_key_with_the_same_payload_is_idempotent` | `RequestKeyBindingTests` | pass |
| 11 | A crash between the native effect and its receipt creates no second session | `test_a_crash_between_the_native_effect_and_its_receipt_creates_no_second_session` | `CrashAndUncertaintyTests` | pass |
| 12 | A crashed creator leaves the request uncertain and not retryable | `test_a_crashed_creator_leaves_the_request_uncertain_and_not_retryable` | `CrashAndUncertaintyTests` | pass |
| 13 | A named session is registered without a second creation | `test_an_explicitly_named_session_is_registered_without_a_second_creation` | `CrashAndUncertaintyTests` | pass |
| 14 | A native effect that cannot be read back stays uncertain | `test_a_native_effect_that_cannot_be_read_back_stays_uncertain` | `CrashAndUncertaintyTests` | pass |
| 15 | **A native create that fails after it started is uncertain and not retryable** | `test_a_native_create_that_fails_after_it_started_is_uncertain_and_not_retryable` | `ExactHeadInvariantTests` | pass |
| 16 | **A malformed native answer after the command ran is uncertain and not retryable** | `test_a_malformed_native_answer_after_the_command_ran_is_uncertain` | `ExactHeadInvariantTests` | pass |
| 17 | **A safe retry takes the request over and changes the provisioning owner** | `test_a_safe_retry_takes_over_the_request_and_changes_its_owner` | `ExactHeadInvariantTests` | pass |
| 18 | **A registered receipt is never downgraded** | `test_a_registered_receipt_is_never_downgraded` | `ExactHeadInvariantTests` | pass |
| 19 | **A malformed existing queue file refuses the removal** | `test_a_malformed_queue_file_does_not_let_a_removal_pass_unseen` | `QueueAndLedgerIntegrityTests` | pass |
| 20 | **A truncated existing queue file refuses the removal** | `test_a_truncated_queue_file_refuses_the_removal` | `QueueAndLedgerIntegrityTests` | pass |
| 21 | **An existing empty or whitespace queue file refuses the removal** | `test_an_existing_empty_queue_file_refuses_the_removal` | `QueueAndLedgerIntegrityTests` | pass |
| 22 | **An existing queue file without a tasks list refuses the removal** | `test_an_existing_queue_file_without_a_tasks_list_refuses_the_removal` | `QueueAndLedgerIntegrityTests` | pass |
| 23 | A missing queue file is distinguished from an unreadable one | `test_a_missing_queue_file_is_distinguished_from_an_unreadable_one` | `QueueAndLedgerIntegrityTests` | pass |
| 24 | An unreadable task ledger refuses the removal and keeps the roster | `test_an_unreadable_task_ledger_refuses_the_removal_and_keeps_the_roster` | `QueueAndLedgerIntegrityTests` | pass |
| 25 | Terminal queue history does not block the removal | `test_a_terminal_task_in_the_queue_history_does_not_block_the_removal` | `QueueAndLedgerIntegrityTests` | pass |
| 26 | A queue entry that names the worker is never lost by a drain | `test_a_queue_entry_that_names_the_worker_is_never_lost_by_a_drain` | `QueueAndLedgerIntegrityTests` | pass |
| 27 | A running worker is never removed | `test_a_running_worker_is_never_removed` | `RemovalBarrierTests` | pass |
| 28 | A removal keeps the native session and every hold | `test_a_removal_keeps_the_native_session_and_every_hold` | `RemovalBarrierTests` | pass |
| 29 | A removal leaves a dirty, unpublished worktree byte-identical | `test_a_removal_leaves_a_dirty_unpublished_worktree_byte_identical` | `RemovalBarrierTests` | pass |
| 30 | An active worker with unpublished work is never removed | `test_an_active_worker_with_unpublished_work_is_never_removed` | `RemovalBarrierTests` | pass |
| 31 | Concurrent same-key creation makes exactly one native call | `test_two_barrier_aligned_requests_with_one_key_make_exactly_one_native_call` | `BarrierRaceTests` | pass |
| 32 | Concurrent same-key requests with different payloads conflict | `test_two_barrier_aligned_requests_with_one_key_and_different_payloads_conflict` | `BarrierRaceTests` | pass |
| 33 | A drain and a removal of one worker cannot both take effect | `test_a_drain_and_a_removal_of_one_worker_cannot_both_take_effect` | `BarrierRaceTests` | pass |
| 34 | A stale import never binds a task to a removed worker | `test_an_import_with_a_stale_roster_snapshot_never_binds_a_task_to_a_removed_worker` | `BarrierRaceTests` | pass |
| 35 | A model selection never loses a roster entry | `test_a_model_selection_write_never_loses_a_roster_entry` | `BarrierRaceTests` | pass |
| 36 | **The CAS digest is captured with the configuration snapshot** | `test_the_compare_and_swap_uses_the_digest_captured_with_the_snapshot` | `BarrierRaceTests` | pass |
| 37 | **An out-of-band configuration change after the snapshot is rejected** | `test_an_out_of_band_config_change_after_the_snapshot_is_rejected` | `BarrierRaceTests` | pass |
| 38 | A concurrent same-key request creates one session and keeps the final intent | `test_a_concurrent_same_key_request_creates_one_session_and_keeps_the_final_intent` | `BarrierRaceTests` | pass |
| 39 | The same key never creates a second session after a takeover | `test_a_same_key_request_never_creates_a_second_native_session_after_a_takeover` | `ReviewFindingReproductionTests` | pass |
| 40 | A partial-create crash leaves the request uncertain instead of creating again | `test_a_partial_create_crash_leaves_the_request_uncertain_instead_of_creating_again` | `ReviewFindingReproductionTests` | pass |
| 41 | The status report counts the roster and the receipts truthfully | `test_the_status_report_counts_the_roster_and_the_receipts_truthfully` | `ReceiptTruthfulnessTests` | pass |
| 42 | A recovery receipt refuses a turn whose cleanup is not confirmed | `test_a_recovery_receipt_refuses_a_turn_whose_cleanup_is_not_confirmed` | `ReceiptTruthfulnessTests` | pass |
| 43 | A recovery receipt refuses a turn whose process is still live | `test_a_recovery_receipt_refuses_a_turn_whose_process_is_still_live` | `ReceiptTruthfulnessTests` | pass |
| 44 | **A stale caller that resumes after the key was registered gets the idempotent result** | `test_a_stale_caller_that_resumes_after_the_key_was_registered_gets_the_idempotent_result` | `InitialIntentRaceTests` | pass |
| 45 | **A stale caller that resumes while the creator is alive creates nothing extra** | `test_a_stale_caller_that_resumes_while_the_creator_is_alive_creates_nothing_extra` | `InitialIntentRaceTests` | pass |
| 46 | **A stale caller that resumes after an uncertain attempt fails closed** | `test_a_stale_caller_that_resumes_after_an_uncertain_attempt_fails_closed` | `InitialIntentRaceTests` | pass |
| 47 | **Complete reconciliation of all partial native effects is required** | the accounted-set rule is exercised by rows 12, 14, 40 and 46 — a continuation is refused unless the exact identities account for every session the request was going to create | `CrashAndUncertaintyTests`, `InitialIntentRaceTests` | pass |

Every fixture is synthetic and disposable: a temporary fleet root, a temporary native identity
inventory, the sandbox's fake native server (with a post-launch failure mode and a malformed-answer
mode), and a real local git worktree carrying a dirty tracked file, an untracked file and an
unpublished commit. The race cases use events as barriers, so the order is chosen rather than
hoped for; the owner-PID case uses a process id that cannot exist, so the dead creator is modelled
without weakening the runtime's own liveness check.

## 3. The published defects and the open review findings (retained as failing acceptance evidence)

Each item is a failing test in the committed suite. **No production fix is implemented or copied
here**; the suggestion is the smallest change in the source owner's own paths, offered for the
owner to accept, reject or re-shape.

**Status against the owner published head `0cca1b35`, compared with `9d2bd8db`:**

| Finding | At `9d2bd8db` | At `0cca1b35` | Owner fix observed in `0cca1b3` |
| --- | --- | --- | --- |
| D1 — request body does not conform to the proved schema | repaired | **pass** (regression kept) | `session_create_payload` and the resolved-type checks in the support probe |
| D2 — an unreadable queue makes the removal barrier blind | fail | **pass** | `roster.QueueUnreadable`; the removal refuses |
| D3 — terminal queue history blocks the removal | fail | **pass** | `queued_tasks_for` skips a task that is already in the ledger |
| D4 — an import with a stale roster snapshot targets a removed worker | fail | **pass** | the roster is read again inside the writer lock; a removed worker is refused |
| D5 — a model selection loses a concurrently added worker | fail | **pass** | the selection takes the roster lock before the model lock and writes through the compare-and-swap |
| D6 — the same key creates a second native session after a takeover | fail | **pass** | the intent may only continue when the sessions it can account for cover the whole request |
| D7 — a crash in the middle of a creation is not left uncertain | fail | **pass** | the same accounted-set rule |
| D8 — a dry run mutates a receipt | fail | **pass** | the receipt reconciliation is skipped for a dry run |
| D9 — an existing empty or whitespace queue file is treated as an empty queue | — | **FAIL** | not fixed: `if not raw.strip(): return []` |
| D10 — an existing queue file without a tasks list is treated as an empty queue | — | **FAIL** | not fixed: `queue.get("tasks", [])` defaults to an empty list |
| D11 — a configuration change after the model writer's snapshot is overwritten | — | **FAIL** | not fixed: the compare-and-swap digest is read again at write time |
| D12 — the compare-and-swap does not use the digest captured with the snapshot | — | **FAIL** | the same cause as D11, observed directly |
| D13 — a late same-key request creates a second native session and overwrites the registered intent | — | **FAIL** | not fixed at `0cca1b35`: the guard reads the intent's status only when it is `creating` |
| **D14 — a late same-key request that resumes after an uncertain attempt creates a second native session and overwrites the uncertain intent** | — | — (first seen at `709dfcb`) | **FAIL at `709dfcb`** (the lock-time checks covered `registered` and `creating`, but not `uncertain`) → **pass at `20670971`**: the request is now decided under the writer lock |

**At the owner's current published head `709dfcb` every one of these passes** (§1.1): the suite is
40 tests, 40 pass, 0 fail. The table above is the record of what each finding was, so the repair
stays covered by the regression that found it.

**Nine of the twelve findings are repaired and covered by passing regressions; five cases remain
open.** D11 and D12 are one cause with two observations: the model writer reads the configuration,
then compares against a digest it reads **again** at write time, so the compare-and-swap can never
detect a change that arrived in between.

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

### D6 — the same key creates a second native session after a takeover

* **Test:** `test_a_same_key_request_never_creates_a_second_native_session_after_a_takeover`
  (class `ReviewFindingReproductionTests`)
* **Command:** `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"`
* **Observed:** `2 not less than or equal to 1` — two barrier-aligned requests that carry **the same
  request key** each performed a native creation. The intent row of that key named a creator that
  is gone (the fixture writes a pid that cannot exist), and one native session of that key was
  already receipted.
* **Why it matters:** the CLI's own contract is that the same key never adds twice. The guard that
  keeps two same-key requests apart reads the creator identity in the request's durable intent, and
  `_record_provisioning` does **not** refresh that identity when a request takes the key over
  (`ON CONFLICT(request_key) DO UPDATE SET status, updated, detail, payload_digest` leaves
  `creator_pid` alone). After a takeover the intent still names the dead creator, the guard reports
  no live attempt, and both requests create native sessions for one key.
* **Smallest owned-path suggestion** (`src/fleet/opencode_lifecycle.py`): refresh `creator_pid` in
  the `ON CONFLICT` clause, so the intent always names the attempt that currently holds the key.

### D7 — a crash in the middle of a creation is not left uncertain

* **Test:** `test_a_partial_create_crash_leaves_the_request_uncertain_instead_of_creating_again`
  (class `ReviewFindingReproductionTests`)
* **Observed:** the crashed attempt created **two** native sessions but receipted only the first
  (the crash lands between the native effect and its receipt). The retry then created a **third**
  native session instead of refusing: the assertion `a retry must not create a native session while
  the crashed attempt's outcome is unknown` fails, and no refusal is raised.
* **Why it matters:** the product's own contract is that an uncertain outcome stays uncertain and
  the next run adopts nothing on its own. The guard that enforces it only refuses when the request
  has **no** receipted session, so one receipted session masks the unknown state of the others: the
  retry proceeds, and the session the crashed attempt created without a receipt is orphaned and
  never reported.
* **Smallest owned-path suggestion** (`src/fleet/opencode_lifecycle.py`): treat a `"creating"`
  intent with a **dead** creator as uncertain regardless of how many sessions were receipted, so
  the request stops and asks for an explicit reconciliation.

### D8 — a dry run mutates a receipt

* **Test:** `test_a_dry_run_does_not_mutate_a_receipt` (class `ReviewFindingReproductionTests`)
* **Observed:** a dry run over a request whose receipt is still `unverified` changed that receipt:
  the reconciliation that runs before the plan is reported rewrites the receipt to `created` with
  the detail "reconciled: the native identity was read back". The assertion
  `a dry run must not change a receipt` fails, comparing the receipt rows before and after.
* **Why it matters:** the dry run's own contract is "A dry run writes nothing at all: no receipt,
  no roster, no native state" — and the reconciliation that precedes the plan's return commits a
  receipt change, so a read-only request is not read-only. It also happens before the request is
  refused, so a dry run can leave state behind even when it does not return a plan.
* **Smallest owned-path suggestion** (`src/fleet/opencode_lifecycle.py`): resolve the unresolved
  receipts *after* the dry-run early return, so a dry run reports the plan and the receipt state it
  found, and changes nothing.

### D9 — an existing empty or whitespace-only queue file is treated as an empty queue

* **Test:** `test_an_existing_empty_queue_file_refuses_the_removal`
  (class `QueueAndLedgerIntegrityTests`)
* **Observed:** `AssertionError: ValueError not raised` — with a queue file that exists but carries
  only whitespace, the removal **proceeded** and the worker left the roster.
* **Why it matters:** the queue file is there, so the work in it is unknown. The owner's own rule
  is "An unreadable or malformed queue is **not** an empty queue"; an existing file that carries
  nothing readable is in the same position, and the current implementation answers it with
  `if not raw.strip(): return []` — the empty answer — before the malformed check is reached.
* **Smallest owned-path suggestion** (`src/fleet/roster.py`): treat an existing but empty or
  whitespace-only file as unreadable, and keep only the **absent** file as the empty case, so the
  two are no longer answered the same way.

### D10 — an existing queue file without a tasks list is treated as an empty queue

* **Test:** `test_an_existing_queue_file_without_a_tasks_list_refuses_the_removal`
  (class `QueueAndLedgerIntegrityTests`)
* **Observed:** `AssertionError: unexpectedly None` — the removal **proceeded**. The check
  `not isinstance(queue.get("tasks", []), list)` cannot see the difference, because a missing
  `tasks` key defaults to an empty **list**, which is a list.
* **Why it matters:** a queue file that names no task list cannot be read; the work in it is
  unknown, and the removal must refuse rather than report that nothing is waiting.
* **Smallest owned-path suggestion** (`src/fleet/roster.py`): require the `tasks` key to be present
  and to be a list, instead of defaulting it.

### D11 — a configuration change after the model writer's snapshot is overwritten

* **Test:** `test_an_out_of_band_config_change_after_the_snapshot_is_rejected`
  (class `BarrierRaceTests`)
* **Observed:** `AssertionError: ValueError not raised` — the model writer read the configuration,
  another writer changed it, and the model writer's write **overwrote the change** instead of being
  refused. The compare-and-swap did not fire.
* **Why it matters:** the roster is the identity source of the fleet. A write that silently
  discards a configuration change made in between loses whatever that change carried.
* **Smallest owned-path suggestion** (`src/fleet/model_selection.py`): capture the configuration's
  digest **together with the configuration** it read, and hand that digest to
  `roster.write_config_cas` — not a digest read again at write time.

### D12 — the compare-and-swap does not use the digest captured with the snapshot

* **Test:** `test_the_compare_and_swap_uses_the_digest_captured_with_the_snapshot`
  (class `BarrierRaceTests`)
* **Observed:** `AssertionError: ValueError not raised`. The digest handed to
  `roster.write_config_cas` is the digest of the configuration **as it is at write time**, not the
  one captured with the snapshot, so the comparison always matches and the compare-and-swap is
  inert against any change that arrived in between.
* **Why it matters:** this is the mechanism behind D11, observed directly: the guard exists and is
  called, but it is given a digest that cannot disagree with the file it is about to write.
* **Smallest owned-path suggestion** (`src/fleet/model_selection.py`): the same one — capture the
  digest with the snapshot and pass that digest to the compare-and-swap.

### D13 — a late same-key request creates a second native session and overwrites the registered intent

* **Test:** `test_a_concurrent_same_key_request_creates_one_session_and_keeps_the_final_intent`
  (class `BarrierRaceTests`)
* **Observed:** `AssertionError: 2 != 1 : one key must create at most one native session` — with
  the interleaving forced (the late request computes what it still needs before the first request
  has registered anything, and reaches the writer lock after the first request has finished), **two
  native sessions were created for one request key**. The late request's intent write also moves the
  key's intent from `registered` back to `creating`, which is the property the owner's
  `keep_registered` clause was added for.
* **Why it matters:** the command's own contract is that the same key never adds twice. The guard
  that enforces it refuses a request only while the intent says `creating` **and** names a live
  creator; a request that arrives after the key was registered passes the guard, creates a second
  native session, and then loses the compare-and-swap. That second native session exists in the
  native inventory, is not in the roster, and is reported by neither request.
* **Smallest owned-path suggestion** (`src/fleet/opencode_lifecycle.py`): re-read the request's
  receipts and its intent **inside** the writer lock, immediately before the creation, and refuse a
  request whose key is already registered — so the number of sessions to create is decided under the
  lock rather than before it.

### D14 — a late same-key request that resumes after an uncertain attempt creates a second native session

* **Test:** `test_a_stale_caller_that_resumes_after_an_uncertain_attempt_fails_closed`
  (class `InitialIntentRaceTests`)
* **Observed:** `AssertionError: 2 != 1 : an uncertain attempt must not be followed by a second
  native session`. The late caller snapshots what it still needs before the first caller has
  written anything, the first caller's native identity then cannot be read (its attempt is
  **uncertain**), and the late caller resumes: it **creates a second native session** and its
  intent write replaces the first caller's `uncertain` state.
* **Why it matters:** an uncertain outcome must stay uncertain — "absence is no proof" is the
  product's own rule. The guard that enforces it (line 767) reads `existing`, the intent row as the
  caller saw it **at the start of its own request**, not the durable row. A late caller whose
  snapshot predates the other caller's write sees no intent at all and skips the guard. The
  lock-time checks (lines 827–847) read the durable row `current` and cover a different digest, a
  `registered` row and a `creating` row with a live creator — but **not** an `uncertain` row, so
  nothing stops the late caller: it records `creating` over the uncertain state and creates.
* **Smallest owned-path suggestion** (`src/fleet/opencode_lifecycle.py`): evaluate the uncertain
  state from the row read **inside** the writer lock (`current`), not from the caller's early
  snapshot (`existing`), and refuse a request whose key's durable intent says `uncertain` unless
  the operator reconciles it explicitly.

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
* **A dead creator is modelled, not reaped.** The "the attempt's process is gone" precondition is
  expressed by a process id that cannot exist on this host, and in one case by telling the runtime's
  own liveness check that the creator is gone — an in-process crash cannot make its own process
  disappear. The runtime's liveness check itself is not re-implemented and not weakened.
* **The pre-existing intent is seeded, not produced by a full crash chain.** The same-key takeover
  (D6) and the unresolved receipt (D8) are reproduced by writing the durable intent and the
  receipt the way an earlier attempt leaves them, using the module's own key and digest functions
  so the fixture cannot drift from the rule it exercises. The full chain that produces such a row
  is the owner's to exercise.
* **Single-process concurrency.** The race cases use threads inside one process; the file locks are
  advisory and would behave the same across processes, but that is not exercised.
* **The recovery receipts are checked as evidence, not as a recovery.** `verify_stopped_native_execution`
  is called with synthetic receipts; the surrounding recovery flow (authorization, adoption,
  reconciliation) is out of scope.

## 6. Commands and results

| Command | Scope | Result at `8ef01717` |
| --- | --- | --- |
| `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"` | **this suite** | **47 tests: 47 pass, 0 fail** — 32.8 s, every fixture in a temporary directory |
| `python3 -m unittest tests.test_lifecycle_qualification_independent.ExactHeadInvariantTests tests.test_lifecycle_qualification_independent.InitialIntentRaceTests tests.test_lifecycle_qualification_independent.QueueAndLedgerIntegrityTests tests.test_lifecycle_qualification_independent.BarrierRaceTests` | the focused groups for this task's invariants | **24 tests, 0 fail** — 17.1 s |
| `python3 -m py_compile tests/test_lifecycle_qualification_independent.py` | the owned test file | pass |
| `git diff --name-status 8ef01717...codex/20261021-w8-independent-lifecycle-qualification-at-8ef01717` | the owned delta | two new paths only; 0 owner paths touched |
| **No broad aggregate run and no whole-repository run** | this task ran only bounded focused synthetic tests. The owner's own suite at this head was **not** measured, so its baseline here is **unknown** — see §7 | — |

### 6.1 The earlier heads — historical evidence, kept separate

| Head | Owner commit | Suite | Branch |
| --- | --- | --- | --- |
| `0cca1b35` | fix(fleet): close the seven reviewed lifecycle findings | 35 pass / 5 fail | `…-at-0cca1b35` @ `37a47e1` |
| `709dfcb` | fix(fleet): hold the queue evidence, the snapshot and the stale caller | 42 pass / 1 fail (D14) | `…-at-709dfcb` @ `e75e6ce` |
| `20670971` | fix(fleet): decide the request under the writer lock | 43 pass / 0 fail | `…-at-20670971` @ `fafdf5b` |
| **`8ef01717`** | **fix(fleet): treat an ambiguous native effect as uncertain, and keep the receipt monotonic** | **47 pass / 0 fail** | `…-at-8ef01717` |

Those results are retained on their own branches with their own patches and logs. **They are
evidence for the head they were run on and not for this one**: every row of the matrix in §2 was
re-run against `8ef01717`.

## 7. Exact remaining gaps

1. **This qualification's suite is green at the exact head `8ef01717`** — 47 tests, 47 pass, 0 fail
   (§6). Every invariant in the matrix is covered by a regression that passes on this head.
2. **The owner's own suite at this head is not measured.** This task ran only bounded focused
   synthetic tests, so the repository's own state here is **unknown**. The last measurement (543
   tests at `709dfcb`, with 30 errors in the owner's recovery, queued-holds and fleet fixtures) is
   historical evidence for that head; the owner has since rewritten `opencode_lifecycle.py` and
   added `tests/test_ambiguous_effects.py` and a new `tests/test_lifecycle_findings.py`, so the
   baseline at this head must be measured before any merge. **Unverified and pristine baseline
   failures are kept distinct from this suite's results.**
3. **The pull request's draft and auto-merge state is unknown from any local record** (§1); the
   GitHub API is not reachable from this worker.
4. **Publication is blocked in this worker**: no GitHub authorization; no credential was probed or
   changed and no alternate route was used.
5. **The native and production limits in §5 are not closed by this suite** — no real CLI, native
   database, provider or turn; no live manager, fleet, configuration, session, worktree or
   resource; no Docker; the crash windows are injected and the dead creator is modelled with a
   process id that cannot exist; the out-of-band configuration change is written by the test
   itself, bypassing the writer lock; the head could not be re-fetched from the remote here.
6. **Root review of the two owned paths is separate** and has not happened.

## 8. Focused finding at `8ef01717` — the receipt-transition guard

This section is **additive**. The completed exact-head result in §6 — 47 tests, 47 pass — stands as
recorded; this is a narrowly scoped addition for the finding root raised against that head, and it
does not re-run or restate the matrix.

**Target:** the immutable head `8ef01717d9810196a5d84416be8c54386812814e`, the same branch and
worktree as §6 (`codex/20261021-w8-independent-lifecycle-qualification-at-8ef01717`).

**Root's finding.** The `registered` guard appears to reject the lifecycle's own transitions: a
`registered` receipt can only be updated to `registered`, so the writes that record a worker as
`draining` and as `removed` are refused, while the roster mutation that removed the worker goes
through. Root's inspection of the guard and the drain/remove callers is confirmed by the source and
by the three tests below.

### 8.1 The mechanism, read from the source

`_record_session` (line 526) writes the receipt with

```
ON CONFLICT(session_id) DO UPDATE SET status=excluded.status, updated=excluded.updated,
 detail=excluded.detail
 WHERE opencode_sessions.status<>'registered' OR excluded.status='registered'
```

An existing `registered` receipt therefore accepts only another `registered` write. The two
callers that move a worker on are exactly the two it refuses:

* `drain_session` (line 1140) writes `("draining", "draining")` — refused;
* `_remove_session` (line 1249) writes `("removed", "removed from the roster")` — refused.

`_record_session_locked` (line 555) makes the same decision explicitly and returns `"registered"`
without writing. Neither call is an error: the roster write is a separate statement and it
proceeds.

### 8.2 The three focused tests

| Test | What it asserts | Result at `8ef01717` |
| --- | --- | --- |
| `test_the_receipt_follows_the_worker_from_registered_to_draining_to_removed` | the add → `registered` → drain → remove lifecycle, and the receipt records `draining` and then `removed` | **FAIL** — `AssertionError: 'registered' != 'draining'` |
| `test_a_same_key_retry_after_a_removal_never_reports_a_worker_that_is_gone` | a same-key retry after the removal must not report a worker that is not in the roster, and its counts must agree with the sessions it lists | **FAIL** — `AssertionError: 'ses_NEW000000000000000-2' not found in ['ses_EXISTING000000000001']` |
| `test_a_stale_write_does_not_downgrade_a_registered_receipt` | the guard's legitimate case: a stale `created` or `unverified` write must not un-register a session | **pass** |

The third test is the distinction the finding turns on: the guard's stated purpose holds, and what
fails is the lifecycle's own transitions.

### 8.3 Why it matters

The receipt is the durable record of what happened to a worker. After a drain the receipt still
says `registered`; after a removal it still says `registered` while the worker is gone from the
roster. A later same-key retry then reports that removed worker as registered — a result that
disagrees with the roster, which is the property the status report is supposed to guarantee. The
failure is silent: neither the drain nor the removal reports an error, and the roster mutation
succeeds.

### 8.4 The smallest suggestion (owner's path)

`src/fleet/opencode_lifecycle.py` — let the guard refuse only the writes it is meant to refuse, and
let the lifecycle's own forward transitions through. Either

* admit a transition out of `registered` when the new state is a later state of the same lifecycle
  (`draining`, `removed`) and refuse only a downgrade to `created` or `unverified`; or
* have the drain and the removal record their transition with a statement that is not subject to
  the monotonic guard, and keep the guard for the reconciliation writes it protects.

The receipt's own transition order — `created` → `registered` → `draining` → `removed` — is the
natural rule; the guard as written forbids two of those three steps.

### 8.5 The limits of this focused addition

It is three tests over synthetic temporary state, on one head, for one property. It does not re-run
the matrix, does not measure the owner's own suite, and does not exercise the native server, a
provider, a live manager, a live fleet, a real session or a real worktree. The whole suite at this
head is now **50 tests: 48 pass, 2 fail** — the 47 tests of the completed exact-head result all
still pass, and the two failures are the finding above.
