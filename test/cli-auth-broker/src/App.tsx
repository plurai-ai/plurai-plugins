import { useEffect, useMemo, useState } from "react";
import { useAuth, useClerk, useUser } from "@clerk/clerk-react";

const JWT_TEMPLATE = import.meta.env.VITE_JWT_TEMPLATE ?? "pluto-agent-authz";
const ALLOWED_REDIRECT_HOSTS = (import.meta.env.VITE_ALLOWED_REDIRECT_HOSTS ?? "127.0.0.1,localhost")
  .split(",").map((s: string) => s.trim());
const ALLOWED_REDIRECT_PORTS = (import.meta.env.VITE_ALLOWED_REDIRECT_PORTS ?? "8765,8766,8767,8768")
  .split(",").map((s: string) => Number(s.trim()));

type Validated =
  | { ok: true; redirectUrl: URL; state: string }
  | { ok: false; error: string };

function validateParams(search: string): Validated {
  const params = new URLSearchParams(search);
  const redirectUri = params.get("redirect_uri");
  const state = params.get("state");

  if (!redirectUri || !state) {
    return { ok: false, error: "Missing required query parameters: redirect_uri and state." };
  }
  let url: URL;
  try { url = new URL(redirectUri); }
  catch { return { ok: false, error: `Invalid redirect_uri: ${redirectUri}` }; }

  if (url.protocol !== "http:") {
    return { ok: false, error: "redirect_uri must use http:// (loopback only)." };
  }
  if (!ALLOWED_REDIRECT_HOSTS.includes(url.hostname)) {
    return {
      ok: false,
      error: `redirect_uri host not allowed: ${url.hostname}. Allowed: ${ALLOWED_REDIRECT_HOSTS.join(", ")}.`,
    };
  }
  const port = Number(url.port);
  if (!ALLOWED_REDIRECT_PORTS.includes(port)) {
    return {
      ok: false,
      error: `redirect_uri port not allowed: ${url.port}. Allowed: ${ALLOWED_REDIRECT_PORTS.join(", ")}.`,
    };
  }
  if (url.pathname !== "/callback") {
    return { ok: false, error: `redirect_uri path must be /callback (got: ${url.pathname}).` };
  }
  return { ok: true, redirectUrl: url, state };
}

function decodeJwtExp(jwt: string): number | null {
  try {
    const b64 = jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(b64));
    return typeof payload.exp === "number" ? payload.exp : null;
  } catch {
    return null;
  }
}

export function App() {
  const validation = useMemo(() => validateParams(window.location.search), []);
  const { isLoaded, isSignedIn } = useAuth();
  const { user } = useUser();
  const clerk = useClerk();
  const [status, setStatus] = useState("Loading…");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!validation.ok) {
      setError(validation.error);
      return;
    }
    if (!isLoaded) return;

    if (!isSignedIn) {
      setStatus("Redirecting to sign-in…");
      clerk.redirectToSignIn({ signInForceRedirectUrl: window.location.href });
      return;
    }

    let cancelled = false;
    setStatus(`Signed in as ${user?.primaryEmailAddress?.emailAddress ?? "unknown"}. Minting CLI token…`);

    (async () => {
      let token: string | null;
      try {
        token = await clerk.session!.getToken({ template: JWT_TEMPLATE });
      } catch (e) {
        if (!cancelled) setError(`getToken failed: ${(e as Error).message}`);
        return;
      }
      if (cancelled) return;
      if (!token) {
        setError(`getToken returned null — is the "${JWT_TEMPLATE}" JWT template configured on this Clerk instance?`);
        return;
      }

      const target = new URL(validation.redirectUrl.toString());
      target.searchParams.set("state", validation.state);
      target.searchParams.set("token", token);
      const exp = decodeJwtExp(token);
      if (exp != null) target.searchParams.set("expires_at", String(exp));

      setStatus("Logged in. You can close this tab.");
      setTimeout(() => window.location.replace(target.toString()), 300);
    })();

    return () => { cancelled = true; };
  }, [validation, isLoaded, isSignedIn, user, clerk]);

  return (
    <main style={{
      fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
      maxWidth: "30rem",
      margin: "4rem auto",
      padding: "0 1rem",
      lineHeight: 1.5,
      colorScheme: "light dark",
    }}>
      <h1 style={{ fontSize: "1.25rem", marginBottom: "0.25rem" }}>Pluto CLI sign-in</h1>
      <p style={{ color: "#666", marginTop: 0 }}>
        Authorising the <code>pluto-judge</code> CLI.
      </p>
      {!error && <p style={{ marginTop: "1rem" }}>{status}</p>}
      {error && (
        <p style={{ marginTop: "1rem", color: "#b00020", whiteSpace: "pre-wrap" }}>
          {error}
        </p>
      )}
    </main>
  );
}
