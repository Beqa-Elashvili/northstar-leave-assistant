-- Defence in depth for Supabase: the HR data is reachable only through the backend
-- (MCP server / scripts using DATABASE_URL). Supabase grants the public API roles `anon` and
-- `authenticated` full privileges on new tables by default; RLS without policies already
-- returns no rows to them, and this migration also removes the privileges themselves, so a
-- future mistaken RLS policy cannot expose employee or leave data through the REST API.
--
-- On plain PostgreSQL (no such roles) this migration does nothing.

DO $$
DECLARE
    api_role text;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', current_schema(), api_role);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', current_schema(), api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON TABLES FROM %I',
                           current_schema(), api_role);
            EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM %I',
                           current_schema(), api_role);
        END IF;
    END LOOP;
END
$$;
