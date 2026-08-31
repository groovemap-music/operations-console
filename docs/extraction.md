# Extraction provenance

This repository was extracted without modifying its source monorepository. The standalone history retains the commits relevant to the operations console, while unrelated application and deployment paths remain with their owning repositories.

The reproducible extraction used an isolated clone and `git-filter-repo` with the console application, tests, license, administrator guide, and relevant historical design material. Dashboard tests were promoted to the repository-level `tests/` tree. Source tags were not copied because monorepository tags did not unambiguously version this service.

Historical implementation plans are now preserved by the private `planning-archive` repository. They are removed from the current public-intent tree. Before publication, `scripts/rehearse-history-sanitization.sh` proves in a separate mirror clone that the private planning paths can be removed from every ref, records the old-to-new commit map, and passes complete reachable-object and secret scans. The script cannot change a remote; a real private-remote cutover and a later visibility change each require separate explicit approval.
