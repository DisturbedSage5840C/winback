-- Winback — append-only enforcement
--
-- An audit trail that the application is *trusted* not to modify is not an audit
-- trail. Three independent layers here, because a panelist will ask and each layer
-- alone has a hole:
--
--   1. A BEFORE UPDATE OR DELETE trigger that raises. Blocks the application even
--      when it connects as the owner, and blocks anyone at a psql prompt.
--   2. Revoked UPDATE/DELETE grants for the app role (03_grants.sql). Defence in
--      depth: the trigger can be disabled by a superuser, the grant cannot be
--      restored by the app role itself.
--   3. TRUNCATE is caught separately -- it is not an UPDATE or a DELETE and would
--      otherwise walk straight through layer 1.
--
-- Deliberately NOT protected: invoices.status and subscriptions.status are live
-- state and are meant to change. The facts (attempts, decisions, audit events) are
-- immutable; the state derived from them is not.

BEGIN;

CREATE OR REPLACE FUNCTION winback_reject_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    -- OLD is unassigned in a statement-level trigger (TRUNCATE), and reading it
    -- there raises 55000 instead of our own error -- which would make the TRUNCATE
    -- test pass for entirely the wrong reason.
    offending_row text := CASE WHEN TG_LEVEL = 'ROW' THEN OLD::text ELSE '(statement)' END;
BEGIN
    RAISE EXCEPTION
        'append_only_violation: % on %.% is not permitted (row: %)',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME, offending_row
        USING ERRCODE = 'restrict_violation',
              HINT = 'This table is append-only. Correct a record by inserting a '
                     'superseding row, never by mutating history.';
END;
$$;

COMMENT ON FUNCTION winback_reject_mutation() IS
    'Raises restrict_violation on any UPDATE/DELETE/TRUNCATE. Attached to the '
    'immutable-fact tables: audit_log, decisions, payment_attempts.';

-- ---------------------------------------------------------------- audit_log
CREATE TRIGGER audit_log_no_mutate
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION winback_reject_mutation();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION winback_reject_mutation();

-- ---------------------------------------------------------------- decisions
CREATE TRIGGER decisions_no_mutate
    BEFORE UPDATE OR DELETE ON decisions
    FOR EACH ROW EXECUTE FUNCTION winback_reject_mutation();

CREATE TRIGGER decisions_no_truncate
    BEFORE TRUNCATE ON decisions
    FOR EACH STATEMENT EXECUTE FUNCTION winback_reject_mutation();

-- ---------------------------------------------------------------- payment_attempts
-- An attempt happened or it did not. Rewriting one would rewrite both the training
-- data and the evaluation, which is precisely the failure mode this project claims
-- to guard against.
CREATE TRIGGER payment_attempts_no_mutate
    BEFORE UPDATE OR DELETE ON payment_attempts
    FOR EACH ROW EXECUTE FUNCTION winback_reject_mutation();

CREATE TRIGGER payment_attempts_no_truncate
    BEFORE TRUNCATE ON payment_attempts
    FOR EACH STATEMENT EXECUTE FUNCTION winback_reject_mutation();

COMMIT;

-- ---------------------------------------------------------------------------
-- Regenerating the dataset
-- ---------------------------------------------------------------------------
-- sim/generate.py rebuilds the world from scratch, which means dropping immutable
-- rows -- legitimately, because it is rebuilding history, not editing it. It calls
-- winback_reset_world() rather than being handed DELETE rights, so the escape hatch
-- is a single named, greppable, logged function instead of an ambient permission.
CREATE OR REPLACE FUNCTION winback_reset_world()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    RAISE WARNING 'winback_reset_world: dropping all facts and rebuilding the world';

    ALTER TABLE audit_log         DISABLE TRIGGER audit_log_no_mutate;
    ALTER TABLE decisions         DISABLE TRIGGER decisions_no_mutate;
    ALTER TABLE payment_attempts  DISABLE TRIGGER payment_attempts_no_mutate;

    -- Children before parents: eval_intervals and eval_arm_violations both point
    -- at eval_arm_results, which points at eval_runs.
    DELETE FROM eval_intervals;
    DELETE FROM eval_arm_violations;
    DELETE FROM eval_arm_results;
    DELETE FROM eval_runs;
    DELETE FROM audit_log;
    DELETE FROM decisions;
    DELETE FROM payment_attempts;
    DELETE FROM invoices;
    DELETE FROM subscriptions;
    DELETE FROM customers;

    ALTER TABLE audit_log         ENABLE TRIGGER audit_log_no_mutate;
    ALTER TABLE decisions         ENABLE TRIGGER decisions_no_mutate;
    ALTER TABLE payment_attempts  ENABLE TRIGGER payment_attempts_no_mutate;

    ALTER SEQUENCE audit_log_event_id_seq RESTART WITH 1;
END;
$$;

COMMENT ON FUNCTION winback_reset_world() IS
    'The ONLY sanctioned path that removes immutable rows. Used by sim/generate.py '
    'to rebuild the synthetic world. Never called by the agent or the API.';
