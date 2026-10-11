-- Escalation (LLD 10): ticket lifecycle, tutor queue and claim, student actions, replies, bounded tutor window.

-- ------------------------------------------------------------------------------------------------ ticket shape
ALTER TABLE escalation_ticket DROP CONSTRAINT escalation_ticket_status_check;
ALTER TABLE escalation_ticket ADD CONSTRAINT escalation_ticket_status_check
  CHECK (status IN ('open', 'assigned', 'in_progress', 'resolved', 'closed_no_response', 'reopened', 'closed'));
ALTER TABLE escalation_ticket
  ADD COLUMN student_id uuid,
  ADD COLUMN trigger text NOT NULL DEFAULT 'router',
  ADD COLUMN restricted boolean NOT NULL DEFAULT false,   -- sensitive: only designated tutors may claim (D5)
  ADD COLUMN dedup_key text,
  ADD COLUMN claimed_at timestamptz,
  ADD COLUMN first_response_at timestamptz,
  ADD COLUMN resolved_at timestamptz,
  ADD COLUMN bot_could_have_answered boolean,
  ADD COLUMN retrieval_gap boolean,
  ADD COLUMN notify_count integer NOT NULL DEFAULT 0,
  ADD COLUMN notify_level smallint NOT NULL DEFAULT 0 CHECK (notify_level BETWEEN 0 AND 2),  -- 0 tutors, 1 instructor, 2 admin
  ADD COLUMN last_notified_at timestamptz,
  ADD COLUMN breach_alerted_at timestamptz;
ALTER TABLE escalation_ticket ADD FOREIGN KEY (tenant_id, student_id) REFERENCES app_user (tenant_id, id);
CREATE INDEX ticket_student_idx ON escalation_ticket (tenant_id, student_id, course_id, created_at DESC);
CREATE INDEX ticket_queue_idx ON escalation_ticket (tenant_id, course_id, created_at) WHERE assigned_tutor_id IS NULL;

-- Tutors designated for sensitive tickets (D5) are marked on the roster row.
ALTER TABLE enrollment ADD COLUMN sensitive_ok boolean NOT NULL DEFAULT false;

CREATE FUNCTION is_course_tutor(p_course uuid, p_sensitive boolean) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM enrollment e WHERE e.tenant_id = app_tenant() AND e.course_id = p_course AND e.user_id = app_user_id()
                 AND e.role_in_course = 'tutor' AND (NOT p_sensitive OR e.sensitive_ok)) $$;

-- ------------------------------------------------------------------------------------------------ policies
GRANT UPDATE (claimed_at, first_response_at, resolved_at, bot_could_have_answered, retrieval_gap) ON escalation_ticket TO sb_app;

DROP POLICY ticket_insert ON escalation_ticket;
CREATE POLICY ticket_insert ON escalation_ticket FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND status = 'open' AND assigned_tutor_id IS NULL AND student_id = app_user_id()
              AND EXISTS (SELECT 1 FROM conversation c WHERE c.id = escalation_ticket.conversation_id AND c.tenant_id = escalation_ticket.tenant_id));

-- The unclaimed queue: a tutor of the course sees ticket METADATA (never the conversation, which needs assignment);
-- restricted tickets only to designated tutors.
CREATE POLICY ticket_queue ON escalation_ticket FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND app_role() = 'tutor' AND assigned_tutor_id IS NULL AND status IN ('open', 'reopened')
         AND is_course_tutor(course_id, restricted));
-- Claim (D1): first tutor wins; the row condition is re-checked on conflict, so the loser updates 0 rows.
CREATE POLICY ticket_claim ON escalation_ticket FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND app_role() = 'tutor' AND assigned_tutor_id IS NULL AND status IN ('open', 'reopened')
         AND is_course_tutor(course_id, restricted))
  WITH CHECK (tenant_id = app_tenant() AND assigned_tutor_id = app_user_id() AND status = 'assigned');
-- The student sees and may reopen/cancel their own ticket (a trigger limits what they may change).
CREATE POLICY ticket_student_read ON escalation_ticket FOR SELECT TO sb_app USING (tenant_id = app_tenant() AND student_id = app_user_id());
CREATE POLICY ticket_student_update ON escalation_ticket FOR UPDATE TO sb_app
  USING (tenant_id = app_tenant() AND student_id = app_user_id() AND app_role() = 'student')
  WITH CHECK (tenant_id = app_tenant() AND student_id = app_user_id());
-- An instructor sees tickets of their own course once they were escalated past the tutors.
CREATE POLICY ticket_instructor_read ON escalation_ticket FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND app_role() = 'instructor' AND notify_level >= 1 AND is_course_instructor(course_id));

CREATE FUNCTION ticket_actor_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r text := app_role();
BEGIN
  IF current_user <> 'sb_app' THEN RETURN NEW; END IF;  -- the system worker (SLA monitor) is trusted code
  IF (NEW.student_id, NEW.course_id, NEW.conversation_id, NEW.message_id, NEW.reason, NEW.trigger, NEW.restricted, NEW.dedup_key, NEW.created_at, NEW.sla_deadline)
     IS DISTINCT FROM
     (OLD.student_id, OLD.course_id, OLD.conversation_id, OLD.message_id, OLD.reason, OLD.trigger, OLD.restricted, OLD.dedup_key, OLD.created_at, OLD.sla_deadline) THEN
    RAISE EXCEPTION 'ticket origin fields are immutable' USING ERRCODE = '42501';
  END IF;
  IF (NEW.notify_count, NEW.notify_level, NEW.last_notified_at, NEW.breach_alerted_at)
     IS DISTINCT FROM (OLD.notify_count, OLD.notify_level, OLD.last_notified_at, OLD.breach_alerted_at) THEN
    RAISE EXCEPTION 'notification state is system-managed' USING ERRCODE = '42501';
  END IF;
  IF r = 'student' THEN
    IF (NEW.priority, NEW.assigned_tutor_id, NEW.resolution, NEW.outcome, NEW.approved_faq, NEW.bot_could_have_answered, NEW.retrieval_gap,
        NEW.claimed_at, NEW.first_response_at, NEW.resolved_at)
       IS DISTINCT FROM
       (OLD.priority, OLD.assigned_tutor_id, OLD.resolution, OLD.outcome, OLD.approved_faq, OLD.bot_could_have_answered, OLD.retrieval_gap,
        OLD.claimed_at, OLD.first_response_at, OLD.resolved_at) THEN
      RAISE EXCEPTION 'a student can only reopen or cancel a ticket' USING ERRCODE = '42501';
    END IF;
    IF NOT ((OLD.status = 'resolved' AND NEW.status = 'reopened')
            OR (OLD.status IN ('open', 'assigned', 'in_progress', 'reopened') AND NEW.status = 'closed')) THEN
      RAISE EXCEPTION 'that status change is not allowed' USING ERRCODE = '42501';
    END IF;
  ELSIF r = 'tutor' THEN
    IF NEW.priority IS DISTINCT FROM OLD.priority THEN
      RAISE EXCEPTION 'a tutor cannot change priority' USING ERRCODE = '42501';
    END IF;
    IF NEW.assigned_tutor_id IS DISTINCT FROM OLD.assigned_tutor_id
       AND NOT (OLD.assigned_tutor_id IS NULL AND NEW.assigned_tutor_id = app_user_id()) THEN
      RAISE EXCEPTION 'a tutor can only claim an unassigned ticket for themselves' USING ERRCODE = '42501';
    END IF;
    IF NEW.status NOT IN ('assigned', 'in_progress', 'resolved') THEN
      RAISE EXCEPTION 'that status change is not allowed' USING ERRCODE = '42501';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ticket_actor BEFORE UPDATE ON escalation_ticket FOR EACH ROW EXECUTE FUNCTION ticket_actor_guard();

-- ticket_assignment_guard (002) still blocks hand-offs by anyone but an admin or the worker; claiming is the one exception.
CREATE OR REPLACE FUNCTION ticket_assignment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.assigned_tutor_id IS DISTINCT FROM OLD.assigned_tutor_id AND app_role() IS DISTINCT FROM 'tenant_admin'
     AND current_user <> 'sb_worker'
     AND NOT (app_role() = 'tutor' AND OLD.assigned_tutor_id IS NULL AND NEW.assigned_tutor_id = app_user_id()) THEN
    RAISE EXCEPTION 'only a tenant admin can assign tickets' USING ERRCODE = '42501';
  END IF;
  RETURN NEW;
END $$;

-- ------------------------------------------------------------------------------------------------ replies
CREATE TABLE ticket_reply (
  id uuid PRIMARY KEY DEFAULT uuid_v7(),
  tenant_id uuid NOT NULL,
  ticket_id uuid NOT NULL,
  author_id uuid NOT NULL,
  author_role text NOT NULL CHECK (author_role IN ('student', 'tutor')),
  body text NOT NULL CHECK (length(body) BETWEEN 1 AND 4000),  -- already PII-redacted by the caller
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, ticket_id) REFERENCES escalation_ticket (tenant_id, id),
  FOREIGN KEY (tenant_id, author_id) REFERENCES app_user (tenant_id, id)
);
CREATE INDEX ticket_reply_idx ON ticket_reply (tenant_id, ticket_id, created_at);
ALTER TABLE ticket_reply ENABLE ROW LEVEL SECURITY;
ALTER TABLE ticket_reply FORCE ROW LEVEL SECURITY;
REVOKE ALL ON ticket_reply FROM PUBLIC;
GRANT SELECT, INSERT ON ticket_reply TO sb_app;
GRANT SELECT ON ticket_reply TO sb_worker;
CREATE POLICY reply_read ON ticket_reply FOR SELECT TO sb_app
  USING (tenant_id = app_tenant() AND EXISTS (SELECT 1 FROM escalation_ticket t WHERE t.id = ticket_reply.ticket_id AND t.tenant_id = ticket_reply.tenant_id
         AND (t.student_id = app_user_id() OR t.assigned_tutor_id = app_user_id() OR app_role() = 'tenant_admin')));
CREATE POLICY reply_insert ON ticket_reply FOR INSERT TO sb_app
  WITH CHECK (tenant_id = app_tenant() AND author_id = app_user_id() AND EXISTS (
    SELECT 1 FROM escalation_ticket t WHERE t.id = ticket_reply.ticket_id AND t.tenant_id = ticket_reply.tenant_id
      AND t.status NOT IN ('closed', 'closed_no_response')
      AND ((author_role = 'student' AND t.student_id = app_user_id() AND app_role() = 'student')
        OR (author_role = 'tutor' AND t.assigned_tutor_id = app_user_id() AND app_role() = 'tutor'))));
CREATE POLICY reply_worker ON ticket_reply FOR SELECT TO sb_worker USING (tenant_id = app_tenant());
CREATE TRIGGER ticket_reply_append_only BEFORE UPDATE OR DELETE ON ticket_reply FOR EACH ROW EXECUTE FUNCTION reject_change();

-- ------------------------------------------------------------------------------------------------ bounded tutor window (D4)
-- Replaces the 002 version (same signature, so owner and grants stay): the tutor gets the escalated message, up to 10
-- messages before it, and the bot's answer right after it (the "attempted answer"). Never later messages, never the
-- student's whole history.
CREATE OR REPLACE FUNCTION tutor_read_ticket(p_ticket uuid, p_include_raw boolean DEFAULT false)
RETURNS TABLE (message_id uuid, role text, content_redacted text, raw_content text, created_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
  v_conversation uuid;
  v_message uuid;
  v_msg_at timestamptz;
  v_upper_at timestamptz;
  v_upper_id uuid;
BEGIN
  IF public.app_role() IS DISTINCT FROM 'tutor' OR public.app_tenant() IS NULL OR public.app_user_id() IS NULL THEN
    RAISE EXCEPTION 'only a tutor can read a ticket conversation' USING ERRCODE = '42501';
  END IF;
  SELECT t.conversation_id, t.message_id INTO v_conversation, v_message
    FROM public.escalation_ticket t
   WHERE t.id = p_ticket AND t.tenant_id = public.app_tenant() AND t.assigned_tutor_id = public.app_user_id();
  IF v_conversation IS NULL THEN
    RAISE EXCEPTION 'ticket not found or not assigned to you' USING ERRCODE = '42501';
  END IF;
  INSERT INTO public.audit_log (tenant_id, actor_id, action, target)
  VALUES (public.app_tenant(), public.app_user_id(), CASE WHEN p_include_raw THEN 'raw_chat.read' ELSE 'chat.read' END,
          'ticket:' || p_ticket::text);
  SELECT m.created_at INTO v_msg_at FROM public.message m WHERE m.id = v_message AND m.tenant_id = public.app_tenant();
  SELECT m.created_at, m.id INTO v_upper_at, v_upper_id FROM public.message m
   WHERE m.conversation_id = v_conversation AND m.tenant_id = public.app_tenant() AND m.role = 'assistant' AND m.created_at >= v_msg_at
   ORDER BY m.created_at, m.id LIMIT 1;
  IF v_upper_at IS NULL THEN v_upper_at := v_msg_at; v_upper_id := v_message; END IF;
  RETURN QUERY
    SELECT w.id, w.role, w.content_redacted,
           CASE WHEN p_include_raw THEN (SELECT r.content FROM public.message_raw r
                                          WHERE r.message_id = w.id AND r.tenant_id = w.tenant_id LIMIT 1) END,
           w.created_at
      FROM (SELECT m.* FROM public.message m
             WHERE m.conversation_id = v_conversation AND m.tenant_id = public.app_tenant()
               AND (m.created_at, m.id) <= (v_upper_at, v_upper_id)
             ORDER BY m.created_at DESC, m.id DESC LIMIT 12) w
     ORDER BY w.created_at, w.id;
END $$;
