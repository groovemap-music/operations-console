# Third-party dependency notices

The GrooveMap operations console depends on third-party software. Upstream
license terms control; this file records the distribution boundary and the
rights that must accompany releases. `uv.lock` and `package-lock.json` are the
authoritative version locks. `scripts/release-dry-run.sh` also writes a complete
machine-readable Python inventory from an isolated installation of the runtime
wheel.

The operations-console wheel declares Python dependencies but does not bundle
their code. The OCI image installs the hashed runtime requirements and the two
first-party wheels, retaining the license files supplied in each installed
package's `.dist-info` directory. The optional alternative licensing described
in `COMMERCIAL-LICENSING.md` applies only to first-party rights held by the
participating copyright holders; it does not change any third-party license.

## Reciprocal-license Python dependencies

- `certifi` 2026.7.22 is licensed under `MPL-2.0` and is included through HTTP
  clients. Source: <https://github.com/certifi/python-certifi>.
- `orjson` 3.12.0 declares `MPL-2.0 AND (Apache-2.0 OR MIT)` and is a direct
  runtime dependency. Source: <https://github.com/ijl/orjson>.
- `psycopg` 3.3.4 and `psycopg-binary` 3.3.4 are licensed under
  `LGPL-3.0-only` and provide PostgreSQL access. Source:
  <https://github.com/psycopg/psycopg>.

The locked development environment also includes `chardet` 5.2.0 under
`LGPL-2.1-or-later`, plus `fqdn` 1.5.1 and `pathspec` 1.1.1 under `MPL-2.0`.
They are tools or transitive development dependencies and are not installed in
the runtime image.

For MPL-covered files, preserve license and copyright notices and make the
source form of distributed modifications to those files available under
`MPL-2.0`. For LGPL-covered libraries, preserve their notices and license
texts, provide the applicable covered source, and do not prevent replacement
or reverse engineering for debugging modifications. Reassess these obligations
before modifying, statically combining, vendoring, or changing how a covered
dependency is distributed.

## JavaScript build and test dependencies

The direct, build-only JavaScript dependencies are `@tailwindcss/cli` 4.3.3,
`@tailwindcss/forms` 0.5.11, `@vitest/coverage-v8` 4.1.11, `jsdom` 30.0.1,
and `vitest` 4.1.11. Each declares the `MIT` license. Their exact transitive
dependency versions, source URLs, integrity hashes, and SPDX license identifiers
are recorded in `package-lock.json`.

The locked Tailwind/Vite graph includes `lightningcss` 1.32.0 and 1.33.0 plus
their platform-specific optional packages under `MPL-2.0`. Those tools compile
CSS during the build and are not shipped in the runtime wheel or image. Preserve
the MPL notices and provide source for any distributed modifications if that
distribution boundary changes.

The remaining locked dependencies primarily use permissive `MIT`, BSD, ISC,
Python, or `Apache-2.0` terms. Preserve their copyright, attribution, license,
and upstream NOTICE material as required.

## First-party names and marks

The GrooveMap name, logos, and other first-party brand assets are governed by
the public `groovemap-music/design` repository's trademark-use policy. The
software's `AGPL-3.0-only` license does not grant trademark rights.
