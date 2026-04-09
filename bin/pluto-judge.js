#!/usr/bin/env node

const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

const serverPy = path.join(__dirname, "..", "server.py");

// Find python3 — VS Code has minimal PATH so check common locations
const candidates = [
  "/opt/homebrew/bin/python3",   // macOS Homebrew ARM
  "/usr/local/bin/python3",      // macOS Homebrew Intel / Linux
  "/usr/bin/python3",            // System Python
  "python3",                     // PATH fallback
];

let python = null;
for (const p of candidates) {
  try {
    if (p.startsWith("/")) {
      if (fs.existsSync(p)) { python = p; break; }
    } else {
      // Check PATH
      const { execSync } = require("child_process");
      execSync(`which ${p}`, { stdio: "ignore" });
      python = p;
      break;
    }
  } catch {}
}

if (!python) {
  process.stderr.write("Error: python3 not found. Install Python 3.10+.\n");
  process.exit(1);
}

// Spawn python3 server.py and pipe stdin/stdout through
const child = spawn(python, [serverPy], {
  stdio: ["pipe", "pipe", "inherit"],
  env: { ...process.env },
});

process.stdin.pipe(child.stdin);
child.stdout.pipe(process.stdout);

child.on("exit", (code) => process.exit(code || 0));
process.on("SIGTERM", () => child.kill("SIGTERM"));
process.on("SIGINT", () => child.kill("SIGINT"));
