# Pinned Stage 2 development inputs

`acqivo/` contains byte-for-byte files read with `git show` from Acqivo commit
`7eafeeb63e6291f85e7660447740658a3771ace5`, plus the generated MANIFEST.json.
The manifest identifies the commit, source paths, compliance policy and every
source-file SHA-256. Tests require no access to the source checkout.

`load_stage2_inputs` is the runtime mapping used by the required-path tests.
It preserves original cases and timestamps, derives consent eligibility from
matching identity/address/method and current evidence, and ingests provider
signals through SyntheticWorld. Missing consent stays ineligible; missing
required input fields raise rather than receiving test-generated replacements.

Canonical queue records identify their origin action; their provider key is
unknown until dispatch computes it from persisted workflow identity. Canonical
failure injection is likewise matched by workflow/entity/action immediately
before execution and armed against the actual runtime key. Neither depends on
hard-coded database IDs or test-side queue metadata.

Scripted workflow decisions, explicit transition requirement evidence, founder
approval variants, and negative-test world mutations remain test scenarios.
They do not alter the canonical input snapshots and are not a scoreable run.
The canonical graphs have no repeated outbound action in the same instance;
the focused B-II test retains the proof that a later action gets a new key.
