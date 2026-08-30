import { cpSync, existsSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export const root = resolve(process.env.GROOVEMAP_E2E_ROOT ?? repositoryRoot);
export const backupRoot = resolve(root, ".build/e2e-original");
export const sources = ["dashboard/static/admin.js", "dashboard/static/dashboard.js"];

export function restoreSources() {
  for (const relativePath of sources) {
    const backup = resolve(backupRoot, relativePath);
    if (existsSync(backup)) cpSync(backup, resolve(root, relativePath));
  }
  if (existsSync(backupRoot)) rmSync(backupRoot, { recursive: true, force: true });
}
