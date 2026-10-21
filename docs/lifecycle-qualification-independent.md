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
| Repository | `/Users/amf/Developer/.codex/fleet` (remote `0xPlayerOne/fleet`) |
| **Qualification base** | **`0cca1b35fa76fccde3bc8d11a6dd9bdfe6838462`** — "fix(fleet): close the seven reviewed lifecycle findings" — the published PR #8 head; the remote-tracking ref `origin/feat/opencode-session-lifecycle-add-remove` names exactly this commit |
| **Qualification branch** | `codex/20261021-w8-independent-lifecycle-qualification-at-0cca1b35` |
| **Qualification worktree** | `/Users/amf/Developer/.worktrees/fleet-lifecycle-qualification-w8-at-0cca1b35` — created clean from that commit |
| **Previous base (the comparison)** | `9d2bd8db9432fe51c3848c8e143c50f3272be662` — the head the previous report ran on; every invariant is compared against it (§3) |
| **Previous branch (preserved, untouched)** | `codex/20261021-w8-independent-lifecycle-qualification` @ `6e3454ffd632bb1177821d5b4a08d93d82c5de0b` — the four earlier commits, the two artifacts and every earlier receipt are left exactly as they were |
| Carry-forward | the four W8 commits were carried onto the new base with `git cherry-pick` (`927f855`, `f7eca20`, `b6d6de3`, `6e3454ff` → `b10a617`, `88693b7`, `bc0de8a`, `1b34baa`). They touch **only** the two owned paths, so nothing of W5's source is rewritten or duplicated |
| Owned delta against the base | exactly two new paths: `tests/test_lifecycle_qualification_independent.py`, `docs/lifecycle-qualification-independent.md` |
| W5's branch and worktree | **untouched.** `feat/opencode-session-lifecycle-add-remove` still names `0cca1b35`; their worktree's uncommitted tree was not read, copied, merged or stashed — only the fact that it carries uncommitted work was observed |
| Operations not performed | no fetch, no pull, no reset, no stash, no prune, no rebase of the previous branch, no mutation of W5's branch, no publication |

The base moved because the owner published the repair of the seven reviewed findings. This
qualification therefore starts a **separate clean worktree and branch from exactly that commit** and
carries its own artifacts forward, so the previous branch, its commits and its receipts stay
intact and independently checkable.

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
| **The same key never creates a second native session after a takeover** | `test_a_same_key_request_never_creates_a_second_native_session_after_a_takeover` | barrier | **FAIL (D6)** |
| **A partial-create crash leaves the request uncertain instead of creating again** | `test_a_partial_create_crash_leaves_the_request_uncertain_instead_of_creating_again` | fault injection | **FAIL (D7)** |
| **A dry run does not mutate a receipt** | `test_a_dry_run_does_not_mutate_a_receipt` | fault injection | **FAIL (D8)** |
| **An existing empty or whitespace-only queue file refuses the removal** | `test_an_existing_empty_queue_file_refuses_the_removal` | fault injection | **FAIL (D9)** |
| **An existing queue file without a tasks list refuses the removal** | `test_an_existing_queue_file_without_a_tasks_list_refuses_the_removal` | fault injection | **FAIL (D10)** |
| **A truncated queue file refuses the removal** | `test_a_truncated_queue_file_refuses_the_removal` | fault injection | pass |
| **A missing queue file is distinguished from an unreadable one** | `test_a_missing_queue_file_is_distinguished_from_an_unreadable_one` | fault injection | pass |
| **An out-of-band configuration change after the snapshot is rejected** | `test_an_out_of_band_config_change_after_the_snapshot_is_rejected` | fault injection | **FAIL (D11)** |
| **The compare-and-swap uses the digest captured with the snapshot** | `test_the_compare_and_swap_uses_the_digest_captured_with_the_snapshot` | fault injection | **FAIL (D12)** |
| **A concurrent same-key request registers the worker exactly once** | `test_a_concurrent_same_key_request_registers_the_worker_exactly_once` | barrier | pass |
| **A final intent never overwrites a registered request** | `test_a_final_intent_never_overwrites_a_registered_request` | — | pass |
| The status report counts the roster and the receipts truthfully | `test_the_status_report_counts_the_roster_and_the_receipts_truthfully` | — | pass |
| **A recovery receipt refuses a turn whose cleanup is not confirmed** | `test_a_recovery_receipt_refuses_a_turn_whose_cleanup_is_not_confirmed` | fault injection | pass |
| **A recovery receipt refuses a turn whose process is still live** | `test_a_recovery_receipt_refuses_a_turn_whose_process_is_still_live` | fault injection | pass |

Barrier cases use `threading.Barrier(2)` so the two writers meet at a known point instead of
depending on scheduling; the fault injections replace the native call, the identity row and the
ledger with controlled failures. The suite asserts, in every case, that **no duplicate native
call** was made, that **no queue entry was lost**, that **no unsafe registration or removal**
happened, and that the reported status matches what actually occurred.

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
| D13 — a late same-key request creates a second native session and overwrites the registered intent | — | **FAIL** | not fixed: the guard reads the intent's status only when it is `creating` |

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

| Command | Scope | Result |
| --- | --- | --- |
| `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"` | **this suite at the published head `0cca1b35`** | **40 tests: 35 pass, 5 fail** — D9, D10, D11, D12, D13. Identical on two consecutive runs (the concurrent case forces its interleaving) |
| `python3 -m unittest discover -s tests -t . -p "test_[!l]*.py"` | the same repository **without** this file | **475 tests: 30 errors, 4 skipped, 0 failures** — the owner's own state at this head (§6.1) |
| `python3 -m unittest discover -s tests -t .` | the whole repository at `0cca1b35`, including this suite | **529 tests: 5 failures, 30 errors, 4 skipped** — the 5 failures are this suite's retained evidence, the 30 errors are the owner's (§6.1) |
| `python3 -m unittest discover -s tests -t . -p "test_lifecycle_qualification_independent.py"` | the previous base `9d2bd8db` (the comparison) | 32 tests: 25 pass, 6 assertion failures, 1 error — D2–D8 |
| `python3 -m unittest discover -s tests -t . -p "test_[!l]*.py"` | the previous base `9d2bd8db`, without this file | 475 tests: 7 failures |
| `python3 -m py_compile tests/test_lifecycle_qualification_independent.py` | the owned test file | pass |
| `git diff --stat 0cca1b35...codex/20261021-w8-independent-lifecycle-qualification-at-0cca1b35` | the owned delta against the published head | two new paths only |

### 6.1 The owner's own suite at `0cca1b35` — a regression, preserved as a failure

The owner's suite, **without** this qualification, is **475 tests with 30 errors** at this head
(0 failures; the seven `tests/test_queue_runway.py` failures of the previous head are gone). The
thirty errors are all one cause: the new `manager.import_queue` reads the roster with
`paths.all_slots()`, which reads the configuration file, and the recovery, queued-holds and fleet
fixtures do not have one — so `import_queue` raises `FileNotFoundError` before it can do anything:

```
File ".../src/fleet/manager.py", line 120, in import_queue
    current = {slot["sessionId"] for slot in paths.all_slots()}
File ".../src/fleet/paths.py", line 43, in slots
    cfg = config() if cfg is None else cfg
File ".../src/fleet/paths.py", line 27, in config
    return json.loads(Path(path or CONFIG).read_text())
FileNotFoundError: .../config/fleet.local.json
```

`paths.all_slots()` has a guard for exactly this case ("Legacy unit fixtures often replace the
provider pair without creating a config file"), but it is unreachable: `slots(cfg)` is called
before the guard, and it is `slots()` that reads the file. **This qualification does not touch the
owner's tests or the owner's source; the failures are preserved and reported here for root.**

### 6.2 Per-test receipt at the published head `0cca1b35`

| # | Test | Class | Result |
| --- | --- | --- | --- |
| 1 | `test_the_native_call_carries_only_the_model_and_the_directory` | `NativeEffectTests` | pass |
| 2 | `test_the_created_session_is_read_back_from_the_synthetic_identity_row` | `NativeEffectTests` | pass |
| 3 | `test_the_native_request_body_conforms_to_the_schema_the_runtime_proved` | `NativeEffectTests` | pass (D1, repaired) |
| 4 | `test_a_duplicate_native_identity_is_refused_and_the_roster_stays_exact` | `NativeEffectTests` | pass |
| 5 | `test_the_registration_proof_counts_distinct_sessions_exactly` | `NativeEffectTests` | pass |
| 6 | `test_a_dry_run_makes_no_native_call_and_writes_no_receipt` | `DryRunTests` | pass |
| 7 | `test_a_dry_run_of_a_target_total_writes_nothing` | `DryRunTests` | pass |
| 8 | `test_the_same_key_with_a_different_payload_is_refused_before_any_native_call` | `RequestKeyBindingTests` | pass |
| 9 | `test_the_same_key_with_the_same_payload_is_idempotent` | `RequestKeyBindingTests` | pass |
| 10 | `test_a_crash_between_the_native_effect_and_its_receipt_creates_no_second_session` | `CrashAndUncertaintyTests` | pass |
| 11 | `test_a_crashed_creator_leaves_the_request_uncertain_and_not_retryable` | `CrashAndUncertaintyTests` | pass |
| 12 | `test_an_explicitly_named_session_is_registered_without_a_second_creation` | `CrashAndUncertaintyTests` | pass |
| 13 | `test_a_native_effect_that_cannot_be_read_back_stays_uncertain` | `CrashAndUncertaintyTests` | pass |
| 14 | `test_a_malformed_queue_file_does_not_let_a_removal_pass_unseen` | `QueueAndLedgerIntegrityTests` | pass (D2, repaired) |
| 15 | `test_an_unreadable_task_ledger_refuses_the_removal_and_keeps_the_roster` | `QueueAndLedgerIntegrityTests` | pass |
| 16 | `test_a_terminal_task_in_the_queue_history_does_not_block_the_removal` | `QueueAndLedgerIntegrityTests` | pass (D3, repaired) |
| 17 | `test_a_queue_entry_that_names_the_worker_is_never_lost_by_a_drain` | `QueueAndLedgerIntegrityTests` | pass |
| 18 | `test_an_existing_empty_queue_file_refuses_the_removal` | `QueueAndLedgerIntegrityTests` | **FAIL (D9)** |
| 19 | `test_an_existing_queue_file_without_a_tasks_list_refuses_the_removal` | `QueueAndLedgerIntegrityTests` | **FAIL (D10)** |
| 20 | `test_a_truncated_queue_file_refuses_the_removal` | `QueueAndLedgerIntegrityTests` | pass |
| 21 | `test_a_missing_queue_file_is_distinguished_from_an_unreadable_one` | `QueueAndLedgerIntegrityTests` | pass |
| 22 | `test_a_running_worker_is_never_removed` | `RemovalBarrierTests` | pass |
| 23 | `test_a_removal_keeps_the_native_session_and_every_hold` | `RemovalBarrierTests` | pass |
| 24 | `test_a_removal_leaves_a_dirty_unpublished_worktree_byte_identical` | `RemovalBarrierTests` | pass |
| 25 | `test_an_active_worker_with_unpublished_work_is_never_removed` | `RemovalBarrierTests` | pass |
| 26 | `test_two_barrier_aligned_requests_with_one_key_make_exactly_one_native_call` | `BarrierRaceTests` | pass |
| 27 | `test_two_barrier_aligned_requests_with_one_key_and_different_payloads_conflict` | `BarrierRaceTests` | pass |
| 28 | `test_a_drain_and_a_removal_of_one_worker_cannot_both_take_effect` | `BarrierRaceTests` | pass |
| 29 | `test_an_import_with_a_stale_roster_snapshot_never_binds_a_task_to_a_removed_worker` | `BarrierRaceTests` | pass (D4, repaired) |
| 30 | `test_a_model_selection_write_never_loses_a_roster_entry` | `BarrierRaceTests` | pass (D5, repaired) |
| 31 | `test_an_out_of_band_config_change_after_the_snapshot_is_rejected` | `BarrierRaceTests` | **FAIL (D11)** |
| 32 | `test_the_compare_and_swap_uses_the_digest_captured_with_the_snapshot` | `BarrierRaceTests` | **FAIL (D12)** |
| 33 | `test_a_concurrent_same_key_request_creates_one_session_and_keeps_the_final_intent` | `BarrierRaceTests` | **FAIL (D13)** |
| 34 | `test_a_same_key_request_never_creates_a_second_native_session_after_a_takeover` | `ReviewFindingReproductionTests` | pass (D6, repaired) |
| 35 | `test_a_partial_create_crash_leaves_the_request_uncertain_instead_of_creating_again` | `ReviewFindingReproductionTests` | pass (D7, repaired) |
| 36 | `test_a_dry_run_does_not_mutate_a_receipt` | `ReviewFindingReproductionTests` | pass (D8, repaired) |
| 37 | `test_the_status_report_counts_the_roster_and_the_receipts_truthfully` | `ReceiptTruthfulnessTests` | pass |
| 38 | `test_a_recovery_receipt_refuses_a_turn_whose_cleanup_is_not_confirmed` | `ReceiptTruthfulnessTests` | pass |
| 39 | `test_a_recovery_receipt_refuses_a_turn_whose_process_is_still_live` | `ReceiptTruthfulnessTests` | pass |

The repository has no separate lint or format tool (no CI workflow, no packaging configuration);
the documented check is the unittest discovery above, plus a byte-compile of the owned file. The
owned file follows the repository's style (module docstring, `unittest.TestCase` classes, no
third-party imports, the same source-layout path bootstrap as the repository's own tests) and stays
within 100 columns.

## 7. Exact remaining gaps

1. **The suite does not pass on the published head.** Five invariants do not pass at `0cca1b35` —
   D9, D10, D11, D12 and D13 (§3). Until the source owner repairs them — or records why the
   invariant is wrong — this branch cannot be qualified.
2. **The published head is not green in the owner's own suite either.** Without this qualification,
   the repository's own suite is **475 tests with 30 errors** at `0cca1b35` (§6.1): the new
   `manager.import_queue` reads the configuration through `paths.all_slots()`, whose legacy
   no-config-file guard is unreachable, so every fixture without a configuration file fails before
   the import can run. Those errors are **preserved as failures** and are the owner's to fix; this
   qualification changed nothing of theirs.
3. **Nine of the twelve findings are repaired** and are now covered by passing regressions, so the
   repairs stay protected. D1–D8's repairs are all covered by tests that remain in the suite.
4. **D11 and D12 are one cause**, and D13 is a second window in the same area: the runtime decides
   what a request still needs *before* it takes the writer lock, so a late same-key request can
   create a second native session and move a registered intent back to `creating`.
5. **The native and production limits in §5 are not closed by this suite.** Closing them needs a
   disposable native CLI and a disposable fleet, which are outside this outcome's scope and outside
   its authority. In particular the D13 result is observed against the **fake** native server: the
   second native session is real in the synthetic inventory, and nothing here says what a real
   server would do with it.
6. **The out-of-band change in D11 and D12 is written by the test itself**, bypassing the writer
   lock, because that is what "out of band" means. A change that respects the lock cannot reach
   that window, so the two cases describe the same exposure from two directions.
7. **The published head was not re-fetched.** This worker has no GitHub authorization, so
   `git ls-remote` fails; the head this qualification ran against is the commit `0cca1b35` itself,
   and the local remote-tracking ref `origin/feat/opencode-session-lifecycle-add-remove` names it.
8. **The two owned paths are the whole delta.** No Fleet runtime, CLI, roster, existing test or
   documentation file was modified, W5's branch and worktree were not touched, and no live manager,
   session, configuration, registered worktree or resource was changed.
