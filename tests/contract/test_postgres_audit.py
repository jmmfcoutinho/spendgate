"""admin_audit is append-only on Postgres: a trigger for everyone, REVOKE for the app role."""

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from tests.contract.rig import Rig, workdir_location

pytestmark = pytest.mark.postgres

INSERT_ROW = (
    "INSERT INTO admin_audit (id, at, actor_key_id, action, target_type, target_id) "
    "VALUES ('a1', 1, 'admin', 'budget.set_cap', 'budget', 'b1')"
)


@pytest.fixture
def conn(
    postgres_url: str, tmp_path: Path, new_id: Callable[[], str]
) -> Iterator[psycopg.Connection]:
    rig = Rig("postgres", workdir_location("postgres", tmp_path), new_id)
    rig.close_connection()  # the schema is migrated; this test talks to it directly
    with psycopg.connect(rig.location, autocommit=True) as connection:
        connection.execute(INSERT_ROW)
        yield connection
    rig.close()


@pytest.mark.parametrize(
    "statement",
    ["UPDATE admin_audit SET action = 'x'", "DELETE FROM admin_audit", "TRUNCATE admin_audit"],
)
def test_the_trigger_refuses_changes_even_for_the_table_owner(
    conn: psycopg.Connection, statement: str
) -> None:
    with pytest.raises(errors.IntegrityConstraintViolation, match="append-only"):
        conn.execute(statement)
    assert conn.execute("SELECT count(*) FROM admin_audit").fetchone() == (1,)


def test_inserts_still_work(conn: psycopg.Connection) -> None:
    conn.execute(INSERT_ROW.replace("'a1'", "'a2'"))
    assert conn.execute("SELECT count(*) FROM admin_audit").fetchone() == (2,)


def test_the_documented_revoke_stops_an_app_role_before_the_trigger_runs(
    conn: psycopg.Connection,
) -> None:
    role = f"sg_app_{secrets.token_hex(4)}"
    schema = conn.execute("SELECT current_schema()").fetchone()
    assert schema is not None
    try:
        conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
    except errors.InsufficientPrivilege:
        pytest.skip("the test database user may not create roles")
    try:
        conn.execute(f'GRANT USAGE ON SCHEMA "{schema[0]}" TO "{role}"')
        conn.execute(  # the app role may do everything ...
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema[0]}" '
            f'TO "{role}"'
        )
        # ... except what the migration's comment says to revoke:
        conn.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON admin_audit FROM "{role}"')
        conn.execute(f'SET ROLE "{role}"')
        conn.execute(INSERT_ROW.replace("'a1'", "'a3'"))
        for statement in ("UPDATE admin_audit SET action = 'x'", "DELETE FROM admin_audit"):
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute(statement)
    finally:
        conn.execute("RESET ROLE")
        conn.execute(f'DROP OWNED BY "{role}"')
        conn.execute(f'DROP ROLE "{role}"')
