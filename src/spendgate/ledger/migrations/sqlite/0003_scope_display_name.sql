-- Task 5.4: the admin API's `display_name` on scopes (ScopeCreate, ScopeUpdate). Free text, not part of the path.
ALTER TABLE scopes ADD COLUMN display_name TEXT;
