# Decision gm-operations-console-8eg: Overview pipeline visualization

- Bead: `gm-operations-console-8eg` (epic `gm-operations-console-t34`)
- Spike: [`gm-operations-console-2rf-pipeline-visualization.md`](gm-operations-console-2rf-pipeline-visualization.md)
- Date: 2026-10-24. The user confirmed the replan below on 2026-10-24.

## Verdict

| Candidate | Verdict |
|---|---|
| Entity x consumer matrix | **GO, primary Overview view** |
| Run progress | **GO, secondary view**, with a per-pipeline ETA headline |
| Left-to-right flow | **NO-GO as the Overview.** Retained only as an optional, desktop-only topology drill-down |

The verdict differs from the filed beads (flow was planned as a peer view), so the plan was
replanned rather than recorded unchanged.

## Evidence that decided it

All evidence is from the spike's synthetic fixtures and fixation model, not operator trials.

- The matrix is the only view with every fault on the first screen at 1440 px and 390 px. Its
  desktop page height stays at 900 px with twice the consumers, and it raised no false alarms.
- The flow needs horizontal panning at 390 px (fault 2+ screens away). It is 1.6x taller than the
  matrix at 1x and 2.9x at 2x consumers, and its fan-in becomes unreadable at 2x.
- Run progress is the only view that answers "when will it finish, and what limits it", including
  "Cannot finish: tableinator · releases stalled".
- The live model reports "rate unknown" as 0.0, so a stalled rule would fire falsely after a
  console restart or broker counter reset. This must be fixed before any view ships.

## Plan amendments

- `gm-operations-console-cn6`: matrix promoted to the Overview's primary view (P1). Depends on 1g0j.
- `gm-operations-console-06s`: amended to run progress as a secondary view with a per-pipeline ETA
  headline (P2). Expected totals come from `groovemap_extraction_file_progress_ratio{source,entity}`,
  with per-run acked baselines. Depends on `fsh` and `cn6`.
- `gm-operations-console-9nd`: re-scoped to an optional desktop-only topology drill-down (P4).
  Depends on `cn6`.
- `gm-operations-console-1g0j` (new): the live model returns null for uncomputable rates instead of
  0.0. `cn6` and `krq` depend on it.
- `gm-design-d0z3` (design hive): amends ADR 0018 "Pipeline visualization" to "matrix primary; run
  progress secondary; flow not adopted", citing the spike and its synthetic-data caveat.

A short check with a real operator on a real run is still worth doing before the Overview is
considered final.
