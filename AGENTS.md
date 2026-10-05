# Soundloom — Project Development Instructions

These instructions govern how work is done in this repository. They are binding on
human and AI-assisted contributions alike. Where a convenience change conflicts with a
rule below, the rule wins.

## Core Purpose

Soundloom exists to make a personal music library **trustworthy, self-maintaining, and understandable**.

The primary goal is not to accumulate features. The primary goal is that Soundloom can reliably answer:

* What music do I have?
* Where is it?
* What exactly is the file?
* Where did it come from?
* Has it changed?
* Was it moved, deleted, replaced, or newly acquired?
* Can Soundloom safely repair or organize it without destroying user data?

Every feature should strengthen those guarantees before adding convenience features.

## Engineering Priority

Work in this order:

1. **Data integrity**
2. **Filesystem truth**
3. **Acquisition reliability**
4. **Provenance and identity**
5. **Safe organization and repair**
6. **User-visible diagnostics**
7. **Automation**
8. **Convenience features**

Do not prioritize UI polish, integrations, duplicate detection, or new acquisition sources over correctness of the underlying library state.

## Repository Hygiene

The repository must remain independent of external coding-agent tooling.

Do not add or retain:

* tool-specific instructions;
* tool-generated banners or comments;
* generated attribution or contribution claims;
* tool-specific configuration files;
* tool-specific ignore rules;
* temporary agent state;
* generated workspace identifiers;
* documentation whose only purpose is to configure or describe an external coding agent;
* dependencies, scripts, environment variables, or runtime behavior that exist solely to support an external coding agent.

Before committing, inspect tracked, untracked, and ignored repository content for accidental tool residue.

Project documentation must describe Soundloom itself, not the software used to modify it.

Historical Git metadata is handled separately from the working tree. Do not rewrite published commit history merely to remove tooling attribution unless history rewriting has been explicitly approved.

## No Blind Code Generation

AI-assisted changes are acceptable, but generated code must be treated exactly like human-written code.

Every change must:

1. identify the invariant being changed;
2. inspect the existing production call path;
3. add or update regression tests;
4. run the relevant tests;
5. run the full suite before declaring the work complete;
6. mutation-test critical regressions when practical;
7. inspect the actual runtime/database/filesystem behaviour for changes involving real library state.

A passing unit test is not sufficient evidence for filesystem, acquisition, migration, observer, or provenance changes.

## Protect the State Machine

Whenever production code depends on a state machine:

* derive tests from the production transition rules;
* test the actual production call sequence;
* ensure intermediate states are explicitly recorded;
* reject illegal transitions;
* mutation-test the test when the invariant is important.

Never allow a state to exist only in an enum or transition table without production code actually reaching it.

## Filesystem Truth Is Authoritative

A library record must distinguish at least:

* present;
* missing;
* moved;
* replaced/changed;
* unknown/unverified.

Do not collapse these states merely because a path no longer exists.

A missing file does not imply deletion.

An in-library relocation is different from an out-of-library disappearance.

A file existing at the expected path does not prove it is the same file that was previously indexed.

## Provenance Is First-Class Data

Acquisition records and forensic observations remain separate concerns.

`acquisition` answers:

> How did this item enter the system?

`file_observation` answers:

> What is happening to the filesystem object we previously knew?

Do not merge these concepts merely to simplify queries.

Known-provenance files may be continuously observed.

Files without enough identity information must remain `Unknown`, not be incorrectly promoted to `Missing`.

## Content Identity Before Duplicate Logic

Before implementing aggressive duplicate detection, repair, or automatic replacement:

1. establish stable content identity;
2. record reliable digests;
3. distinguish metadata changes from byte changes;
4. identify replacement-in-place;
5. determine whether changed bytes correspond to another known library object.

The current population of size mismatches is an investigation priority.

Do not automatically "fix" those files until the source of the discrepancy is understood.

## Current High-Priority Investigation

The next forensic investigation should analyze the known-present files whose recorded size differs from the actual filesystem size.

For representative samples:

1. calculate the current digest;
2. compare against the recorded digest when available;
3. search `library_content` for the current digest;
4. determine whether the bytes correspond to another known track;
5. inspect timestamps and provenance;
6. determine whether the mismatch comes from:

   * replacement in place;
   * transcoding;
   * tagging;
   * post-index mutation;
   * incorrect historical metadata;
   * another pipeline defect.

Do this before implementing any automatic reconciliation.

The important outcome is a classification, not merely a count.

## Acquisition Must Be Transactional

An acquisition should behave as one recoverable workflow:

```text
queued
→ downloading
→ staged
→ matched
→ converted
→ tagged
→ organized
→ indexed
→ committed
```

Every intermediate step must have an observable and recoverable state.

Failures must leave enough journal/provenance information to answer:

* what was attempted;
* what source was used;
* where the temporary/staged file was;
* what failed;
* whether any library mutation occurred;
* whether the operation can safely be retried.

Never delete evidence of a failed operation before the failure is recorded.

## Safe Local Acquisition

`SourceType.LOCAL` must remain a first-class source.

A local-source acquisition should:

* read the source file's own metadata;
* copy rather than move the user's file;
* never delete or mutate the original unless explicitly requested;
* pass through the normal matching, conversion, tagging, organization, indexing, and commit pipeline;
* preserve provenance indicating that the source was local.

Windows paths must never be interpreted as URL schemes.

## Continuous Observer Design

The observer must be:

* debounced;
* scoped;
* incremental;
* provenance-aware;
* cheap enough to run continuously;
* conservative in classification.

Avoid whole-library rehashing on every poll.

A contradiction or transient filesystem condition should not immediately become a destructive verdict.

The observer should prefer:

```text
observe → confirm → classify → record
```

over:

```text
observe once → mutate state
```

## User Safety Rules

Never perform destructive filesystem operations implicitly.

Before moving, replacing, deleting, or reconciling user files:

* establish identity;
* establish the intended destination;
* record provenance;
* make the operation recoverable;
* preserve a backup or rollback path where appropriate;
* expose what will happen to the user.

The database must never claim a filesystem mutation succeeded until the filesystem operation actually succeeded.

## Migrations

Every schema migration must:

* have a unique migration number;
* be idempotent or fail safely;
* create an automatic pre-migration backup;
* be tested against a representative prior schema;
* preserve existing user/library data;
* report exactly what changed.

Never treat a successful migration process exit as sufficient evidence. Verify the resulting schema and important row counts.

## Definition of Done

A significant backend change is not complete until:

```text
production path verified
+ regression test added
+ full test suite passing
+ important invariant mutation-tested
+ database/filesystem behaviour inspected where applicable
+ no unintended generated/tool residue
+ documentation/handoff updated
```

For filesystem or database changes, include before/after evidence.

For a bug that came from a real production path, the regression test must reproduce the original failure mode, not merely test the corrected implementation.

## Product Direction

The best version of Soundloom is not "another downloader."

It should become a **trusted music-library truth engine with acquisition and automation attached**.

The product should progressively provide:

### 1. A trustworthy library ledger

Every track has a coherent identity:

```text
content identity
+ canonical path
+ metadata identity
+ provenance
+ current filesystem state
+ history
```

### 2. A forensic timeline

For every meaningful change, a user should eventually be able to understand:

```text
Indexed
→ Acquired from X
→ Moved to Y
→ Bytes changed
→ Metadata changed
→ Missing
→ Reappeared
```

This is more valuable than simply showing a red "missing" badge.

### 3. Safe automatic maintenance

Once identity is trustworthy, Soundloom can safely automate:

* organization;
* metadata cleanup;
* cover-art repair;
* format normalization;
* relocation recovery;
* duplicate suggestions;
* acquisition retries;
* playlist synchronization.

Automation should become more aggressive only as confidence increases.

### 4. Explainable decisions

For consequential actions, expose why Soundloom believes something is true:

```text
Moved
because:
same digest
+ new path inside library root
+ old path disappeared
```

or:

```text
Replaced
because:
same path
+ previous digest differs
+ current digest matches another known object
```

The system should make its reasoning inspectable rather than magical.

## Recommended Development Sequence

### Phase A — Stabilize truth

Finish the forensic model.

Resolve the current size/digest mismatch investigation.

Establish robust identity and replacement detection.

Harden migration and operation-journal recovery.

### Phase B — Make the pipeline boring

Make acquisition adapters conform to one reliable contract.

Add end-to-end canaries for:

```text
remote acquisition
local acquisition
failure
retry
duplicate
conversion
organization
indexing
rollback
```

Every source adapter should feed the same acquisition state machine.

### Phase C — Build the repair engine

Once the system knows what happened, add safe responses:

```text
Missing → locate
Moved → relink
Replaced → investigate
Duplicate → suggest
Bad metadata → repair
Wrong location → reorganize
```

Do not build the repair engine before the forensic classifications are dependable.

### Phase D — Turn observations into value

Expose an actionable dashboard:

```text
Healthy
Needs attention
Moved
Missing
Changed
Duplicate candidate
Unverified
Acquisition failed
```

Prioritize items by confidence and impact.

### Phase E — Automate carefully

Only after the above is reliable:

* scheduled playlist sync;
* artist watching;
* automatic metadata repair;
* automatic organization;
* background acquisition;
* retry policies;
* maintenance jobs.

Automation should operate from the same audited state and provenance machinery used by manual actions.

## Architectural Principle

Prefer a small number of strong primitives over many feature-specific implementations.

The core primitives should converge toward:

```text
Identity
Observation
Provenance
Operation
Transaction
Recovery
Repair
```

New features should compose those primitives instead of creating parallel state models.

## Project Success Metric

The most important metric is not number of integrations or downloads.

It is:

> **Can Soundloom explain the state and history of every known piece of music without lying, losing data, or requiring the user to remember what happened?**

When that answer is consistently yes, the rest of the product becomes much easier to build.

## One Strategic Directive

Do not chase the size-mismatch population one file at a time. Turn it into a small
forensic classification experiment first. The next milestone is a report that can
automatically say "these N files fall into M causes", because that identifies which
architectural flaw is actually worth fixing.

Treat tool cleanup as repository hygiene, not as a reason to rewrite history: remove
tool-specific files and future instructions, while preserving legitimate authorship and
license provenance.
