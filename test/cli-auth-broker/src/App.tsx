// IMPORTANT — security invariants for this file:
//   - Never store the JWT in React state. It lives in a useRef and is cleared
//     immediately after the redirect navigation.
//   - Never console.log the token or any prefix of it.
//   - Never render the token, the redirect_uri, the state, or any decoded
//     payload into JSX. Status text is from a fixed set; user email is
//     allowed because the user is signing themselves in.
//   - Errors reported to the CLI use a closed enum (`mint_failed`,
//     `session_expired`). Never include `e.message` or stack traces.
//   - Errors that happen BEFORE redirect_uri is validated must NOT redirect
//     anywhere — render a static page and rely on the CLI's timeout.
//
// See /Users/ben/workspace/pluto-judge/docs/rfcs/0001-web-broker-cli-auth.md
// and the hardening plan in /Users/ben/.claude/plans/.

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
  | "redirecting"
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
const STATUS_DONE = "Logged in. You can close this tab.";
const STATUS_ALREADY = "This sign-in already completed. Close this tab and re-run pluto auth login.";
const STATUS_ERROR = "Sign-in failed. Close this tab and try pluto auth login again.";

function completedKey(state: string): string {
  return `pluto-broker-completed:${state}`;
}

function buildErrorRedirect(redirectUrl: URL, state: string, code: ErrorCode): string {
  const target = new URL(redirectUrl.toString());
  target.searchParams.set("state", state);
  target.searchParams.set("error", code);
  target.searchParams.set("error_description", ERROR_DESCRIPTIONS[code]);
  return target.toString();
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

    const session = clerk.session;
    if (!session) {
      reportError(validation.redirectUrl, validation.state, "session_expired");
      return;
    }

    let cancelled = false;
    setPhase("minting");

    void (async () => {
      let token: string | null = null;
      try {
        token = await session.getToken({ template: JWT_TEMPLATE });
      } catch {
        if (!cancelled) reportError(validation.redirectUrl, validation.state, "mint_failed");
        return;
      }
      if (cancelled) return;
      if (!token) {
        reportError(validation.redirectUrl, validation.state, "mint_failed");
        return;
      }

      const exp = decodeJwtExp(token);
      if (exp == null) {
        reportError(validation.redirectUrl, validation.state, "mint_failed");
        return;
      }

      tokenRef.current = token;
      const target = new URL(validation.redirectUrl.toString());
      target.searchParams.set("state", validation.state);
      target.searchParams.set("token", token);
      target.searchParams.set("expires_at", String(exp));

      sessionStorage.setItem(completedKey(validation.state), "1");
      setPhase("redirecting");
      const url = target.toString();
      tokenRef.current = null;
      window.location.replace(url);
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
  if (phase === "redirecting") {
    return <p style={{ marginTop: "1rem" }}>{STATUS_DONE}</p>;
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

function reportError(redirectUrl: URL, state: string, code: ErrorCode): void {
  window.location.replace(buildErrorRedirect(redirectUrl, state, code));
}
