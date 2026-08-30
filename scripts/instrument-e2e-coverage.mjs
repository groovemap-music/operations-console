import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import instrument from "istanbul-lib-instrument";

const { createInstrumenter } = instrument;

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const backupRoot = resolve(root, ".build/e2e-original");
const sources = ["dashboard/static/admin.js", "dashboard/static/dashboard.js"];

mkdirSync(backupRoot, { recursive: true });
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

console.log("Instrumented browser JavaScript; originals are isolated under .build/e2e-original.");
