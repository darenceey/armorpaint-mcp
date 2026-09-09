# Installing armorpaint-mcp (Windows)

Two halves have to be installed: a **plugin inside ArmorPaint**, and a **Python MCP server** your
client launches. They find each other through a directory of files (the *spool*), so the only
configuration that matters is that both agree on where that directory is.

Linux and macOS should work — nothing in the design is Windows-specific — but only Windows has been
tested, so this document is written for it.

## 0. Prerequisites

- **ArmorPaint 1.0** — the current C/minic generation, either the paid binary or a self-built one.
  **Help → About…** reports the version, build date, commit SHA, graphics API and GPU. A second,
  more decisive check: the install directory contains `data\plugins\*.c`. If the plugins there are
  `.js` or `.hx` instead, you have a pre-2025 Haxe/Kha build and nothing in this repository applies
  to it.
- **Python 3.11 or newer** — `python --version`. (`pyproject.toml` sets `requires-python = ">=3.11"`; pip refuses to install on 3.10.)
- **An MCP client** that launches stdio servers (Claude Code, Claude Desktop, …).

No compiler is required. The optional viewport patch (step 8) is the only part that needs one.

## 1. Find ArmorPaint's `data` directory

This is the one step people get wrong, so it is worth doing deliberately.

ArmorPaint resolves its data directory **relative to the executable** — literally
`<directory containing ArmorPaint.exe>\data\`. It is not in `AppData`, not in `Documents`, and it
does **not** move even when ArmorPaint redirects other files (see the note below).

So: **find `ArmorPaint.exe`, and `data` is the folder next to it.** With ArmorPaint running, this
tells you exactly where that is:

```powershell
$exe = (Get-Process ArmorPaint).Path
$exe
$data = Join-Path (Split-Path $exe) "data"
$data
```

If it is not running, the answer depends on how you installed it — a zip you unpacked yourself puts
it wherever you unpacked it; the itch.io app puts it under its own managed apps folder; a
self-built copy puts it in the build output directory beside the executable. Rather than guess:

```powershell
Get-ChildItem C:\, D:\, $env:LOCALAPPDATA, $env:APPDATA -Recurse -Filter ArmorPaint.exe `
    -ErrorAction SilentlyContinue -Depth 6 | Select-Object -ExpandProperty FullName
```

**Confirm you have the right `data` directory** — it must contain a `plugins` subdirectory holding
the bundled examples:

```powershell
Get-ChildItem "$data\plugins" -Filter *.c
```

You should see `autosave.c`, `converter.c`, `hello_world.c`, `hello_node.c`,
`hello_node_brush.c`, `import_stl.c`, `make_tilesheet.c`, `viewport_celshade.c`. If you see those
eight, this is the directory. If the directory does not exist, create it — ArmorPaint reads it
whether or not it was shipped.

> ### The `Program Files` complication
>
> When ArmorPaint's install directory contains `Program Files`, it treats itself as living in a
> protected location and writes its **`config.json` to `%USERPROFILE%\Saved Games\ArmorPaint\`**
> instead of into `data\`. Plugins are **not** redirected — they are still loaded from
> `<install>\data\plugins\`, which now needs administrator rights to write.
>
> Consequences: copy the plugin with the in-app **Import** button (step 3, variant B) rather than by
> hand, and do not leave the spool at its default under `data\` — set `ARMORPAINT_SPOOL` to
> something like `%LOCALAPPDATA%\armorpaint-mcp\spool` (step 6).
>
> Installing ArmorPaint somewhere like `C:\ArmorPaint` sidesteps all of this.

## 2. Get this repository

```powershell
git clone https://github.com/<owner>/armorpaint-mcp.git C:\src\armorpaint-mcp
```

Any location works; it does not need to be near ArmorPaint.

## 3. Install the bridge plugin

**Variant A — copy the file** (works when `data\plugins` is writable):

```powershell
Copy-Item "C:\src\armorpaint-mcp\plugin\armorpaint_mcp_bridge.c" "C:\ArmorPaint\data\plugins\armorpaint_mcp_bridge.c"
```

**Variant B — let ArmorPaint copy it** (works regardless of permissions, and is the safer default):

In ArmorPaint, go to the **Plugins** tab → **Preferences** button → the **Plugins** section →
**Import**, and pick `C:\src\armorpaint-mcp\plugin\armorpaint_mcp_bridge.c`. ArmorPaint copies it into
`data\plugins` for you and confirms in the console: `Plugin imported: armorpaint_mcp_bridge.c`.

Either way, keep the `.c` extension exactly. The plugin list filters on it, and a file saved as
`armorpaint_mcp_bridge.c.txt` by a browser simply will not appear.

## 4. Enable it

In ArmorPaint:

1. Open the **Plugins** tab (a sidebar tab).
2. Click **Preferences** — this opens the Preferences box already on its Plugins section.
3. Find **armorpaint_mcp_bridge** in the list and **tick its checkbox.** (The list shows each
   plugin's filename with the extension stripped, so it matches `armorpaint_mcp_bridge.c`.)

The plugin starts the moment you tick it — there is no restart, and no "apply". Close the
Preferences box; that is when ArmorPaint writes the setting to `config.json`, so the plugin will
auto-start on every subsequent launch.

To confirm it loaded, look at ArmorPaint's **Console**: the bridge logs a one-line banner with its
version and spool path. Any minic compile error appears there too, as
`armorpaint_mcp_bridge.c:<line>: error: …` — there is no dialog, so the console is the only place it shows.

**To disable or remove it later:** untick the checkbox (stops it immediately), or right-click its
row for a menu offering **Edit in Text Editor**, **Edit in Script Tab**, **Export**, and **Delete**.

## 5. Install the Python server

```powershell
cd C:\src\armorpaint-mcp
python -m pip install -e .
```

Verify the package imports and can work out where your spool is — this exercises step 6's discovery
without needing ArmorPaint running:

```powershell
python -c "from armorpaint_mcp.transport import resolve_spool; r = resolve_spool(); print(r.path); print(r.source)"
```

It prints the resolved spool directory and the rule that chose it. If `source` says it fell back to
a per-user default, discovery did not find your ArmorPaint — see step 6.

## 6. Point both halves at the same spool

The spool is the mailbox directory. Its default is `<ArmorPaint data dir>\mcp_spool` — the plugin's
own default, because ArmorPaint's `data` directory is the only stable location a plugin can name
from inside the app. **Most installs need no configuration here at all.**

The server works out the path in this order, first hit wins:

1. **`ARMORPAINT_SPOOL`** — an absolute path to the spool directory itself.
2. **A config file** — `ARMORPAINT_MCP_CONFIG` if set, else
   `%APPDATA%\armorpaint-mcp\config.json`, `~/.config/armorpaint-mcp/config.json`, or
   `~/.armorpaint-mcp.json`. Recognised keys: `{"spool": "<abs path>"}` or
   `{"armorpaint_dir": "<install root>"}`.
3. **An ArmorPaint install** — `ARMORPAINT_DIR`, then `ARMORPAINT_EXE` (its parent), then a short
   list of common roots: `%LOCALAPPDATA%\Programs\ArmorPaint`, `%PROGRAMFILES%\ArmorPaint`,
   `%PROGRAMFILES(X86)%\ArmorPaint`, `C:\ArmorPaint`, `%USERPROFILE%\ArmorPaint`,
   `%USERPROFILE%\Documents\ArmorPaint`. **A candidate is accepted only if it actually contains
   `data\plugins`**, so a stale guess cannot win. The spool is then `<install>\data\mcp_spool`.
4. **A per-user fallback** — `%LOCALAPPDATA%\armorpaint-mcp\spool`.

Step 4 is a fallback, not a solution: it only works if the plugin is pointed at the same path.
**If the server lands there, it means it could not find your ArmorPaint at all** — the fix is
`ARMORPAINT_DIR`, not living with the fallback. The server records how it decided and names the
resolved path in every transport error, so you never have to guess which branch was taken.

Note that the itch.io app's managed directory is deliberately *not* in the guess list — its layout
is not stable enough to guess safely. If that is how you installed ArmorPaint, set `ARMORPAINT_DIR`
to the directory you found in step 1.

Whatever you choose, the plugin must agree. It defaults to `<data>\mcp_spool` with no configuration;
if you override the server side, point the plugin at the same directory.

## 7. Register the server with your MCP client

Add an entry to your client's `.mcp.json`. Two shapes, both fine — mirror whichever one your other
servers use.

**Plain Python:**

```json
{
  "mcpServers": {
    "armorpaint": {
      "command": "python",
      "args": ["-m", "armorpaint_mcp"]
    }
  }
}
```

No `env` block is needed when step 6's discovery finds your install. Add one only if it does not:

```json
{
  "mcpServers": {
    "armorpaint": {
      "command": "python",
      "args": ["-m", "armorpaint_mcp"],
      "env": {
        "ARMORPAINT_DIR": "C:\\ArmorPaint"
      }
    }
  }
}
```

**With `uv`,** which avoids depending on whatever `python` happens to be first on `PATH`:

```json
{
  "mcpServers": {
    "armorpaint": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": [
        "run",
        "--directory",
        "C:\\src\\armorpaint-mcp",
        "python",
        "-m",
        "armorpaint_mcp"
      ],
      "env": {
        "ARMORPAINT_DIR": "C:\\ArmorPaint"
      }
    }
  }
}
```

Note the doubled backslashes — these are JSON strings. Forward slashes work too and are less
error-prone. Restart your MCP client afterwards so it picks up the new server.

## 8. Optional: viewport capture (needs a self-built ArmorPaint)

Skip this unless you specifically want the agent to see the shaded 3D viewport. Everything else
works without it, and exported textures — which do reach disk — cover most of what an agent needs
to check its own work.

ArmorPaint's plugin API can capture the viewport to a GPU texture but has no binding that writes
those pixels to a file. The patch adds one (three hunks, twelve lines, wrapping a function that
already exists upstream):

```powershell
python C:\src\armorpaint-mcp\patch\apply_viewport_patch.py E:\path\to\armorpaint
# then rebuild, per upstream's readme:
cd E:\path\to\armorpaint\paint
..\base\make
MSBuild build\ArmorPaint.vcxproj -p:Configuration=Release -p:Platform=x64 -p:LLVMInstallDir="C:\Program Files\LLVM"
```

`--revert` undoes it. The script refuses to patch rather than guess if upstream has moved the
function it anchors on. Full rationale, the exact diff, and the licence position:
[UPSTREAM_CHANGES.md](UPSTREAM_CHANGES.md).

On a stock binary, `ap_capture_viewport` reports `unsupported` and names the missing
binding. It never silently does nothing.

## 9. Smoke test

Ask your agent for `ap_ping`. A healthy response names the ArmorPaint version, its uptime, and the
currently open project (or an empty string if none).

Then something with a visible effect, so you know the round trip is real and not cached:

> Using ArmorPaint, write "bridge ok" to the console.

The text should appear in ArmorPaint's console and in its status bar.

## If it does not work

Work through the [Troubleshooting section of the README](../README.md#troubleshooting) — it covers
bridge-not-detected, whether the window must be foregrounded (it must not), a plugin missing from
the list, and a plugin that loads but does nothing.

The two fastest diagnostics:

```powershell
# 1. Is the plugin alive? Read this twice, a second apart: "t" must increase.
Get-Content "C:\ArmorPaint\data\mcp_spool\heartbeat.json"

# 2. Is anything stuck? Leftover files here mean requests that were never answered.
Get-ChildItem "C:\ArmorPaint\data\mcp_spool\req","C:\ArmorPaint\data\mcp_spool\res"
```

A heartbeat whose `t` is frozen means the plugin loaded but is not being ticked: the bridge is
disabled, or ArmorPaint is not running. (`t` is seconds since app start, not wall-clock time, so its
absolute value means nothing — only that it advances.)
