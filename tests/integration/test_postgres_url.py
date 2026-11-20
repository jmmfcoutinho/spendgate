import pytest


@pytest.mark.postgres
def test_postgres_url_is_provided_or_the_test_skips(postgres_url: str) -> None:
    assert postgres_url.startswith(("postgres://", "postgresql://"))
