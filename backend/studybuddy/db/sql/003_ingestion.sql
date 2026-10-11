-- Ingestion support (LLD 04): retirement timestamps for the cleanup jobs, and a narrow outbox relay role.
ALTER TABLE document ADD COLUMN deleted_at timestamptz;
ALTER TABLE document_version ADD COLUMN retired_at timestamptz;  -- set when a newer version becomes current (D5: cleanup after 7 days)

-- The cleanup jobs (running as sb_worker, one tenant at a time) hard-delete after the grace period.
GRANT DELETE ON document, document_version TO sb_worker;

-- Outbox relay (LLD 04 s5). It must see unpublished events of every tenant, so it cannot use the tenant-scoped policy.
-- It gets a role of its own that can read outbox rows and set published_at, and nothing else: no tenant data, no inserts.
GRANT USAGE ON SCHEMA public TO sb_relay;
GRANT SELECT, UPDATE (published_at) ON outbox_event TO sb_relay;
CREATE POLICY outbox_relay_read ON outbox_event FOR SELECT TO sb_relay USING (true);
CREATE POLICY outbox_relay_mark ON outbox_event FOR UPDATE TO sb_relay USING (published_at IS NULL) WITH CHECK (published_at IS NOT NULL);
