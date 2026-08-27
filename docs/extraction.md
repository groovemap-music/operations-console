# Extraction provenance

This repository was extracted without modifying the source monorepo. Its `main` history contains 194 commits relevant to the operations console before the standalone-establishment commit.

Source: `SimplicityGuy/discogsography`, bead branch `wt/bead/issue/discogsography-2kpm.20`.

The reproducible extraction used an isolated clone and `git-filter-repo` with `dashboard/`, `tests/dashboard/`, the PolyForm license, `docs/admin-guide.md`, and the dashboard-specific design plans/specifications. Tests were promoted with `--path-rename tests/dashboard/:tests/`.

The platform-wide `docs/monitoring.md` was deliberately excluded for ownership by `deployment`; excluding it also prevents historical example credentials from becoming findings in this repository's retained history. No source tags were copied because the monorepo tags do not unambiguously version this console.
