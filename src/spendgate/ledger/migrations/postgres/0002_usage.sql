-- Task 3.5: retention deletes by age and by session. (The calls indexes on finished_at,
-- (key_id, finished_at) and (model, finished_at) already exist in 0001.)
CREATE INDEX reservations_finished ON reservations(finished_at) WHERE state <> 'pending';
CREATE INDEX reservations_session ON reservations(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX scopes_closed ON scopes(closed_at) WHERE closed_at IS NOT NULL;
