# Email Intelligence Layers and PDF-Only First Release

> **2026-10-14:** PDF extraction moves to increment E2; E1 records attachment metadata only. The whole-message hold described below is now one value of `ATTACHMENT_UNSUPPORTED_POLICY` ([ADR-016](ARCHITECTURE_DECISIONS.md#adr-016-unsupported-attachment-handling-is-a-configured-policy)), with `publish_partial` as the local/dev default and no production default.

Status: planned behavior, not deployed code. Matthew confirmed on September 25, 2026 that PDF is the only supported attachment format for the first release. Other formats will be enabled in separate tested releases.

The PDF restriction applies to attachments. Ordinary email subject/body text still enters the email intelligence pipeline. An email does not need an attachment to be eligible.

## 1. Outlook and Exchange: The Source

Microsoft 365 retains the original mailboxes. The ingestion identity reads only authorized, configured user/shared mailboxes through Graph. The initial product does not send mail or change originals. The application tracks new, changed, moved and deleted source messages; reading a mailbox is different from publishing its contents to AI search.

Output: source identifiers and change events, subject to test/production scope.

## 2. Capture: Durable Intake

Store minimal event metadata in a durable queue with tenant, mailbox, immutable item ID, source version, observed time and dataset. Commit queued work and the source checkpoint together so a restart does not lose messages. Fetch source content only after scope checks. Retry rate limits and temporary failures; surface persistent failures explicitly.

Output: recoverable work records, not immediately searchable email.

## 3. Deduplication: Reuse Work

Track each mailbox occurrence and its permissions. Correlate email copies with Internet Message-ID plus verified content, and identify exact PDF bytes with SHA-256. Claim reusable work transactionally so concurrent recipients do not start independent durable processing results. Continue per-occurrence source/security checks.

Unsupported attachments mean the complete message cannot be validated for publication under the current policy; retain their metadata and avoid declaring a complete content match using missing attachment bytes. Identical independently eligible PDFs can still have reusable processing results in the appropriate security partition.

Output: source references linked to distinct content/artifacts and reusable processing jobs. See [deduplication design](EMAIL_DEDUPLICATION.md).

## 4. Security and File Eligibility: Decide What May Be Processed

Reject or hold source threats/spam according to verified policy. Admit only PDF file attachments to document extraction. Validate declared media type, filename and actual bytes/structure; an extension or MIME label alone is insufficient. Ambiguous type claims are held, not automatically converted. Apply malware checks before any full PDF parsing or rendering.

Enforce finite file-size, page-count, memory and execution-time limits. Do not execute PDF scripts/actions, launch embedded objects, follow hyperlinks or recursively process embedded files. PDFs with embedded payloads, unsupported active features, encryption/password protection, corruption or untrusted scan results go to review/hold. A PDF container is not automatically safe.

Output: eligible PDFs, rejected threats, pending scans and explicit unsupported/held records.

## 5. Extraction and OCR: Turn PDFs Into Text

For a text PDF, extract its existing text while preserving page references. For scanned/image-based PDF pages, use OCR. For mixed PDFs, use page-aware extraction and avoid including both native and OCR copies of the same text. Text quality and complete page coverage must be checked before publication.

OCR on images inside a PDF is permitted because the admitted document is a PDF. A separate JPG, PNG, TIFF or image attachment is not permitted in this release. The OCR service's support for other formats does not expand our product allowlist.

Output: text, page references, quality/coverage information and extractor version. Unreadable/low-confidence results are held rather than silently treated as empty documents. Diagrams and visual meaning without text are not promised by text OCR alone.

## 6. Sensitivity and Prompt Screening: Decide What May Be Published

Evaluate eligible email text and all extracted PDF text for restricted information and prompt-injection content. Early checks can reject obvious sensitive body text before file processing; the complete policy decision happens only when the required evidence has been evaluated. Do not embed, summarize, classify through general AI or publish rejected content.

Missing policy-service results cause pending/held state. Screening itself is processing, so raw-material and AI-service handling must follow the approved tenant policy. Reusing an OCR result does not bypass current policy evaluation.

Output: approved evidence or a versioned reason for a hold/rejection.

## 7. Normalization and Classification: Describe the Evidence

Normalize sender/recipient roles, dates, source links, artifacts and versions into shared records. Classification assigns proposed labels such as change-order request, invoice, contract, schedule update or general correspondence. Extract candidate attributes such as project, vendor, amount, requested date and document revision, preserving evidence/page references, confidence and unresolved fields.

Email text supplies context: a PDF may be a change-order form while the email says it is withdrawn, revised, disputed or awaiting approval. Classify the document and the communication separately where needed. Reuse extraction for duplicate PDFs; do not reuse context-dependent conclusions solely because their file hash matches.

Output: structured communication records and proposed classifications. A model label such as "approval mentioned" is not a verified business approval.

## 8. Chunking, Embeddings and Indexing: Make Approved Evidence Searchable

Divide approved email/PDF text into useful sections, retaining communication/artifact IDs and page citations. Generate embeddings for semantic similarity and publish keyword/vector evidence into the index. Record model and processing versions. SQL identifies which evidence version is active; partial index writes cannot become visible just because some chunks exist.

PostgreSQL holds authoritative records, permissions and processing state. Private Blob Storage holds authorized original/extracted artifacts. Search contains rebuildable derivatives. Database writes happen throughout processing, not only at this stage.

Output: eligible, versioned search evidence with current authorization references.

## 9. Retrieval and Copilot: Answer With Authorized Evidence

Validate the executive's identity and approved corpus access. Search eligible content, filter by current source/permission/publication state, collapse authorized duplicates and return citations. The agent can then explain or summarize the evidence. Report held/unsupported coverage instead of implying that an unindexed attachment was reviewed.

Output: an answer grounded in accessible email/PDF evidence. No direct access to a broad index that bypasses the API.

## 10. Routing and Lifecycle: Act Carefully and Keep Results Current

Classification may create a governed review candidate or an approved monday projection when that integration is enabled. Use a separate action idempotency key to prevent duplicate change-order records/notifications. Routing is optional for search and must not hold up intake when a destination is down.

Track edits, revocations, deletions and retention changes across source links, stored artifacts, chunks, indexes and destination projections. Held records have reason codes, retry/review paths and retention rules. Audit metadata must not expose the held content.

## First-Release File Matrix

| Input | First Release |
|---|---|
| Email subject/body without files | Eligible after scope and policy checks |
| Normal text PDF | Malware gate, native extraction, policy checks, classification/search |
| Scanned PDF | Malware gate, OCR, quality/policy checks, classification/search |
| Mixed text/scanned PDF | Page-aware extraction/OCR and coverage verification |
| JPG/PNG/TIFF, including inline image attachments | Unsupported; no OCR or content indexing |
| DOC/DOCX, XLS/XLSX, PPT/PPTX, TXT/CSV | Unsupported; no conversion or extraction |
| ZIP/other archives, attached email items, linked/cloud attachments | Unsupported; no unpacking, recursion or external fetching |
| Encrypted/corrupt/over-limit PDF or uncertain file type | Held with a reason; no successful publication |
| PDF with embedded payloads/unsupported active features | Held; no recursion or execution |

### Current Unsupported-Attachment Policy

Preserve the existing conservative whole-message policy: an unsupported or unevaluated attachment marks the occurrence held for review, and that occurrence's body/PDFs are not published as a complete searchable message. Record attachment metadata and reason codes; do not extract or embed the unsupported file. Do not remove or alter the email in Outlook.

This includes inline image attachments, which can include email logos/signatures. That will reduce searchable coverage in a PDF-only pilot. Do not silently exempt them by filename or small size. A separately approved policy could later permit body/PDF-only results with explicit partial coverage or narrowly defined decorative-image exclusions; neither is enabled by this first-release change.

An eligible identical occurrence elsewhere can remain independently searchable if its own scope/security/content checks pass. Do not use that relationship to expose metadata or files from a held copy.

## Rollout Contract

The proposed first-release settings are:

```dotenv
ATTACHMENT_ALLOWED_MIME_TYPES=application/pdf
PDF_OCR_ENABLED=true
ATTACHMENT_UNSUPPORTED_POLICY=hold_message
```

These are configuration specifications, not implemented switches. Non-PDF bytes renamed to `.pdf` must still fail validation. Do not automatically convert unsupported formats to PDF as a workaround.

Add each future format through its adapter, validation, threat/sensitivity controls, citations, deduplication, regression tests and coverage reporting. Expand the allowlist only after that format passes acceptance. A controlled replay may then revisit previously held mail within scope and retention limits without duplicating business actions.

## Required PDF-Only Tests

- Text-only email and eligible text/scanned/mixed PDFs can publish with citations.
- Standalone images/Office files/archives are never sent to extraction, OCR, embeddings or document classification.
- PDF plus unsupported attachment, including an inline image, holds that occurrence with the documented reason.
- Misnamed/mislabeled files, corrupt/encrypted PDFs, embedded payloads and missing scan results cannot publish.
- Duplicate PDFs reuse eligible work; changed revisions and different email context remain distinct.
- Reprocessing after a supported-format rollout respects grants, retention, prior action keys and model/policy versions.

Reference: [Azure Document Intelligence Read](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0). Its broader service capabilities do not change this platform's PDF-only scope.
