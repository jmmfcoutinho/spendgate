-- ADR-0010: `calls.http_status` (the provider's HTTP status for the attempt; NULL when no answer arrived) is a real status.
ALTER TABLE calls ADD CONSTRAINT calls_http_status_range
  CHECK (http_status IS NULL OR (http_status >= 100 AND http_status <= 599));
