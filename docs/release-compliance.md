# Release compliance

No migration or validation command publishes a package, image, tag, release, or deployment. Publication requires an approved annotated version tag and the separately controlled hosted release workflow.

## Validation surfaces

`just check` verifies formatting, linting, promoted contracts, repository policy, secret scans, types, tests and coverage, deterministic web assets, wheel installation, legal metadata, dependency policy, release artifacts, and the version preview. `just image` builds and inspects the local OCI image. `just audit` performs the network-backed Python and JavaScript vulnerability audits.

`just e2e` instruments browser JavaScript with Istanbul, runs Chromium, Firefox, WebKit, iPhone 15, and iPad generation 11 projects, captures per-project raw coverage and LCOV, and always restores the original JavaScript. A restoration guard is active before the first source is changed, including when a later source is unavailable. Failed or crashed pages independently attempt coverage, screenshot, trace, and video finalization; one diagnostic failure cannot skip the remaining cleanup. The shared workflow retains both coverage and failure evidence even when validation fails.

## Automation

The thin CI and release callers pin `groovemap-music/automation` by a reviewed forty-character commit. CI runs for pushes to `main`, ordinary and Dependabot-authored pull requests, manual dispatches, and two weekly full/security schedules. There is one required job graph for every pull request; no actor-specific skip or reduced fallback exists.

Full validation requires read access to the pinned `python-libraries` revision. `GROOVEMAP_CI_APP_CLIENT_ID` and `GROOVEMAP_CI_APP_PRIVATE_KEY` supply that read-only checkout. `CODECOV_TOKEN` is mapped explicitly and uploads fail closed. Infrastructure must provide both secrets to Dependabot as well as ordinary Actions; until that external rollout is verified, the full dependency-update graph is expected to fail rather than silently weaken.

## Package and image evidence

The wheel carries `AGPL-3.0-only` and every legal file. The image carries the repository URL, exact source revision, version, creation time, and license annotation. The local release dry-run emits checksums, an SBOM, a complete locked runtime dependency notice inventory, and provenance containing the exact commit without uploading an artifact.

## Historical planning privacy

Historical implementation plans are preserved in the private `planning-archive` before removal from this public-intent repository. Deleting them from the current tree is not sufficient: before publication, a backed-up separate mirror clone must remove `.planning/**`, `docs/superpowers/plans/**`, and `docs/superpowers/specs/**` from every ref. The rehearsal retains an old-to-new commit map and runs complete reachable-object and secret scans.

The filtered clone is the only permissible rewrite target. Replacing the private remote from that clone and making the repository public are distinct operator-approved actions; neither is performed by repository validation.
