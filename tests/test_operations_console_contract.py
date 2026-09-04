"""Digest and binding tests for the promoted operations-console API contract.

``contracts/catalog-api/operations-console/v1`` is a byte-for-byte promotion of
catalog-api's ``api/contracts/operations-console/v1``. ``source.json`` records the
producer commit it was taken from plus the SHA-256 of both promoted files, and
``scripts/check-contracts.py`` enforces those digests in ``just source-check``.
These tests cover the same digests from the pytest suite and additionally assert
that the operations this console actually calls are present in the promoted
routes and exposed by the generated binding, so a promotion that drops one fails
here rather than at runtime.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from dashboard import catalog_admin_contract


ROOT = Path(__file__).parent.parent
CONTRACT_ROOT = ROOT / "contracts/catalog-api/operations-console/v1"
SOURCE = json.loads((CONTRACT_ROOT / "source.json").read_text())
ROUTES = json.loads((CONTRACT_ROOT / "routes.json").read_text())


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


class TestPromotedContractDigests:
    def test_routes_digest_matches_the_recorded_pin(self) -> None:
        assert _digest(CONTRACT_ROOT / "routes.json") == SOURCE["contract_sha256"]

    def test_binding_digest_matches_the_recorded_pin(self) -> None:
        assert _digest(ROOT / SOURCE["binding"]) == SOURCE["binding_sha256"]

    def test_producer_commit_is_a_full_sha(self) -> None:
        producer_commit = SOURCE["producer_commit"]
        assert len(producer_commit) == 40
        assert all(character in "0123456789abcdef" for character in producer_commit)

    def test_contract_identity_and_version_are_pinned(self) -> None:
        assert ROUTES["contract"] == "groovemap.operations-console-api"
        assert ROUTES["version"] == 1
        assert SOURCE["version"] == 1
        assert catalog_admin_contract.CONTRACT_VERSION == 1


class TestPromotedContractOperations:
    def test_unmapped_media_operation_is_promoted(self) -> None:
        """The media-mapping coverage view reads this route for both providers."""
        assert ROUTES["operations"]["admin_unmapped_media"] == {
            "method": "GET",
            "path": "/api/admin/media/unmapped",
        }

    def test_binding_exposes_every_promoted_operation(self) -> None:
        for name, operation in ROUTES["operations"].items():
            assert getattr(catalog_admin_contract, f"{name.upper()}_METHOD") == operation["method"]
            assert getattr(catalog_admin_contract, f"{name.upper()}_PATH") == operation["path"]
