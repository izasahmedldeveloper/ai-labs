-- LLD 01 s3 and LLD 02 s6 (gate 3): row-level security, least-privilege grants, append-only guards, audited tutor read.
--
-- Contract with the application: every transaction starts with
--     SELECT set_config('app.tenant_id', <verified tenant>, true), set_config('app.user_id', <app_user.id>, true),
--            set_config('app.role', <verified role>, true);
-- (see studybuddy/db/session.py). When app.tenant_id is unset every policy below evaluates to NULL and the role sees
-- NO rows: forgetting to set it fails closed. FORCE ROW LEVEL SECURITY makes the policies apply to the table owner too.

-- ----------------------------------------------------------------------------------------------------- session helpers
CREATE FUNCTION app_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;
CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.role', true), '') $$;

-- Enrollment facts, evaluated under the CALLER's row-level security (so they only ever see the caller's own rows).
CREATE FUNCTION is_enrolled(p_course uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM enrollment e WHERE e.tenant_id = app_tenant() AND e.course_id = p_course AND e.user_id = app_user_id())
$$;
CREATE FUNCTION is_course_instructor(p_course uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM enrollment e WHERE e.tenant_id = app_tenant() AND e.course_id = p_course
                 AND e.user_id = app_user_id() AND e.role_in_course = 'instructor')
$$;

-- ----------------------------------------------------------------------------------------------------- switch RLS on
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenant', 'app_user', 'course', 'enrollment', 'document', 'document_version', 'chunk',
                           'conversation', 'message', 'message_raw', 'escalation_ticket', 'faq_entry', 'usage_ledger',
                           'audit_log', 'outbox_event'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
  END LOOP;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO sb_auth, sb_app, sb_worker, sb_eval, sb_reader;

-- ----------------------------------------------------------------------------------------------------- tenant
GRANT SELECT ON tenant TO sb_auth, sb_app, sb_worker, sb_eval, sb_reader;
CREATE POLICY tenant_self ON tenant FOR SELECT TO sb_auth, sb_app, sb_worker, sb_eval, sb_reader USING (id = app_tenant());
-- Tenants are created by platform operations as the owner role, never by application roles.
CREATE POLICY tenant_owner ON tenant FOR ALL TO sb_owner USING (true) WITH CHECK (true);

-- ----------------------------------------------------------------------------------------------------- app_user
GRANT SELECT, INSERT ON app_user TO sb_auth;
CREATE POLICY user_auth ON app_user FOR ALL TO sb_auth USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());
GRANT SELECT, INSERT ON app_user TO sb_app;
GRANT UPDATE (role, status) ON app_user TO sb_app;
CREATE POLICY user_read ON app_user FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (id = app_user_id() OR app_role() = 'tenant_admin'));
CREATE POLICY user_admin_write ON app_user FOR INSERT TO sb_app WITH CHECK (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
CREATE POLICY user_admin_update ON app_user FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND app_role() = 'tenant_admin') WITH CHECK (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
GRANT SELECT ON app_user TO sb_worker;
CREATE POLICY user_worker ON app_user FOR SELECT TO sb_worker USING (tenant_id = app_tenant());

-- ----------------------------------------------------------------------------------------------------- course / enrollment
GRANT SELECT, INSERT, UPDATE ON course TO sb_app;
CREATE POLICY course_read ON course FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR is_enrolled(id)));
CREATE POLICY course_admin_insert ON course FOR INSERT TO sb_app WITH CHECK (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
CREATE POLICY course_admin_update ON course FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND app_role() = 'tenant_admin') WITH CHECK (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
GRANT SELECT ON course TO sb_worker;
CREATE POLICY course_worker ON course FOR SELECT TO sb_worker USING (tenant_id = app_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON enrollment TO sb_app;
-- A user sees their own enrollments (that is what the chat enrollment check reads); admins see the tenant's.
CREATE POLICY enrollment_read ON enrollment FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (user_id = app_user_id() OR app_role() = 'tenant_admin'));
CREATE POLICY enrollment_admin_insert ON enrollment FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
CREATE POLICY enrollment_admin_update ON enrollment FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND app_role() = 'tenant_admin') WITH CHECK (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
CREATE POLICY enrollment_admin_delete ON enrollment FOR DELETE TO sb_app USING (tenant_id = app_tenant() AND app_role() = 'tenant_admin');
GRANT SELECT ON enrollment TO sb_worker;
CREATE POLICY enrollment_worker ON enrollment FOR SELECT TO sb_worker USING (tenant_id = app_tenant());

-- ----------------------------------------------------------------------------------------------------- documents
GRANT SELECT, INSERT, UPDATE ON document TO sb_app;
CREATE POLICY document_read ON document FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR is_enrolled(course_id)));
CREATE POLICY document_insert ON document FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR is_course_instructor(course_id)));
CREATE POLICY document_update ON document FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR is_course_instructor(course_id)))
  WITH CHECK (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR is_course_instructor(course_id)));
GRANT SELECT, INSERT, UPDATE ON document TO sb_worker;
CREATE POLICY document_worker ON document FOR ALL TO sb_worker USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

GRANT SELECT, INSERT ON document_version TO sb_app;
CREATE POLICY version_read ON document_version FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND app_role() IN ('instructor', 'tenant_admin')
         AND EXISTS (SELECT 1 FROM document d WHERE d.id = document_version.document_id AND d.tenant_id = document_version.tenant_id));
CREATE POLICY version_insert ON document_version FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND EXISTS (SELECT 1 FROM document d WHERE d.id = document_version.document_id
              AND d.tenant_id = document_version.tenant_id AND (app_role() = 'tenant_admin' OR is_course_instructor(d.course_id))));
GRANT SELECT, INSERT, UPDATE ON document_version TO sb_worker;
CREATE POLICY version_worker ON document_version FOR ALL TO sb_worker USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

-- chunks: only the ingestion worker touches them. The request path reads vectors from Qdrant, never this table.
GRANT SELECT, INSERT, UPDATE, DELETE ON chunk TO sb_worker;
CREATE POLICY chunk_worker ON chunk FOR ALL TO sb_worker USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

-- ----------------------------------------------------------------------------------------------------- conversations
GRANT SELECT, INSERT ON conversation TO sb_app;
CREATE POLICY conversation_own_read ON conversation FOR SELECT TO sb_app USING (tenant_id = app_tenant() AND user_id = app_user_id());
CREATE POLICY conversation_own_insert ON conversation FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND user_id = app_user_id() AND is_enrolled(course_id));
GRANT SELECT (id, tenant_id, created_at), DELETE ON conversation TO sb_worker;  -- retention job: no content, ids only
CREATE POLICY conversation_worker ON conversation FOR SELECT TO sb_worker USING (tenant_id = app_tenant());
CREATE POLICY conversation_worker_delete ON conversation FOR DELETE TO sb_worker USING (tenant_id = app_tenant());

-- D4: nobody reads another user's messages directly. Students read their own; tutors go through tutor_read_ticket().
GRANT SELECT, INSERT ON message TO sb_app;
CREATE POLICY message_own_read ON message FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND EXISTS (SELECT 1 FROM conversation c WHERE c.id = message.conversation_id AND c.tenant_id = message.tenant_id));
CREATE POLICY message_own_insert ON message FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND EXISTS (SELECT 1 FROM conversation c WHERE c.id = message.conversation_id AND c.tenant_id = message.tenant_id));
GRANT SELECT (id, tenant_id, conversation_id, created_at), DELETE ON message TO sb_worker;  -- retention: no content
CREATE POLICY message_worker ON message FOR SELECT TO sb_worker USING (tenant_id = app_tenant());
CREATE POLICY message_worker_delete ON message FOR DELETE TO sb_worker USING (tenant_id = app_tenant());

-- Raw chat (D2): the request path may only INSERT it, and only for tenants that opted in. Only the evaluation role
-- reads it; the worker purges expired rows.
GRANT INSERT ON message_raw TO sb_app;
CREATE POLICY raw_insert ON message_raw FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant()
              AND EXISTS (SELECT 1 FROM tenant t WHERE t.id = message_raw.tenant_id AND t.eval_retention)
              AND EXISTS (SELECT 1 FROM message m WHERE m.id = message_raw.message_id AND m.tenant_id = message_raw.tenant_id));
GRANT SELECT ON message_raw TO sb_eval;
CREATE POLICY raw_eval ON message_raw FOR SELECT TO sb_eval
  USING (tenant_id = app_tenant() AND EXISTS (SELECT 1 FROM tenant t WHERE t.id = message_raw.tenant_id AND t.eval_retention));
GRANT SELECT (id, tenant_id, expires_at), DELETE ON message_raw TO sb_worker;
CREATE POLICY raw_worker ON message_raw FOR SELECT TO sb_worker USING (tenant_id = app_tenant());
CREATE POLICY raw_worker_delete ON message_raw FOR DELETE TO sb_worker USING (tenant_id = app_tenant());

-- ----------------------------------------------------------------------------------------------------- tickets
GRANT SELECT, INSERT ON escalation_ticket TO sb_app;
GRANT UPDATE (status, resolution, outcome, approved_faq, assigned_tutor_id, priority, updated_at) ON escalation_ticket TO sb_app;
CREATE POLICY ticket_read ON escalation_ticket FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR assigned_tutor_id = app_user_id()
         OR EXISTS (SELECT 1 FROM conversation c WHERE c.id = escalation_ticket.conversation_id AND c.tenant_id = escalation_ticket.tenant_id)));
CREATE POLICY ticket_insert ON escalation_ticket FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND status = 'open' AND assigned_tutor_id IS NULL
              AND EXISTS (SELECT 1 FROM conversation c WHERE c.id = escalation_ticket.conversation_id AND c.tenant_id = escalation_ticket.tenant_id));
CREATE POLICY ticket_update ON escalation_ticket FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR assigned_tutor_id = app_user_id()))
  WITH CHECK (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR assigned_tutor_id = app_user_id()));
GRANT SELECT, INSERT, UPDATE ON escalation_ticket TO sb_worker;
CREATE POLICY ticket_worker ON escalation_ticket FOR ALL TO sb_worker USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

GRANT SELECT, INSERT, UPDATE ON faq_entry TO sb_app;
CREATE POLICY faq_read ON faq_entry FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (app_role() = 'tenant_admin' OR is_enrolled(course_id)));
CREATE POLICY faq_insert ON faq_entry FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND app_role() IN ('tutor', 'instructor', 'tenant_admin') AND approved_by = app_user_id());
CREATE POLICY faq_update ON faq_entry FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND app_role() IN ('tutor', 'instructor', 'tenant_admin'))
  WITH CHECK (tenant_id = app_tenant() AND app_role() IN ('tutor', 'instructor', 'tenant_admin') AND approved_by = app_user_id());
GRANT SELECT ON faq_entry TO sb_worker;
CREATE POLICY faq_worker ON faq_entry FOR SELECT TO sb_worker USING (tenant_id = app_tenant());

-- ----------------------------------------------------------------------------------------------------- usage, audit, outbox
GRANT SELECT, INSERT, UPDATE ON usage_ledger TO sb_app;
CREATE POLICY ledger_read ON usage_ledger FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND (user_id = app_user_id() OR app_role() = 'tenant_admin'));
CREATE POLICY ledger_write ON usage_ledger FOR INSERT TO sb_app WITH CHECK (tenant_id = app_tenant() AND user_id = app_user_id());
CREATE POLICY ledger_update ON usage_ledger FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND user_id = app_user_id()) WITH CHECK (tenant_id = app_tenant() AND user_id = app_user_id());
GRANT SELECT, INSERT, UPDATE ON usage_ledger TO sb_worker;
CREATE POLICY ledger_worker ON usage_ledger FOR ALL TO sb_worker USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

-- Audit log: append-only. Writers can INSERT; only a tenant admin can read; nobody gets UPDATE or DELETE (a trigger
-- below also blocks the table owner).
GRANT INSERT ON audit_log TO sb_auth, sb_app, sb_worker, sb_reader;
GRANT SELECT ON audit_log TO sb_app;
CREATE POLICY audit_insert ON audit_log FOR INSERT TO sb_auth, sb_app, sb_worker, sb_reader WITH CHECK (tenant_id = app_tenant());
CREATE POLICY audit_read ON audit_log FOR SELECT TO sb_app USING (tenant_id = app_tenant() AND app_role() = 'tenant_admin');

GRANT INSERT ON outbox_event TO sb_auth, sb_app;
GRANT SELECT, INSERT ON outbox_event TO sb_worker;
GRANT UPDATE (published_at) ON outbox_event TO sb_worker;
CREATE POLICY outbox_insert ON outbox_event FOR INSERT TO sb_auth, sb_app WITH CHECK (tenant_id = app_tenant());
CREATE POLICY outbox_worker ON outbox_event FOR ALL TO sb_worker USING (tenant_id = app_tenant()) WITH CHECK (tenant_id = app_tenant());

-- ----------------------------------------------------------------------------------------------------- guards (triggers)
-- tenant_id never changes on a row.
CREATE FUNCTION forbid_tenant_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'tenant_id is immutable' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION reject_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '42501';
END $$;

CREATE FUNCTION document_version_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.document_id, NEW.version_no, NEW.object_key, NEW.sha256) IS DISTINCT FROM (OLD.document_id, OLD.version_no, OLD.object_key, OLD.sha256) THEN
    RAISE EXCEPTION 'document_version content is immutable; upload a new version' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

-- Only a tenant admin (or the system worker) may assign or reassign a ticket; a tutor cannot hand tickets to themselves.
CREATE FUNCTION ticket_assignment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.assigned_tutor_id IS DISTINCT FROM OLD.assigned_tutor_id AND app_role() IS DISTINCT FROM 'tenant_admin'
     AND current_user <> 'sb_worker' THEN
    RAISE EXCEPTION 'only a tenant admin can assign tickets' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['app_user', 'course', 'enrollment', 'document', 'document_version', 'chunk', 'conversation', 'message',
                           'message_raw', 'escalation_ticket', 'faq_entry', 'usage_ledger', 'audit_log', 'outbox_event'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION forbid_tenant_change()', t || '_tenant_fixed', t);
  END LOOP;
END $$;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION reject_change();
CREATE TRIGGER message_append_only BEFORE UPDATE ON message FOR EACH ROW EXECUTE FUNCTION reject_change();
CREATE TRIGGER document_version_content BEFORE UPDATE ON document_version FOR EACH ROW EXECUTE FUNCTION document_version_immutable();
CREATE TRIGGER ticket_assignment BEFORE UPDATE ON escalation_ticket FOR EACH ROW EXECUTE FUNCTION ticket_assignment_guard();

-- ----------------------------------------------------------------------------------------------------- D4: audited tutor read
-- The ONLY way to read a conversation you do not own. SECURITY DEFINER, owned by sb_reader (a role with no login and
-- read policies scoped to the session's tenant). The function checks the ticket is assigned to the caller, writes the
-- audit row, and only then returns messages, so an unaudited read is impossible by construction.
GRANT SELECT ON message, escalation_ticket TO sb_reader;
GRANT SELECT ON message_raw TO sb_reader;
CREATE POLICY message_reader ON message FOR SELECT TO sb_reader USING (tenant_id = app_tenant());
CREATE POLICY ticket_reader ON escalation_ticket FOR SELECT TO sb_reader USING (tenant_id = app_tenant());
CREATE POLICY raw_reader ON message_raw FOR SELECT TO sb_reader USING (tenant_id = app_tenant());

CREATE FUNCTION tutor_read_ticket(p_ticket uuid, p_include_raw boolean DEFAULT false)
RETURNS TABLE (message_id uuid, role text, content_redacted text, raw_content text, created_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
  v_conversation uuid;
BEGIN
  IF public.app_role() IS DISTINCT FROM 'tutor' OR public.app_tenant() IS NULL OR public.app_user_id() IS NULL THEN
    RAISE EXCEPTION 'only a tutor can read a ticket conversation' USING ERRCODE = '42501';
  END IF;
  SELECT t.conversation_id INTO v_conversation
    FROM public.escalation_ticket t
   WHERE t.id = p_ticket AND t.tenant_id = public.app_tenant() AND t.assigned_tutor_id = public.app_user_id();
  IF v_conversation IS NULL THEN
    RAISE EXCEPTION 'ticket not found or not assigned to you' USING ERRCODE = '42501';
  END IF;
  INSERT INTO public.audit_log (tenant_id, actor_id, action, target)
  VALUES (public.app_tenant(), public.app_user_id(), CASE WHEN p_include_raw THEN 'raw_chat.read' ELSE 'chat.read' END,
          'ticket:' || p_ticket::text);
  RETURN QUERY
    SELECT m.id, m.role, m.content_redacted,
           CASE WHEN p_include_raw THEN (SELECT r.content FROM public.message_raw r
                                          WHERE r.message_id = m.id AND r.tenant_id = m.tenant_id LIMIT 1) END,
           m.created_at
      FROM public.message m
     WHERE m.conversation_id = v_conversation AND m.tenant_id = public.app_tenant()
     ORDER BY m.created_at, m.id;
END $$;
-- ALTER OWNER needs the new owner to hold CREATE on the schema; grant it only for this statement.
GRANT CREATE ON SCHEMA public TO sb_reader;
ALTER FUNCTION tutor_read_ticket(uuid, boolean) OWNER TO sb_reader;
REVOKE CREATE ON SCHEMA public FROM sb_reader;
REVOKE ALL ON FUNCTION tutor_read_ticket(uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION tutor_read_ticket(uuid, boolean) TO sb_app;
