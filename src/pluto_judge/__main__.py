"""Entry point for `python -m pluto_judge` and the `pluto-judge` console script.

Default mode runs the FastMCP stdio server. With a leading `auth` argument,
forwards the rest to the auth subcommand CLI — e.g.
``python -m pluto_judge auth login --key ak_…`` / ``status`` / ``logout``.
"""

from __future__ import annotations

import sys

from .auth.auth import main as auth_main
from .server import mcp


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        return auth_main(sys.argv[2:])
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
