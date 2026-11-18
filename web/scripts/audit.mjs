// CI's web audit: `npm audit` at high severity, except advisories listed in
// audit-allow.json whose `until` date has not passed. Any other high or critical
// advisory, or an expired allowance, fails. No dependencies beyond Node itself.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const LEVELS = new Set(["high", "critical"]);
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const today = new Date().toISOString().slice(0, 10);

const allowList = JSON.parse(readFileSync(join(root, "audit-allow.json"), "utf8")).allow;
const allowed = new Map(allowList.map((a) => [a.id, a]));

let raw;
try {
  raw = execFileSync("npm", ["audit", "--json"], { cwd: root, encoding: "utf8" });
} catch (err) {
  raw = err.stdout; // npm audit exits non-zero when it finds anything
}
const report = JSON.parse(raw);

const found = new Map(); // advisory id -> { title, severity, packages }
for (const [name, vuln] of Object.entries(report.vulnerabilities ?? {})) {
  for (const via of vuln.via) {
    if (typeof via !== "object" || !LEVELS.has(via.severity)) continue;
    const id = via.url.split("/").pop();
    const entry = found.get(id) ?? { title: via.title, severity: via.severity, packages: new Set() };
    entry.packages.add(name);
    found.set(id, entry);
  }
}

let failed = false;
for (const [id, a] of found) {
  const allow = allowed.get(id);
  const pkgs = [...a.packages].join(", ");
  if (allow && allow.until >= today) {
    console.log(`allowed until ${allow.until}: ${id} (${a.severity}) ${a.title} [${pkgs}]`);
  } else if (allow) {
    console.log(`EXPIRED ${allow.until}: ${id} (${a.severity}) ${a.title} [${pkgs}]`);
    failed = true;
  } else {
    console.log(`FAIL: ${id} (${a.severity}) ${a.title} [${pkgs}]`);
    failed = true;
  }
}
for (const id of allowed.keys()) {
  if (!found.has(id)) console.log(`no longer reported, remove from audit-allow.json: ${id}`);
}
console.log(failed ? "audit failed" : `audit ok (${found.size} high or critical, all allowed)`);
process.exit(failed ? 1 : 0);
