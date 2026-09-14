# Test assertion audit

The repository-local audit at `scripts/audit_test_assertions.py` inventories pytest-style test
functions that contain no recognized outcome assertion or rely exclusively on mock call/await
assertions. It recognizes Python `assert`, unittest-style `assert*` methods,
`pytest.raises`/`warns`/`deprecated_call`, and the standard `Mock`/`AsyncMock` call assertion
methods.

Run it from the repository root:

```console
uv run python scripts/audit_test_assertions.py --json
```

## Dashboard fixture-hardening audit

| Revision | Test functions | Assertion-free | Call-only |
| --- | ---: | ---: | ---: |
| Before (`e444ff4`) | 302 | 10 | 5 |
| After (`gm-operations-console-aho.1`) | 304 | 10 | 5 |

The two added fixture-contract tests account for the test-function increase. The shared
dashboard fixtures now model `AsyncResilientNeo4jDriver` → `AsyncSession` → `AsyncResult` and
`AsyncResilientPostgreSQL` → `AsyncConnection` → `AsyncCursor` with `spec_set` autospecs and
await-faithful context managers. Tightening those fixtures exposed no product failure, so there
is no latent product bead to file. The 15 pre-existing weak-assertion findings remain visible in
the JSON output and are outside this database-fixture bead.
