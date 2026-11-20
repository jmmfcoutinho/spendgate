-- ADR-0010: `calls.http_status` must be NULL or 100-599. SQLite cannot add a CHECK to an existing table
-- without rebuilding it (a large, risky copy of the audit table), so triggers abort out-of-range writes.
CREATE TRIGGER calls_http_status_insert BEFORE INSERT ON calls
WHEN NEW.http_status IS NOT NULL AND (NEW.http_status < 100 OR NEW.http_status > 599)
BEGIN
  SELECT RAISE(ABORT, 'CHECK constraint failed: calls.http_status');
END;
CREATE TRIGGER calls_http_status_update BEFORE UPDATE OF http_status ON calls
WHEN NEW.http_status IS NOT NULL AND (NEW.http_status < 100 OR NEW.http_status > 599)
BEGIN
  SELECT RAISE(ABORT, 'CHECK constraint failed: calls.http_status');
END;
