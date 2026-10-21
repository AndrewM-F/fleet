# Credential isolation contract — source-only, not ready

No supported enforcing backend has been established on Andrew's Mac. This
packet supplies a fail-closed trust contract and synthetic tests, not OS
confinement. The production backend registry is deliberately empty. Applying
this branch would block new OpenCode prompts/launches; it must not be activated
on a running fleet without separate review and approval. No policy, credentials,
security settings, accounts, installed launcher, or manager were changed.

Fresh HOME/XDG directories, environment stripping, command regexes, native
permission declarations, and worker-written `enforced: true` JSON are not a
security boundary. They cannot stop an agent from using absolute paths,
credential-helper IPC, Keychain, inherited descriptors, or descendants. Existing
runtime environment hygiene remains useful but supplies no confinement proof.

## Version 2 launch contract and version 1 policy

The administrator-controlled Fleet configuration may eventually contain a
`credentialIsolation` object. This branch does not add one. Configuration never
imports a backend, accepts an arbitrary executable as a backend, or expands the
source-reviewed backend registry.

Required fields are `contractVersion: 2`, `policyVersion: 1`, `backendId`,
`backendVersion`, `executableVersion`, `executableSha256`, `allowedPaths`,
`runtimeRoot`, `logRoot`, `deniedResources`, and `policyDigest`. The digest is
SHA256 over UTF-8 JSON of all policy fields except `policyDigest`, with sorted
keys and compact separators. Unknown/stale versions, unsupported backends,
wrong digests, missing fields, relative paths and noncanonical aliases refuse.

The policy must explicitly cover host credential stores, Keychain,
credential-helper IPC, inherited credential descriptors, credential environment,
and escape through descendants. The exact canonical worktree, native session
working directory, native database, and executable must be on the path
allowlist. Per-turn runtime/log paths must be within their canonical configured
roots. All of these paths are included in the immutable binding, along with the
policy digest, executable bytes hash and version, backend ID/version, session,
Fleet task, turn, model, and launcher-generated launch instance. For recovery,
the exact `recoveryId`, canonical `ownedWorktree`, and branch are also bound.
The owned checkout needs its own explicit allowlist entry even when it differs
from the fixed native session directory. Partial recovery identities and aliases
are refused. These fields are re-read and re-attested at launch.

An enforcing backend must interpret these as actual mandatory resource
restrictions. Names in `deniedResources` alone prove nothing. In particular,
allowing a native database must not let worker tools extract provider secrets
through that database, its containing directory, a helper, or a privileged
broker. The eventual architecture must establish that separation, including
broker capabilities and permitted provider access, before readiness is claimed.

## Trusted backend interface

A backend is trusted launcher code installed/registered outside worker control,
with a pinned `version`. It implements:

- `attest(binding, stage)`: return fresh `Evidence` for `pre-prompt` or `launch`.
- `verify(evidence, binding, stage, process_id)`: authenticate the proof against
  a backend-owned trust anchor and independently measured executable/version,
  effective enforcement policy, canonical paths, and launch ownership. Returning
  true merely because fields agree is forbidden.
- `launch(binding, argv, **popen_options)`: atomically apply the measured policy
  and spawn the exact bound executable. Return the process handle, a unique containment-domain ID, and signed or
  equivalently authenticated `process` evidence bound to its PID and domain. It must not
  fall back to unsandboxed spawning.
- `abort(process, binding, containment_domain)`: contain and stop the entire launch/descendant tree,
  then return authenticated `cleanup` evidence for that PID and domain. The
  proof must certify that the complete launch domain is empty and cannot admit
  new descendants, including after the root process exits normally. If it cannot prove
  cleanup, the outcome remains uncertain.

Evidence carries contract/backend versions, policy digest, complete binding
digest, stage, issue/expiry times, process ID and containment-domain ID when applicable, and opaque proof
bytes. Fleet rejects evidence older than 30 seconds, future-issued or expired
proofs, overlong validity, mismatched identity/stage/PID, and unauthenticated
proofs. Evidence must arrive over a backend-owned protected channel. Native
stdout/events, worker JSON, environment values and worker-writable receipt files
are never authoritative channels.

The backend must reserve the launcher-generated instance for the exact
pre-prompt binding and atomically consume that reservation once at launch.
Another session, task, turn, instance or binding cannot reuse it. Launch must
re-measure the opened executable and policy atomically with enforcement/spawn,
so path replacement between Python's preliminary hash and execution cannot
invalidate the proof. The proof for the child must cover actual effective
confinement and descendants, not just a requested policy or an unverified PID.
A backend that cannot provide these properties cannot be registered.

## Fleet gates and recovery

`send()` preserves the existing dispatch, native identity/model, recovery and
session-lock gates. It obtains fresh enforcing evidence before writing the
prompt request or starting the wrapper. The wrapper independently reloads the
trusted policy and rechecks before native child creation. Native child creation
is delegated exclusively to the enforcing backend; fresh child evidence is
required before Fleet sends prompt bytes to stdin. No request-carried evidence
or boolean is trusted. A prior successful pre-prompt check does not authorize a
stale or changed launch.

A backend launch exception may hide a committed effect. It is reported as
blocked with cleanup uncertain; no fallback or automatic retry occurs. Bad
child evidence triggers backend teardown, and cleanup is confirmed only after
fresh authenticated cleanup evidence. A frozen `LaunchHandle` retains the
original backend, immutable binding, process and domain for every terminal path.
Normal completion, cancellation, timeout, fatal errors and post-launch identity
failures all finish through this handle. Results are persisted only after this
domain proof; unverified cleanup forces blocked/uncertain status and result.
Host PID/PGID checks and signals are supplementary, never domain proof. Backend
ownership must also be durable across wrapper crashes; an absent terminal proof
cannot authorize a retry. The existing ownership, quota, model,
cancellation, process identity and cleanup checks remain in place. Read-only
status and capability reporting do not require an isolation backend.

## Evidence and remaining work

`tests/test_credential_isolation.py` uses an in-process fake authority, synthetic
files and fake process handles only. It exercises refusal paths, binding,
evidence freshness, policy/executable changes, both integration gates, and
ambiguous launch/cleanup. Existing transport/recovery unit fixtures mock the new
trusted boundary so their prior lifecycle assertions continue to run; those
mocks are not confinement qualification.

Before any activation, a separately approved implementation must establish a
supported enforcing backend and its protected trust channel, one-use launch
reservations, executable/policy measurement, descriptor and IPC mediation,
credential broker separation, and verified descendant cleanup. Then root must
review the exact head and authorize appropriate live confinement qualification.
This task performs no actual OpenCode/API/credential/Keychain probe and provides
no evidence that host credentials are inaccessible to a live worker.

Initial contract-v1 qualification: 141 focused contract/adapter/recovery
tests passed, then the full suite completed 564 tests with four deployment-only
skips (no local configuration in this source-only checkout). The verified pinned
installation was supplied explicitly for the synthetic manager checks. Changed
files pass Ruff lint; the new contract/test files pass Ruff formatting;
compileall and diff checks pass. Inherited files retain pre-existing formatting
debt. Initial fresh-worktree fixture configuration failures were isolated and
fixed before the passing full run. These results prove source behavior only.

Root review follow-up at prior head `287f72e`: contract version 2 closes two
future-backend activation blockers: recovery checkout/identity binding and
terminal containment-domain cleanup. The registry remains empty; these were
not exploitable live-backend bypass claims. Fake-backend regressions cover a
distinct native/owned checkout, missing permission, aliases, changed recovery
targets between gates, and unverified domain cleanup after host exit on success,
cancellation, timeout, fatal error and post-launch identity failure.

Contract-v2 qualification: 26 contract/integration regressions passed, and the
full current-source suite completed 575 tests with the same four deployment-only
skips. Changed-file Ruff lint, new module/test formatting, compileall and diff
checks pass. The exact pinned source was supplied for synthetic manager checks.
No enforcing backend or live confinement was tested.
