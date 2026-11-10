"""Verify promoted contracts, generated bindings, and immutable dependency pins."""

import json
import tomllib
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    """Return a file's hexadecimal SHA-256 digest."""
    return sha256(path.read_bytes()).hexdigest()


persistence_source = json.loads((ROOT / "contracts/persistence/v1/source.json").read_text())
compatibility = json.loads((ROOT / "contracts/persistence/v1/compatibility.json").read_text())
admin_root = ROOT / "contracts/catalog-api/operations-console/v1"
admin_source = json.loads((admin_root / "source.json").read_text())
with (ROOT / "pyproject.toml").open("rb") as source:
    pyproject = tomllib.load(source)

# groovemap.catalog-events/v1 is produced independently by two source-owned repositories
# (ADR 0005); the console composes the contract from both promoted producers rather than
# from one combined upstream.
for catalog_producer in ("discogs", "musicbrainz"):
    producer_root = ROOT / "contracts/catalog-events/v1" / catalog_producer
    catalog_source = json.loads((producer_root / "source.json").read_text())
    assert digest(producer_root / "contract.json") == catalog_source["contract_sha256"]
    assert digest(producer_root / "python/catalog_contract.py") == catalog_source["binding_sha256"]

assert digest(ROOT / "contracts/persistence/v1/compatibility.json") == persistence_source["contract_sha256"]
assert compatibility["contract"] == "groovemap.persistence"
assert compatibility["version"] == 1
assert compatibility["application_runtime"]["tested_version"] == "0.1.0"
runtime_source = pyproject["tool"]["uv"]["sources"]["groovemap-runtime"]
assert runtime_source["rev"] == compatibility["application_runtime"]["tested_commit"]

assert digest(admin_root / "routes.json") == admin_source["contract_sha256"]
assert digest(ROOT / admin_source["binding"]) == admin_source["binding_sha256"]
assert admin_source["version"] == 1
assert len(admin_source["producer_commit"]) == 40

# Every operation key this console has been promoted to understand. A key the producer adds
# that is not listed here fails the check, so an unverified promise cannot slip in silently.
# error_fields and error_field_semantics describe the non-2xx body a caller can rely on.
admin_operation_keys = {
    "method",
    "path",
    "query",
    "request_body",
    "response_fields",
    "response_fields_nullable",
    "kinds",
    "dated_from",
    "events",
    "error_fields",
    "error_field_semantics",
}
admin_routes = json.loads((admin_root / "routes.json").read_text())
for name, operation in admin_routes["operations"].items():
    unknown = set(operation) - admin_operation_keys
    assert not unknown, f"unknown keys in operation {name}: {sorted(unknown)}"
    assert {"method", "path"} <= set(operation), f"operation {name} is missing method or path"
    assert all(isinstance(field, str) for field in operation.get("response_fields", ()))
    error_fields = operation.get("error_fields", [])
    assert all(isinstance(field, str) for field in error_fields), f"operation {name} has a non-string error field"
    # Semantics are only meaningful for a field the error body actually carries.
    semantics = operation.get("error_field_semantics", {})
    assert set(semantics) <= set(error_fields), f"operation {name} documents semantics for an undeclared error field"
    assert all(isinstance(text, str) for text in semantics.values())
