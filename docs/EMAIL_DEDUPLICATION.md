# Email and Attachment Deduplication

Status: required design for the email release, not implemented code. This refines the common model: preserve source occurrences while reusing verified content processing.

## Required Outcome

A vendor sends the same change-order email and PDF to Matthew, another employee and a shared mailbox. The platform should retain three source occurrences, reuse one eligible processing result for the identical content, show one grouped result to an authorized reader, and create at most one business action for the same project/document revision.

Shared inboxes can simplify intake, but the platform must deduplicate across approved individual and shared mailboxes too. It still needs to read enough information from each occurrence to establish its identity, content and permissions; deduplication does not remove all Graph fetches or source-specific checks.

## Native Identifiers and Content Fingerprints

| Value | Use | Boundary |
|---|---|---|
| Graph `internetMessageId` | Candidate correlation across copies of the same sent email; represents the Internet Message-ID header | Sender-supplied identifier, not authentication or proof that content/attachments match; may be absent, malformed or reused |
| Graph immutable item ID plus mailbox ID | Track a particular mailbox occurrence through ordinary folder moves | Does not identify all recipients' copies as one object |
| Graph `changeKey` | Notice a source item's version changed | Not a tenant-wide duplicate key or content hash |
| Graph `conversationId` | Thread grouping | Replies in the same thread remain different communications |
| SHA-256 of decoded attachment bytes | Exact artifact reuse, including the same PDF arriving in different emails | Filename alone is irrelevant; modified/re-exported files generally differ even if visually similar |
| Versioned body/content fingerprint | Verify equivalent reusable message content within a candidate group | Normalization must preserve meaningful differences; not an email footer signature or a cryptographic sender signature |

Use tenant + dataset + Internet Message-ID to find candidates, then verify a conservative content fingerprint. Preserve the raw Message-ID and use standards-aware parsing; do not blindly lowercase the entire identifier. If unavailable or unreliable, retain separate logical emails and reuse only independently verified identical artifacts. Near-duplicate matches can be flagged for review.

Fingerprint a versioned, unambiguous structured serialization of sender/from, sent time, subject, conservatively normalized body and the complete attachment manifest. Include artifact hashes, MIME types and attachment roles; preserve duplicate attachment counts. Keep delivery-specific headers, mailbox timestamps, read state and recipient/BCC visibility in occurrence metadata. Do not strip disclaimers, signatures, quoted decisions or amounts merely to force a match. Preserve full original content for provenance.

## Three Separate Uniqueness Rules

1. **Source occurrence:** tenant/dataset + mailbox ID + immutable item ID. Replayed capture updates the existing occurrence.
2. **Reusable work:** security/cache partition + exact content hash + operation + relevant extractor/chunker/model/policy versions. Identical approved file bytes can reuse OCR even if a forwarded email has a different Message-ID. Classification that depends on surrounding email/project context has its own context fingerprint and cannot be reused on file hash alone.
3. **Business effect:** destination + verified project/asset context + action type + exact document/version identity. Multiple occurrences cannot create multiple change-order candidates or repeated notifications for the same event. Unresolved project/document matches remain review candidates.

Do not key business effects solely by document hash: the same file can be relevant to separate projects. Do not key by change-order number alone: vendors/projects can reuse numbers and revised versions must remain distinguishable. A changed amount, scope or attachment is a new version, not a discarded duplicate. Linking a request to the same action does not approve it.

## Proposed Data Additions

| Record | Responsibility |
|---|---|
| `email_occurrences` | One mailbox copy: source IDs/version, source URL, delivery and visible recipient metadata, access/screening state, linked reusable content |
| `email_content_variants` | Message-ID candidate group plus verified body/attachment fingerprint and provenance; divergent copies stay separate variants |
| `artifact_contents` | Exact byte hash and storage/security partition; no public content-hash lookup |
| `occurrence_artifacts` | Per-occurrence filename, attachment role, artifact content link, visibility and source state |
| `processing_results` | Unique work key, status, lease, fencing revision, result references, model/policy versions and invalidation state |
| `business_actions` and outbox | Stable idempotency key, linked evidence and destination outcome/reconciliation state |

Map these into the existing conceptual communication/version/artifact model during schema implementation. Do not create competing authoritative communication tables accidentally.

## Processing and Race Handling

1. Admit each allowed occurrence idempotently. Evaluate its mailbox scope and source-specific threat state independently.
2. Fetch/stage required artifacts, compute byte fingerprints and correlate candidate email content. Message-ID alone must never skip validation.
3. Claim the applicable work key using a database unique constraint plus transactional insert/lease. Concurrent workers wait for or reuse the same completed result; a check-then-insert without a uniqueness constraint is insufficient.
4. Reuse only completed, eligible results matching the exact input and processing profile. Unknown or stale scan/policy state is not a pass. Malware verdict reuse, if allowed at all, needs its own exact-hash, engine/policy and freshness rules; source email threat verdicts remain occurrence-specific.
5. Store extracted text/chunks/embeddings once within the approved reuse boundary where practical. Associate them with each authorized occurrence without copying another recipient's private metadata into shared content.
6. Publish retrieval links and destination effects through versioned SQL state and a transactional outbox. Collapse search hits after authorization, selecting citations the caller can access.

This provides idempotent durable results, not a universal promise that every remote API is called exactly once. A crash after an OCR or destination request succeeds but before its result is recorded can require reconciliation. Use provider idempotency/status retrieval where supported; otherwise record uncertainty and reconcile instead of blindly recreating external actions. The pipeline may repeat a billable call in an ambiguous failure while still preventing duplicate durable evidence/actions.

## Permissions, Retention and Copies

Keep reuse tenant/dataset/security-partition scoped. Shared processing never widens source or reader permissions. A reader must have at least one currently eligible occurrence that supports the exact returned content; only that occurrence's permitted metadata and citation may be exposed. Do not reveal other recipients, BCC identities or inaccessible source copies through counts or provenance.

Deleting one mailbox copy removes that source relationship. Other independently authorized copies can remain available. Purge shared artifacts only when no permitted retention/hold reference requires them, while ensuring withdrawn occurrences cannot retrieve them. A tenant-wide erasure or security hold can invalidate the shared content and every linked occurrence. Reference counting alone does not override policy.

## Required Tests

- Same sent message reaches two users and a shared mailbox concurrently: three occurrences, one eligible reusable work result, one action for the same project/revision.
- Same Message-ID with different body or attachments: distinct variants and no silent data loss.
- Missing/reused Message-ID: no blind merge; exact attachments still eligible for bounded reuse.
- Forward/reply with unchanged PDF: new communication/context, reused OCR where allowed; new requests or decisions retained.
- Same filename with different bytes: different artifact; different filename with identical bytes: reusable artifact, filenames retained on occurrence links.
- Revised change-order PDF: new evidence/version; supersession only after verified relationship, never filename inference alone.
- Same PDF used for two projects: independent routing context; no erroneous suppression.
- Identical file attached twice in one email: preserve manifest multiplicity without duplicate OCR.
- Different BCC/reader rights or policy partitions: no metadata leakage or authorization bypass.
- Crash, lease expiry, partial publish and ambiguous external response: no duplicate active durable result; destination reconciliation exercised.
- Policy/model/extractor version changes: intended reprocessing, with stale results ineligible.
- Delete/revoke one occurrence, all occurrences, and restore from backup: visibility and cleanup remain correct.

## References

- [Microsoft Graph message properties](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0)
- [Outlook immutable IDs](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
- [Internet Message-ID semantics, RFC 5322 section 3.6.4](https://www.rfc-editor.org/rfc/rfc5322.html#section-3.6.4)
