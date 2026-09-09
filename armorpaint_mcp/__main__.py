"""Module entry point, so ``python -m armorpaint_mcp`` works.

This is the invocation every MCP client config in the README and INSTALL.md uses.
The console script ``armorpaint-mcp`` (see ``pyproject.toml``) and
``python -m armorpaint_mcp.server`` both reach the same ``run()``.
"""

from .server import run

if __name__ == "__main__":
    run()
