"""ArmorPaint MCP — an MCP server that drives ArmorPaint 1.0 through a minic bridge plugin.

Two halves, one contract (``docs/PROTOCOL.md``):

* ``armorpaint_mcp.transport`` — the file-mailbox client (this side writes atomically with
  ``os.replace``; the plugin replies with a two-file commit because no rename binding exists).
* ``armorpaint_mcp.server``    — the MCP server proper, over stdio.
"""

__version__ = "1.0.0"

WIRE_PROTOCOL_VERSION = 1

__all__ = ["__version__", "WIRE_PROTOCOL_VERSION"]
