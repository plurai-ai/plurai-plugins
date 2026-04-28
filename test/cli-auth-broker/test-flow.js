// End-to-end test of the broker flow. Stands in for the CLI: starts a loopback
// server on 8765, opens the broker page in the user's browser, and prints the
// captured token + expiry once the broker POSTs to /callback.
//
// Mirrors the production CLI handler in src/auth.py — same CORS preflight
// logic, same JSON body, same `state` check — so a green run here is a strong
// signal that the real Python CLI will accept the same broker build.
//
// Run after `npm run dev` is up in another terminal:
//   node test-flow.js

import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import { exec } from "node:child_process";
import { URL } from "node:url";

const BROKER_URL = process.env.BROKER_URL ?? "http://127.0.0.1:5173/cli-auth";
const LOOPBACK_PORT = Number(process.env.LOOPBACK_PORT ?? 8765);
const TIMEOUT_MS = 5 * 60 * 1000;

const brokerOrigin = new URL(BROKER_URL).origin;
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
      res.writeHead(404).end();
      return;
    }

    const origin = req.headers.origin ?? "";
    if (origin !== brokerOrigin) {
      res.writeHead(403).end();
      return;
    }

    if (req.method === "OPTIONS") {
      res.writeHead(204, {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "60",
        Vary: "Origin",
      }).end();
      return;
    }

    if (req.method !== "POST") {
      res.writeHead(405, { Allow: "POST, OPTIONS" }).end();
      return;
    }

    const corsHeaders = {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": origin,
      Vary: "Origin",
    };
    const chunks = [];
    let total = 0;
    req.on("data", (c) => {
      total += c.length;
      if (total > 8 * 1024) {
        res.writeHead(413, corsHeaders).end(JSON.stringify({ error: "invalid_body" }));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      let payload;
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      } catch {
        res.writeHead(400, corsHeaders).end(JSON.stringify({ error: "invalid_body" }));
        return;
      }
      if (!payload || typeof payload !== "object") {
        res.writeHead(400, corsHeaders).end(JSON.stringify({ error: "invalid_body" }));
        return;
      }
      if (payload.state !== state) {
        res.writeHead(400, corsHeaders).end(JSON.stringify({ error: "state_mismatch" }));
        clearTimeout(timer);
        server.close();
        reject(new Error("state mismatch"));
        return;
      }
      if (payload.error) {
        res.writeHead(200, corsHeaders).end(JSON.stringify({ ok: true }));
        clearTimeout(timer);
        server.close();
        reject(new Error(`broker reported error: ${payload.error}`));
        return;
      }
      if (typeof payload.token !== "string" || !payload.token) {
        res.writeHead(400, corsHeaders).end(JSON.stringify({ error: "no_token" }));
        return;
      }
      res.writeHead(200, corsHeaders).end(JSON.stringify({ ok: true }));
      clearTimeout(timer);
      server.close();
      resolve(payload);
    });
  });

  server.listen(LOOPBACK_PORT, "127.0.0.1", () => {
    console.log(`Loopback listening on ${redirectUri}`);
    console.log(`Allowed broker origin: ${brokerOrigin}`);
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
