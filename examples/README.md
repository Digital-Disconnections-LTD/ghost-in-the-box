# Examples

Sample files for MCP clients, Substack and the brief engine. The full MCP guide,
including the tool catalog, is [docs/mcp.md](../docs/mcp.md).

| File | What it is for |
|---|---|
| `claude-desktop.mcp.json` | Claude Desktop. Merge its `mcpServers` entries into `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`), then restart Claude Desktop. |
| `claude-code.mcp.json` | Claude Code. Save it as `.mcp.json` in your project root to share it with the project, or add each server with `claude mcp add` (below). |
| `batbox.mcp.json` | Batbox only. Merge it into `~/.batbox/mcp.json`. Batbox sends Content-Length frames, so this file routes `substack-mcp` through `mcp-bridge`. |
| `substack-connection.example.json` | A manual Substack connection file for `SOCIAL_FARM_SUBSTACK_CONFIG`, if you do not use the login wizard. |
| `topic-pack.json` | A sample topic pack for `ghost-in-the-box brief`. `ghost-in-the-box brief --help` describes each field. |

## Before you copy an MCP file

1. Replace `/absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box` with the real path to the
   installed command. Desktop apps do not read your shell `PATH`, so use the full path. On Windows it is
   `C:\path\to\ghost-in-the-box\.venv\Scripts\ghost-in-the-box.exe`; write the backslashes as `\\` inside JSON.
2. Replace `YOUR_HANDLE` and `YOUR_SUBSTACK_HANDLE` with accounts you registered through
   `ghost-in-the-box login start`. The Instagram server refuses to start with a placeholder handle. If exactly
   one Instagram account is registered you may drop `--account` and its value.
3. Remove the Substack entry if you have not installed the `substack` extra.
4. To keep state somewhere other than the default, add `"env": {"SOCIAL_FARM_STATE_DIR": "/absolute/private/dir"}`
   to a server entry.

The arguments are passed straight to the program, not through a shell, so do not add quotes or `~`.

## Which transport

`instagram-mcp` and `substack-mcp` both speak standard MCP stdio (one JSON message per line). Claude Desktop,
Claude Code and other standard clients start them directly, with no bridge.

`mcp-bridge` exists only for clients that send Content-Length frames instead, such as Batbox.
`instagram-mcp` already answers framed messages itself; `substack-mcp` does not, which is why only the Substack
entry in `batbox.mcp.json` uses the bridge. Do not add the bridge for a standard client.

## Claude Code from the command line

```sh
claude mcp add ghost-in-the-box-instagram -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box instagram-mcp --account YOUR_HANDLE
claude mcp add ghost-in-the-box-substack -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box substack-mcp --account YOUR_SUBSTACK_HANDLE
```

Add `--scope project` to write the project's `.mcp.json` instead of your personal settings.

## Check the connection

List the tools offline with `ghost-in-the-box instagram-mcp --list-tools` (add `--format md` for a table).
Once connected, ask your agent to call `instagram_status`: it reports the account, whether its saved session is
ready, and what to run if it is not.
