import { existsSync, mkdirSync, readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import coverage from "istanbul-lib-coverage";
import reportLibrary from "istanbul-lib-report";
import reports from "istanbul-reports";

import { restoreSources, root } from "./e2e-coverage-sources.mjs";

const { createCoverageMap } = coverage;
const { createContext } = reportLibrary;

const coverageRoot = resolve(root, "coverage/e2e");
const rawRoot = resolve(coverageRoot, "raw");
const projects = ["chromium", "firefox", "webkit", "iphone", "ipad"];

function jsonFiles(directory) {
  if (!existsSync(directory)) return [];
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    return entry.isDirectory() ? jsonFiles(path) : entry.name.endsWith(".json") ? [path] : [];
  });
}

function writeReports(files, directory) {
  const map = createCoverageMap({});
  for (const path of files) map.merge(JSON.parse(readFileSync(path, "utf8")));
  mkdirSync(directory, { recursive: true });
  const context = createContext({ coverageMap: map, dir: directory });
  reports.create("lcovonly", {}).execute(context);
  reports.create("text-summary", {}).execute(context);
}

let failure;
try {
  const allFiles = [];
  for (const project of projects) {
    const files = jsonFiles(resolve(rawRoot, project));
    if (files.length === 0) throw new Error(`Missing Istanbul coverage for ${project}`);
    writeReports(files, resolve(coverageRoot, project));
    allFiles.push(...files);
  }
  writeReports(allFiles, coverageRoot);
  console.log(`Merged ${allFiles.length} browser coverage fragments from ${projects.join(", ")}.`);
} catch (error) {
  failure = error;
} finally {
  restoreSources();
}

if (failure) throw failure;
