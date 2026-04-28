// IMPORTANT — security invariants for this file:
//   - Never store the JWT in React state. It lives in a useRef and is cleared
//     immediately after the loopback POST.
//   - Never console.log the token or any prefix of it.
//   - Never put the token, the redirect_uri, the state, or any decoded payload
//     into JSX, the URL, or window.location. The handoff to the CLI is a
//     background `fetch` POST — the browser never navigates with the JWT, so
//     it never lands in history, the address bar, or a referrer header.
//   - Errors reported to the CLI use a closed enum (`mint_failed`,
//     `session_expired`). Never include `e.message` or stack traces.
//   - Errors that happen BEFORE redirect_uri is validated must NOT contact
//     the loopback — render a static page and rely on the CLI's timeout.
//
// See /Users/ben/workspace/pluto-judge/docs/rfcs/0001-web-broker-cli-auth.md.

import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth, useClerk, useUser } from "@clerk/clerk-react";
import { validateParams, type Validated } from "./validateParams";
import { decodeJwtExp } from "./decodeJwt";

const JWT_TEMPLATE = "pluto-agent-authz";

type Phase =
  | "validating"
  | "loading-clerk"
  | "signing-in"
  | "minting"
  | "posting"
  | "completed"
  | "already-completed"
  | "error";

type ErrorCode = "mint_failed" | "session_expired";
const ERROR_DESCRIPTIONS: Record<ErrorCode, string> = {
  mint_failed: "The CLI sign-in failed.",
  session_expired: "The browser sign-in expired before the CLI token could be issued.",
};

const STATUS_LOADING = "Loading…";
const STATUS_SIGNING_IN = "Redirecting to sign-in…";
const STATUS_AUTHORISING = "Authorising…";
const STATUS_POSTING = "Handing off to the CLI…";
const STATUS_DONE = "Logged in. You can close this tab.";
const STATUS_ALREADY = "This sign-in already completed. Close this tab and re-run pluto auth login.";
const STATUS_ERROR = "Sign-in failed. Close this tab and try pluto auth login again.";

function completedKey(state: string): string {
  return `pluto-broker-completed:${state}`;
}

// Send the handoff payload to the CLI loopback. We POST instead of redirecting
// so the JWT never leaves the request body — no browser history entry, no
// referrer, no address-bar exposure. Returns true on a 2xx response from the
// loopback. CORS is enforced by the CLI's `Access-Control-Allow-Origin`
// allowlist, and the CLI cross-checks `state` defensively.
async function postHandoff(redirectUrl: URL, body: Record<string, unknown>): Promise<boolean> {
  try {
    const res = await fetch(redirectUrl.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "omit",
      cache: "no-store",
      mode: "cors",
      redirect: "error",
      referrerPolicy: "no-referrer",
    });
    return res.ok;
  } catch {
    return false;
  }
}

export function App() {
  const validation = useMemo<Validated>(() => validateParams(window.location.search), []);
  const { isLoaded, isSignedIn } = useAuth();
  const { user } = useUser();
  const clerk = useClerk();
  const [phase, setPhase] = useState<Phase>("validating");
  const tokenRef = useRef<string | null>(null);
  const startedRef = useRef(false);

  useEffect(() => {
    if (!validation.ok) {
      setPhase("error");
      return;
    }

    if (sessionStorage.getItem(completedKey(validation.state))) {
      setPhase("already-completed");
      return;
    }

    if (!isLoaded) {
      setPhase("loading-clerk");
      return;
    }

    if (!isSignedIn) {
      setPhase("signing-in");
      clerk.redirectToSignIn({ signInForceRedirectUrl: window.location.href });
      return;
    }

    if (startedRef.current) return;
    startedRef.current = true;

    // Best-effort error notification to the CLI loopback. Same channel as
    // the success path (POST, no redirect) so the error never appears in the
    // URL. Fire-and-forget — if the loopback is gone, the CLI's 5-minute
    // timeout recovers. Always sets the phase to "error" synchronously.
    const reportError = (code: ErrorCode): void => {
      setPhase("error");
      void postHandoff(validation.redirectUrl, {
        state: validation.state,
        error: code,
        error_description: ERROR_DESCRIPTIONS[code],
      });
    };

    const session = clerk.session;
    if (!session) {
      reportError("session_expired");
      return;
    }

    let cancelled = false;
    setPhase("minting");

    void (async () => {
      let token: string | null = null;
      try {
        token = await session.getToken({ template: JWT_TEMPLATE });
      } catch {
        if (!cancelled) reportError("mint_failed");
        return;
      }
      if (cancelled) return;
      if (!token) {
        reportError("mint_failed");
        return;
      }

      const exp = decodeJwtExp(token);
      if (exp == null) {
        reportError("mint_failed");
        return;
      }

      tokenRef.current = token;
      setPhase("posting");
      const ok = await postHandoff(validation.redirectUrl, {
        state: validation.state,
        token,
        expires_at: exp,
      });
      tokenRef.current = null;
      if (cancelled) return;
      if (!ok) {
        setPhase("error");
        return;
      }
      sessionStorage.setItem(completedKey(validation.state), "1");
      setPhase("completed");
    })();

    return () => {
      cancelled = true;
      tokenRef.current = null;
    };
  }, [validation, isLoaded, isSignedIn, clerk]);

  return (
    <main
      style={{
        fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif',
        maxWidth: "30rem",
        margin: "4rem auto",
        padding: "0 1rem",
        lineHeight: 1.5,
        colorScheme: "light dark",
      }}
    >
      <h1 style={{ fontSize: "1.25rem", marginBottom: "0.25rem" }}>Pluto CLI sign-in</h1>
      <p style={{ color: "#666", marginTop: 0 }}>
        Authorising the <code>pluto-judge</code> CLI.
      </p>
      <Status
        phase={phase}
        validationOk={validation.ok}
        validationError={validation.ok ? null : validation.error}
        email={user?.primaryEmailAddress?.emailAddress}
      />
    </main>
  );
}

function Status({
  phase,
  validationOk,
  validationError,
  email,
}: {
  phase: Phase;
  validationOk: boolean;
  validationError: string | null;
  email: string | undefined;
}) {
  if (!validationOk) {
    return (
      <p style={{ marginTop: "1rem", color: "#b00020", whiteSpace: "pre-wrap" }}>
        {validationError ?? STATUS_ERROR}
      </p>
    );
  }
  if (phase === "error") {
    return (
      <p style={{ marginTop: "1rem", color: "#b00020" }}>{STATUS_ERROR}</p>
    );
  }
  if (phase === "already-completed") {
    return <p style={{ marginTop: "1rem" }}>{STATUS_ALREADY}</p>;
  }
  if (phase === "completed") {
    return <p style={{ marginTop: "1rem" }}>{STATUS_DONE}</p>;
  }
  if (phase === "posting") {
    return <p style={{ marginTop: "1rem" }}>{STATUS_POSTING}</p>;
  }
  if (phase === "minting") {
    return (
      <p style={{ marginTop: "1rem" }}>
        {email ? `Signed in as ${email}. ${STATUS_AUTHORISING}` : STATUS_AUTHORISING}
      </p>
    );
  }
  if (phase === "signing-in") {
    return <p style={{ marginTop: "1rem" }}>{STATUS_SIGNING_IN}</p>;
  }
  return <p style={{ marginTop: "1rem" }}>{STATUS_LOADING}</p>;
}

