-- 0002_email.sql
-- Email extension tables on top of the source-neutral foundation (0001).
-- Each mailbox copy of a message is its own communication (identity =
-- dataset + connection + mailbox + immutable id); these tables hold the
-- email-only metadata. Message bodies are never stored here.

SET search_path = ci, public;

-- One row per mailbox scope (source_scopes.scope_kind = 'mailbox'), written by
-- inventory sync. native_scope_id on the scope is the Entra object id.
CREATE TABLE email_mailboxes (
    scope_id             uuid PRIMARY KEY REFERENCES source_scopes(id) ON DELETE CASCADE,
    primary_smtp_address text NOT NULL UNIQUE CHECK (primary_smtp_address = lower(primary_smtp_address)),
    recipient_type       text NOT NULL CHECK (recipient_type IN ('UserMailbox', 'SharedMailbox',
                                                                 'RoomMailbox', 'EquipmentMailbox',
                                                                 'Other')),
    last_inventory_at    timestamptz NOT NULL DEFAULT now()
);

-- Occurrence metadata for one mailbox copy. internet_message_id is kept
-- exactly as delivered; it correlates copies across mailboxes but is never
-- identity on its own (docs/EMAIL_DEDUPLICATION.md).
CREATE TABLE email_messages (
    communication_id    uuid PRIMARY KEY REFERENCES communications(id) ON DELETE CASCADE,
    mailbox_scope_id    uuid NOT NULL REFERENCES email_mailboxes(scope_id),
    internet_message_id text,
    conversation_id     text,
    change_key          text,
    received_at         timestamptz NOT NULL,
    sent_at             timestamptz,
    direction           text NOT NULL CHECK (direction IN ('in', 'out', 'internal', 'unknown')),
    parent_folder_id    text,
    has_attachments     boolean NOT NULL DEFAULT false,
    content_fingerprint text,           -- sha256 of normalized content, set after verification
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX email_messages_mailbox_received ON email_messages (mailbox_scope_id, received_at DESC);
CREATE INDEX email_messages_internet_message_id ON email_messages (internet_message_id)
    WHERE internet_message_id IS NOT NULL;
CREATE INDEX email_messages_conversation ON email_messages (conversation_id)
    WHERE conversation_id IS NOT NULL;

-- Attachment metadata per processed version. In E1 nothing is fetched:
-- status is 'metadata_only' or a policy outcome under
-- ATTACHMENT_UNSUPPORTED_POLICY (ADR-016). E2 adds extraction states.
CREATE TABLE email_attachments (
    version_id           uuid NOT NULL REFERENCES communication_versions(id) ON DELETE CASCADE,
    native_attachment_id text NOT NULL,
    name                 text,
    content_type         text,
    size_bytes           bigint NOT NULL CHECK (size_bytes >= 0),
    is_inline            boolean NOT NULL DEFAULT false,
    content_id           text,
    status               text NOT NULL DEFAULT 'metadata_only'
                         CHECK (status IN ('metadata_only', 'exempt_inline', 'unsupported_partial',
                                           'unsupported_held')),
    PRIMARY KEY (version_id, native_attachment_id)
);

-- Citation and duplicate-collapse fields for search. Built on
-- published_communications, so every authorization and exclusion rule from
-- 0001 applies. Corpus authorization is still applied by the API query.
CREATE VIEW published_email_messages AS
SELECT pc.id AS communication_id, pc.corpus_id, m.primary_smtp_address AS mailbox_address,
       em.internet_message_id, em.conversation_id, em.received_at, em.sent_at, em.direction,
       em.has_attachments, em.content_fingerprint
FROM published_communications pc
JOIN email_messages em ON em.communication_id = pc.id
JOIN email_mailboxes m ON m.scope_id = em.mailbox_scope_id;

-- Attachment coverage (counts by status) for the search coverage block.
CREATE VIEW published_email_attachment_coverage AS
SELECT pc.id AS communication_id, pc.corpus_id, a.status, count(*) AS attachment_count
FROM published_communications pc
JOIN email_attachments a ON a.version_id = pc.active_version_id
GROUP BY pc.id, pc.corpus_id, a.status;

GRANT SELECT ON email_mailboxes, email_messages, email_attachments TO ci_ingest;
GRANT INSERT, UPDATE, DELETE ON email_mailboxes, email_messages, email_attachments TO ci_ingest;
GRANT SELECT ON published_email_messages, published_email_attachment_coverage TO ci_search;
-- Operators list mailboxes for `ci scope list`; addresses are not evidence content.
GRANT SELECT ON email_mailboxes TO ci_operator;
