-- LLD 01 s2. Every tenant-owned table has tenant_id NOT NULL; every parent has UNIQUE (tenant_id, id) and every child
-- references (tenant_id, parent_id), so a row can never point at another tenant's row, even with RLS switched off.

CREATE FUNCTION uuid_v7() RETURNS uuid LANGUAGE sql VOLATILE AS $$
  -- time-ordered ids (LLD 01 principle 5): 48-bit millisecond timestamp, then random bits, version nibble set to 7
  SELECT encode(
    set_bit(set_bit(
      overlay(uuid_send(gen_random_uuid()) PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3) FROM 1 FOR 6),
      52, 1), 53, 1), 'hex')::uuid
$$;

CREATE TABLE tenant (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  name text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
  plan text NOT NULL DEFAULT 'standard',
  monthly_token_budget bigint CHECK (monthly_token_budget >= 0),
  eval_retention boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL REFERENCES tenant (id),
  idp_subject text NOT NULL CHECK (idp_subject <> ''),  -- OIDC `sub`; no passwords are stored (LLD 02)
  role text NOT NULL CHECK (role IN ('student', 'instructor', 'tutor', 'tenant_admin')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, idp_subject)
);

CREATE TABLE course (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL REFERENCES tenant (id),
  title text NOT NULL,
  term text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id)
);

CREATE TABLE enrollment (
  tenant_id uuid NOT NULL,
  course_id uuid NOT NULL,
  user_id uuid NOT NULL,
  role_in_course text NOT NULL CHECK (role_in_course IN ('student', 'instructor', 'tutor')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, course_id, user_id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES course (tenant_id, id),
  FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX enrollment_user_idx ON enrollment (tenant_id, user_id);

CREATE TABLE document (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  course_id uuid NOT NULL,
  title text NOT NULL,
  current_version_id uuid,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES course (tenant_id, id)
);
CREATE INDEX document_course_idx ON document (tenant_id, course_id);

CREATE TABLE document_version (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  document_id uuid NOT NULL,
  version_no integer NOT NULL CHECK (version_no > 0),
  object_key text NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  status text NOT NULL DEFAULT 'uploaded' CHECK (status IN ('uploaded', 'processing', 'ready', 'failed')),
  error_reason text,
  attempts integer NOT NULL DEFAULT 0,
  progress smallint NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  flagged_injection boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, document_id, version_no),
  FOREIGN KEY (tenant_id, document_id) REFERENCES document (tenant_id, id)
);
ALTER TABLE document ADD FOREIGN KEY (tenant_id, current_version_id) REFERENCES document_version (tenant_id, id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE chunk (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  course_id uuid NOT NULL,
  document_id uuid NOT NULL,
  version_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  text text NOT NULL,
  page integer,
  heading_path text,
  content_hash text NOT NULL,
  token_count integer NOT NULL CHECK (token_count >= 0),
  embedding_model text NOT NULL,
  flagged boolean NOT NULL DEFAULT false,
  UNIQUE (tenant_id, version_id, ordinal),
  FOREIGN KEY (tenant_id, course_id) REFERENCES course (tenant_id, id),
  FOREIGN KEY (tenant_id, document_id) REFERENCES document (tenant_id, id),
  FOREIGN KEY (tenant_id, version_id) REFERENCES document_version (tenant_id, id)
);

CREATE TABLE conversation (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  course_id uuid NOT NULL,  -- one course per conversation (LLD 01 D1)
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES course (tenant_id, id)
);
CREATE INDEX conversation_user_idx ON conversation (tenant_id, user_id, created_at DESC);

CREATE TABLE message (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content_redacted text NOT NULL,  -- PII scrubbed before persist (LLD 01 s5, LLD 08)
  raw_content_id uuid,
  route text CHECK (route IN ('cache', 'rule', 'redirect', 'cheap_llm', 'strong_llm', 'human')),
  model text,
  cited_chunk_ids text[] NOT NULL DEFAULT '{}',
  tokens_in integer,
  tokens_out integer,
  cost_usd numeric(12, 6),
  latency_ms integer,
  trace_id text,
  pipeline_meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation (tenant_id, id)
);
CREATE INDEX message_conversation_idx ON message (tenant_id, conversation_id, created_at);

CREATE TABLE message_raw (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  message_id uuid NOT NULL,
  content text NOT NULL,
  expires_at timestamptz NOT NULL DEFAULT now() + interval '30 days',
  FOREIGN KEY (tenant_id, message_id) REFERENCES message (tenant_id, id)
);
CREATE INDEX message_raw_expiry_idx ON message_raw (tenant_id, expires_at);
CREATE INDEX message_raw_message_idx ON message_raw (tenant_id, message_id);

CREATE TABLE escalation_ticket (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  course_id uuid NOT NULL,
  conversation_id uuid NOT NULL,  -- kept here so message/ticket policies never reference each other (no policy recursion)
  message_id uuid NOT NULL,
  reason text NOT NULL,
  priority text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'assigned', 'resolved', 'closed')),
  assigned_tutor_id uuid,
  sla_deadline timestamptz,
  resolution text,
  outcome text,
  approved_faq boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, course_id) REFERENCES course (tenant_id, id),
  FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversation (tenant_id, id),
  FOREIGN KEY (tenant_id, message_id) REFERENCES message (tenant_id, id),
  FOREIGN KEY (tenant_id, assigned_tutor_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX ticket_tutor_idx ON escalation_ticket (tenant_id, assigned_tutor_id, status);
CREATE INDEX ticket_conversation_idx ON escalation_ticket (tenant_id, conversation_id);

CREATE TABLE faq_entry (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  course_id uuid NOT NULL,
  question text NOT NULL,
  answer text NOT NULL,
  ticket_id uuid,
  approved_by uuid NOT NULL,
  reviewed_at timestamptz NOT NULL DEFAULT now(),
  source_tags jsonb NOT NULL DEFAULT '[]',
  FOREIGN KEY (tenant_id, course_id) REFERENCES course (tenant_id, id),
  FOREIGN KEY (tenant_id, ticket_id) REFERENCES escalation_ticket (tenant_id, id),
  FOREIGN KEY (tenant_id, approved_by) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX faq_course_idx ON faq_entry (tenant_id, course_id);

CREATE TABLE usage_ledger (
  tenant_id uuid NOT NULL,
  user_id uuid NOT NULL,
  day date NOT NULL,
  tokens_in bigint NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out bigint NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  cost_usd numeric(14, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
  PRIMARY KEY (tenant_id, user_id, day),
  FOREIGN KEY (tenant_id, user_id) REFERENCES app_user (tenant_id, id)
);

CREATE TABLE audit_log (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL REFERENCES tenant (id),
  actor_id uuid,
  action text NOT NULL,
  target text,
  at timestamptz NOT NULL DEFAULT now(),
  ip text
);
CREATE INDEX audit_log_idx ON audit_log (tenant_id, at DESC);

CREATE TABLE outbox_event (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL REFERENCES tenant (id),
  type text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);
CREATE INDEX outbox_unpublished_idx ON outbox_event (tenant_id, created_at) WHERE published_at IS NULL;

CREATE TABLE schema_migration (
  name text PRIMARY KEY,
  sha256 text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
