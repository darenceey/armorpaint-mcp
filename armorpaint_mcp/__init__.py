"""ArmorPaint MCP — an MCP server that drives ArmorPaint 1.0 through a minic bridge plugin.

Two halves, one contract (``docs/PROTOCOL.md``):

* ``armorpaint_mcp.transport`` — the file-mailbox client (this side writes atomically with
  ``os.replace``; the plugin replies with a two-file commit because no rename binding exists).
* ``armorpaint_mcp.server``    — the MCP server proper, over stdio.

Answered by the server itself, on any ArmorPaint build:

* ``armorpaint_mcp.window_capture`` — screenshots of ArmorPaint's window (X11, Win32, macOS).
* ``armorpaint_mcp.desktop_input``  — synthetic input to that window: waking a dozing app, and
  clicks / keys / drags / scrolls for UI automation and keyboard undo on stock builds.
* ``armorpaint_mcp.local_tools``    — resource search and per-project metadata sidecars.
"""

__version__ = "1.2.0"

WIRE_PROTOCOL_VERSION = 1

__all__ = ["__version__", "WIRE_PROTOCOL_VERSION"]
