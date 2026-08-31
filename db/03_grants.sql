-- Winback — roles and grants
--
-- Three roles, because "who is allowed to move money" should be answerable from the
-- database and not only from the code:
--
--   winback_owner  DDL owner (the POSTGRES_USER). Migrations only. Nothing at
--                  runtime connects as this.
--   winback_agent  What the agent and the simulator connect as. Can insert facts,
--                  can advance invoice/subscription state, can NEVER rewrite history.
--   winback_reader What the FastAPI backend and therefore the dashboard connect as.
--                  SELECT only. A read-only dashboard cannot corrupt an audit trail
--                  even if the API has a bug.
--
-- Dev passwords only; production would use IAM/scram with rotated secrets. The
-- values here match .env.example and never leave the Docker network.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'winback_agent') THEN
        CREATE ROLE winback_agent LOGIN PASSWORD 'winback_agent_dev';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'winback_reader') THEN
        CREATE ROLE winback_reader LOGIN PASSWORD 'winback_reader_dev';
    END IF;
END;
$$;

GRANT CONNECT ON DATABASE winback TO winback_agent, winback_reader;
GRANT USAGE   ON SCHEMA public    TO winback_agent, winback_reader;

-- ---------------------------------------------------------------- reader
GRANT SELECT ON ALL TABLES IN SCHEMA public TO winback_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO winback_reader;

-- ---------------------------------------------------------------- agent
-- Mutable state: the agent advances an invoice from at_risk -> recovered, and a
-- subscription from pending -> active. These are current state, not history.
GRANT SELECT, INSERT, UPDATE ON invoices, subscriptions, customers TO winback_agent;

-- Immutable facts: INSERT and SELECT only. No UPDATE. No DELETE. The trigger in
-- 02_append_only.sql is the belt; this is the braces.
GRANT SELECT, INSERT ON audit_log, decisions, payment_attempts TO winback_agent;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log, decisions, payment_attempts FROM winback_agent;

-- Evaluation bookkeeping is regenerated per run, so it is genuinely mutable. Not an
-- inconsistency with the append-only tables above: these hold derived summaries that
-- `python -m eval.report` recomputes from the facts, and a summary you cannot
-- recompute in place is a summary that drifts from the facts it summarises.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON eval_runs, eval_arm_results, eval_arm_violations, eval_intervals
    TO winback_agent;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO winback_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO winback_agent;

-- The one sanctioned path that removes immutable rows (see 02_append_only.sql).
REVOKE EXECUTE ON FUNCTION winback_reset_world() FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION winback_reset_world() TO winback_agent;

COMMIT;
