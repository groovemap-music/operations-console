# Release compliance

No migration or validation command publishes a package, image, tag, release, or deployment. Publication requires an approved annotated version tag and the separately controlled hosted release workflow.

## Validation surfaces

`just check` is the authoritative local gate. It composes `source-check`, `secret-scan`,
Python type checking and coverage, `js-test`, the deterministic web and Python builds, wheel
installation, legal and dependency policy, release artifacts, and the non-mutating version
preview. `source-check` is limited to formatting, linting, promoted contracts, brand assets,
and repository policy. `secret-scan` is the narrow Git-history and working-tree scan used by
the hosted secret-scanning job. The internal `web-dependencies` prerequisite installs locked Node
dependencies once when JavaScript tests and the web build run in the same invocation.

`just coverage` emits both `coverage.xml` and `coverage/javascript/lcov.info`. `just image`
builds and inspects the local OCI image. `just audit` performs the network-backed Python and
JavaScript vulnerability audits.

`just e2e` instruments browser JavaScript with Istanbul, runs Chromium, Firefox, WebKit, iPhone 15, and iPad generation 11 projects, captures per-project raw coverage and LCOV, and always restores the original JavaScript. A restoration guard is active before the first source is changed, including when a later source is unavailable. Failed or crashed pages independently attempt coverage, screenshot, trace, and video finalization; one diagnostic failure cannot skip the remaining cleanup. The shared workflow retains both coverage and failure evidence even when validation fails.

## Automation

The thin CI and release callers pin `groovemap-music/automation` at
`833cb464507678c38ab78bd4718ce697399463e9`. CI runs for pushes to `main`, ordinary and
Dependabot-authored pull requests, manual dispatches, and two weekly full/security schedules.
There is one required job graph for every pull request; no actor-specific skip or reduced
fallback exists. The hosted E2E lifecycle remains split into `e2e-setup`, `e2e-instrument`,
`e2e-run`, and `e2e-post` so setup, browser-specific evidence, and source restoration remain
visible to the reusable workflow.

**Public-library cutover: complete.** Full validation resolves `python-libraries` from its public
repository at the immutable revision recorded in `pyproject.toml`; no first-party repository
credential is required. `CODECOV_TOKEN` remains explicitly mapped and uploads fail closed, while
the release caller passes no inherited secrets. Dependabot-authored pull requests use the same
complete required graph as every other pull request.

The release caller retains the supported `prepare-image-command: just prepare-runtime-wheel`
interface so the pinned public runtime wheel is staged in the local image context. For local
builds, `GROOVEMAP_RUNTIME_REPO` remains an optional override for an explicit clean checkout at
that revision; without it, the preparation script uses a matching adjacent checkout or creates a
temporary checkout from the public source.

## Package and image evidence

The wheel carries `AGPL-3.0-only` and every legal file. The image carries the repository URL, exact source revision, version, creation time, and license annotation. The local release dry-run emits checksums, an SBOM, a complete locked runtime dependency notice inventory, and provenance containing the exact commit without uploading an artifact.

## Historical planning privacy

Historical implementation plans are preserved in the private `planning-archive` before removal from this public-intent repository. Deleting them from the current tree is not sufficient: before publication, a backed-up separate mirror clone must remove `.planning/**`, `docs/superpowers/plans/**`, and `docs/superpowers/specs/**` from every ref. The rehearsal retains an old-to-new commit map and runs complete reachable-object and secret scans.

The filtered clone is the only permissible rewrite target. Replacing the private remote from that clone and making the repository public are distinct operator-approved actions; neither is performed by repository validation.
