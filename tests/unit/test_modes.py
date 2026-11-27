"""`serves`: which endpoint kinds an entry's catalog mode may be sent (ADR-0033)."""

import pytest

from spendgate.core.catalog import PriceEntry
from spendgate.core.estimate import RequestKind
from spendgate.core.modes import serves

TEXT_MODES = ("chat", "responses", "messages")
TEXT_KINDS = {RequestKind.CHAT, RequestKind.RESPONSES, RequestKind.MESSAGES}


@pytest.mark.parametrize("kind", list(RequestKind))
@pytest.mark.parametrize("mode", [*TEXT_MODES, "embedding", "transcription", "speech"])
def test_t_dos_06_an_entry_serves_only_endpoints_of_its_mode(kind: RequestKind, mode: str) -> None:
    """The text modes are one family (an adapter picks among them); the rest need their own."""
    expected = (mode in TEXT_MODES) if kind in TEXT_KINDS else mode == kind.value
    assert serves(mode, kind) is expected


def test_every_catalog_mode_serves_some_endpoint_kind() -> None:
    """A new mode without a kind would be refused everywhere: the rule must know every mode."""
    modes = PriceEntry.model_fields["mode"].annotation.__args__  # type: ignore[union-attr]
    for mode in modes:
        assert any(serves(mode, kind) for kind in RequestKind), mode
