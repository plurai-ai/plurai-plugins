import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { ClerkProvider } from "@clerk/clerk-react";
import { App } from "./App";

// 1. Top-frame guard. Belt-and-braces with `frame-ancestors 'none'` from the
//    server-side CSP — see hardening plan §C2. Cross-origin top-window access
//    can throw, so treat any throw as "framed" and refuse to render.
try {
  if (window.top !== window.self) {
    document.documentElement.replaceChildren();
    throw new Error("framed");
  }
} catch (err) {
  document.documentElement.replaceChildren();
  throw err;
}

// 2. HTTPS guard for production. Loopback exemption is on the redirect target,
//    not the broker page itself.
if (import.meta.env.PROD && window.location.protocol !== "https:") {
  document.body.textContent = "Insecure context. Refusing to run.";
  throw new Error("insecure context");
}

// 3. Suppress global error/unhandledrejection bubbling. If the parent webapp
//    ever wires Sentry/Datadog/RUM, this stops them from capturing
//    window.location while the token is momentarily on the URL.
window.addEventListener("error", (e) => e.preventDefault(), { capture: true });
window.addEventListener("unhandledrejection", (e) => e.preventDefault());

const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;

if (!PUBLISHABLE_KEY) {
  throw new Error(
    "VITE_CLERK_PUBLISHABLE_KEY is required. Set it in .env.local — see .env.example.",
  );
}

class BrokerErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }
  componentDidCatch(_err: Error, _info: ErrorInfo): void {
    // Deliberately swallow — see security invariants in App.tsx.
  }
  render(): ReactNode {
    if (this.state.failed) {
      return (
        <p style={{ fontFamily: "system-ui, sans-serif", margin: "4rem auto", maxWidth: "30rem" }}>
          Sign-in failed. Close this tab and try <code>pluto auth login</code> again.
        </p>
      );
    }
    return this.props.children;
  }
}

const here = window.location.pathname + window.location.search;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrokerErrorBoundary>
      <ClerkProvider
        publishableKey={PUBLISHABLE_KEY}
        signInForceRedirectUrl={here}
        signUpForceRedirectUrl={here}
        signInFallbackRedirectUrl={here}
        signUpFallbackRedirectUrl={here}
      >
        <App />
      </ClerkProvider>
    </BrokerErrorBoundary>
  </StrictMode>,
);
