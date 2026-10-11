-- Cluster-level roles (LLD 01 s3, LLD 02 s7). Run ONCE by a superuser; idempotent. No application role is a
-- superuser and none has BYPASSRLS, so row-level security always applies to them.
--   sb_owner   owns the schema, runs migrations. Never used by the running application.
--   sb_auth    the authentication path (JIT provisioning, user lookup): app_user only.
--   sb_app     the request path: acts for one user, one tenant, one role per transaction.
--   sb_worker  ingestion, retention and maintenance jobs: tenant-scoped, wider table rights, no chat access.
--   sb_eval    the evaluation job: may read message_raw (opt-in tenants only), nothing else.
--   sb_reader  NOLOGIN. Owns the audited tutor-read function; the only way to read another user's conversation.
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['sb_owner', 'sb_auth', 'sb_app', 'sb_worker', 'sb_eval'] LOOP
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE', r);
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sb_reader') THEN
    CREATE ROLE sb_reader NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;
-- the owner must be able to hand function ownership to sb_reader
GRANT sb_reader TO sb_owner;
