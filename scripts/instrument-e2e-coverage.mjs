import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import instrument from "istanbul-lib-instrument";

import { backupRoot, restoreSources, root, sources } from "./e2e-coverage-sources.mjs";

const { createInstrumenter } = instrument;

mkdirSync(backupRoot, { recursive: true });
try {
  for (const relativePath of sources) {
    const source = resolve(root, relativePath);
    const backup = resolve(backupRoot, relativePath);
    if (existsSync(backup)) continue;
    mkdirSync(dirname(backup), { recursive: true });
    cpSync(source, backup);
    const instrumenter = createInstrumenter({
      compact: false,
      esModules: false,
      produceSourceMap: false,
    });
    writeFileSync(source, instrumenter.instrumentSync(readFileSync(source, "utf8"), relativePath));
  }
} catch (error) {
  restoreSources();
  throw error;
}

console.log("Instrumented browser JavaScript; originals are isolated under .build/e2e-original.");
