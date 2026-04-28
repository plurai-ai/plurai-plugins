"""Auth dispatcher for pluto-judge.

Selects the auth backend at import time based on `PLUTO_AUTH_METHOD`:

- `chrome` (default) — reads the user's Pluto session out of the local
  Chrome cookie store. See `auth_chrome.py`. Used while the production
  broker page (RFC 0001) is still being wired up.
- `broker` — web-broker JWT flow, RFC 0001. See `auth_broker.py`. Will
  become the default once the broker page is live.

Re-exports the public API (`get_token`, `force_login`, `pluto_headers`,
`agent_headers`, `main`) from the chosen backend so `server.py` can keep
its existing `import auth` / `from auth import ...` bindings.
"""

import os
import sys

__all__ = ["agent_headers", "force_login", "get_token", "main", "pluto_headers"]

_METHOD = os.environ.get("PLUTO_AUTH_METHOD", "chrome").lower()

if _METHOD == "broker":
    from auth_broker import (
        agent_headers,
        force_login,
        get_token,
        main,
        pluto_headers,
    )
elif _METHOD == "chrome":
    from auth_chrome import (
        agent_headers,
        force_login,
        get_token,
        main,
        pluto_headers,
    )
else:
    raise RuntimeError(
        f"Unknown PLUTO_AUTH_METHOD={_METHOD!r}. Use 'broker' or 'chrome'."
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
