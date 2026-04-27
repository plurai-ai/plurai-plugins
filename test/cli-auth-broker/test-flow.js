// End-to-end test of the broker flow. Stands in for the CLI: starts a loopback
// server on 8765, opens the broker page in the user's browser, and prints the
// captured token + expiry once the redirect arrives.
//
// Run after `npm start` is up in another terminal:
//   node test-flow.js

import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import { exec } from "node:child_process";
import { URL } from "node:url";

const BROKER_URL = process.env.BROKER_URL ?? "http://127.0.0.1:5173/cli-auth";
const LOOPBACK_PORT = Number(process.env.LOOPBACK_PORT ?? 8765);
const TIMEOUT_MS = 5 * 60 * 1000;

const state = randomBytes(16).toString("base64url");
const redirectUri = `http://127.0.0.1:${LOOPBACK_PORT}/callback`;
const authUrl = `${BROKER_URL}?redirect_uri=${encodeURIComponent(redirectUri)}&state=${state}`;

const captured = await new Promise((resolve, reject) => {
  const timer = setTimeout(() => {
    server.close();
    reject(new Error(`Timed out after ${TIMEOUT_MS / 1000}s waiting for callback.`));
  }, TIMEOUT_MS);

  const server = createServer((req, res) => {
    const u = new URL(req.url, `http://127.0.0.1:${LOOPBACK_PORT}`);
    if (u.pathname !== "/callback") {
      res.writeHead(404).end(); return;
    }
    const params = Object.fromEntries(u.searchParams.entries());
    if (params.state !== state) {
      res.writeHead(400, { "Content-Type": "text/html" }).end("<h1>State mismatch.</h1>");
      clearTimeout(timer);
      server.close();
      reject(new Error("state mismatch"));
      return;
    }
    res.writeHead(200, { "Content-Type": "text/html" })
       .end("<h1>Logged in. You can close this tab.</h1>");
    clearTimeout(timer);
    server.close();
    resolve(params);
  });

  server.listen(LOOPBACK_PORT, "127.0.0.1", () => {
    console.log(`Loopback listening on ${redirectUri}`);
    console.log(`Opening: ${authUrl}\n`);
    openBrowser(authUrl);
  });
  server.on("error", reject);
});

const decoded = decodeJwtPayload(captured.token);
console.log("✓ Captured callback");
console.log(`  state:      ${captured.state}`);
console.log(`  expires_at: ${captured.expires_at} (${new Date((+captured.expires_at) * 1000).toISOString()})`);
console.log(`  token (first 32):  ${captured.token.slice(0, 32)}…`);
console.log(`  decoded payload:`);
console.log(JSON.stringify(decoded, null, 2));

function decodeJwtPayload(jwt) {
  const b64 = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(Buffer.from(b64, "base64").toString("utf8"));
}

function openBrowser(url) {
  const cmd = process.platform === "darwin" ? "open"
    : process.platform === "win32" ? "start \"\""
    : "xdg-open";
  exec(`${cmd} "${url}"`, (err) => {
    if (err) console.warn(`Couldn't auto-open browser. Paste this URL: ${url}`);
  });
}
