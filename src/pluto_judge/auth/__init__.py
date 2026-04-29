# pyright: reportUnknownVariableType=false, reportMissingTypeStubs=false
"""Auth dispatcher for pluto-judge.

Selects the auth backend at import time based on ``PLUTO_AUTH_METHOD``
(default: ``chrome``) and re-exports its public API (``get_token``,
``force_login``, ``pluto_headers``, ``agent_headers``, ``main``).

- ``chrome`` — reads the user's Pluto session out of the local Chrome
  cookie store. See ``pluto_judge.auth.chrome``.
- ``broker`` — web-broker JWT flow (RFC 0001). See
  ``pluto_judge.auth.broker``.
"""

import os
import sys

__all__ = ["agent_headers", "force_login", "get_token", "main", "pluto_headers"]

_METHOD = os.environ.get("PLUTO_AUTH_METHOD", "chrome").lower()

if _METHOD == "broker":
    from .broker import (
        agent_headers,
        force_login,
        get_token,
        main,
        pluto_headers,
    )
elif _METHOD == "chrome":
    from .chrome import (
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
