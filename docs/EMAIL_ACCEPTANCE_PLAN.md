# Email Intelligence Acceptance Plan

Status: planned. None of these cases has been run against an implementation in this repository.

Use synthetic mail first, then approved user/shared mailboxes. Fixtures, logs and reports must not commit employee email content, credentials or PHI to GitHub. Store restricted test evidence under the approved data policy.

## Coverage and Correctness

| Scenario | Expected Evidence |
|---|---|
| Received and sent mail; selected user and shared mailbox | Each eligible source occurrence has correct sender, recipient roles, source date and stable identity |
| Ordinary nested folders; excluded folder descendants | Eligible folders reconcile; excluded content is not published |
| Repeated events or re-run capture | No duplicate occurrence or active chunk version; cursor and job counts reconcile |
| Move, edit, delete, folder removal or expired cursor | Fresh source state governs; bounded recovery; removed evidence is not returned |
| Native PDF, scanned PDF and mixed PDF | Searchable extracted text with correct source/page references and no native/OCR double counting |
| Standalone image, Office file, archive or linked attachment | No extraction, conversion, OCR, embedding or document classification; occurrence held under the unsupported-file policy, including inline-image cases |
| Mislabeled non-PDF or PDF with embedded payloads/unsupported active features | Type validation/inspection holds the file; no active execution or recursive extraction |
| Corrupt, encrypted, unsupported or over-limit file | Explicit held/review state, meaningful coverage reporting and no silent claim of complete extraction |
| Same email in two mailboxes or BCC variant | Distinct occurrences; no permission or recipient-field leakage through deduplication |

## Security and Scope

| Scenario | Expected Evidence |
|---|---|
| Empty test list, invalid IDs or misspelled mode | Capture fails closed; never expands to tenant users |
| Test-approved versus production-approved scopes | Correct intersection for each mode; enabling production does not enable other sources |
| Out-of-scope mailbox | Registry check prevents source read; source-side permission test also denies it |
| Mode transition while work is running | In-flight work cannot publish under an inactive revision or wrong dataset; stale retrieval refuses evidence |
| Known spam/threat, unknown verdict or delayed scan | Excluded/held/pending status; no extraction of unscanned attachments and no unsafe publication |
| Representative sensitive information or prompt attack | Policy decision recorded; rejected content does not reach embeddings, agent context or monday |
| Screening/OCR/embedding/search dependency outage | Bounded retry/dead-letter behavior; outage does not become a clean verdict or false success |
| Missing/expired/wrong-tenant/wrong-audience token | Request rejected before content retrieval |
| Valid but unapproved executive or forged access filter | No evidence; API derives permissions from validated identity and current grants |
| Revoked source/corpus access, stale index or changed source | Retrieval suppresses results; withdrawal/reprocessing recorded |

## Search and Agent Quality

Prepare a small question set with expected evidence, including exact terms, paraphrases, dates, attachments, contradictory statements and intentionally missing information. Record both relevant results and unauthorized candidates that must never reach the model.

Answers should cite the source message and attachment page where available, distinguish statements from verified decisions, and acknowledge partial history or held attachments. Do not treat successful API responses alone as successful retrieval quality. An empty result must not imply that no matching communication exists anywhere in the company.

## Operations and Lifecycle

| Scenario | Expected Evidence |
|---|---|
| Worker crash before/after source checkpoint or partial publication | Durable replay without lost records or exposed partial versions |
| Concurrent workers and rate limits | No duplicate active publication; fair retries and bounded provider traffic |
| New mail arrives during historical backfill | Separate capacity/backpressure keeps agreed freshness targets; backfill progress is measurable |
| Retention expiry, deletion and revocation | Results stop first; physical cleanup and destination withdrawals complete within agreed policy windows |
| Backup restoration | Checkpoints, tombstones and revocations reconcile before restored evidence becomes searchable |
| Release rollback | Known-good artifact runs against compatible schema; mode/scope are preserved |
| Dead-letter or scan backlog | Alert reaches its owner; authorized replay is documented and auditable |

## Release Record

Capture commit/artifact IDs, schema and policy versions, configuration revision, approved mailbox inventory, executive entitlement, backfill boundary, file-format coverage, known held categories, test results, measured throughput/cost, recovery evidence and the approving owners.

The previous prototype's 28 passing automated tests are reusable test input only. They are not acceptance evidence for this repository, real tenant permissions, Defender signals, OCR quality or Copilot authentication. Production launch requires the implementation and tenant evidence above.
