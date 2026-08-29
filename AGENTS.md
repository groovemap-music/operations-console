# Repository instructions

- Run `just check` before proposing a change; run `just e2e` for browser-visible changes.
- Treat files under `contracts/` and generated `dashboard/*_contract.py` modules as promoted producer contracts. Update provenance, bindings, and checks together.
- Canonical editable branding belongs to the public [`groovemap-music/design`](https://github.com/groovemap-music/design) repository. Only promote verified generated assets from the pinned design commit through `scripts/promote-brand.sh`, using `GROOVEMAP_DESIGN_REPO` when the checkout is not at `../design`.
- Never add a relative import or Docker build-context dependency on another GrooveMap repository.
- Do not commit credentials, local state, build output, Playwright recordings, or decrypted secret material.
- Releases are versioned with Commitizen and require an approved `v$version` tag. Migration work must not publish artifacts.
