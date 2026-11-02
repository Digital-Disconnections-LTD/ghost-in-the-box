# Examples

Sample files for MCP clients, Substack and the brief engine. The full MCP guide,
including the tool catalog, is [docs/mcp.md](../docs/mcp.md).

| File | What it is for |
|---|---|
| `claude-desktop.mcp.json` | Claude Desktop. Merge its `mcpServers` entries into `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`), then restart Claude Desktop. |
| `claude-code.mcp.json` | Claude Code. Save it as `.mcp.json` in your project root to share it with the project, or add each server with `claude mcp add` (below). |
| `batbox.mcp.json` | Batbox only. Merge it into `~/.batbox/mcp.json`. Batbox sends Content-Length frames, so this file routes `substack-mcp` through `mcp-bridge`. |
| `codex.config.toml` | Codex CLI. Merge its `[mcp_servers.NAME]` tables into `~/.codex/config.toml`, or add each server with `codex mcp add` (below). |
| `substack-connection.example.json` | A manual Substack connection file for `SOCIAL_FARM_SUBSTACK_CONFIG`, if you do not use the login wizard. |
| `topic-pack.json` | A sample topic pack for `ghost-in-the-box brief`. `ghost-in-the-box brief --help` describes each field. |

## Before you copy an MCP file

1. Replace `/absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box` with the real path to the
   installed command. Desktop apps do not read your shell `PATH`, so use the full path. On Windows it is
   `C:\path\to\ghost-in-the-box\.venv\Scripts\ghost-in-the-box.exe`; write the backslashes as `\\` inside JSON.
2. Replace `YOUR_HANDLE` and `YOUR_SUBSTACK_HANDLE` with accounts you registered through
   `ghost-in-the-box login start`: your Instagram handle in the Instagram entry, your LinkedIn public handle
   (the part after `linkedin.com/in/`) in the LinkedIn entry. The Instagram and LinkedIn servers refuse to
   start with a placeholder handle. If exactly one account of that platform is registered you may drop
   `--account` and its value.
3. Remove the entries you do not use, and the Substack entry if you have not installed the `substack` extra.
4. To keep state somewhere other than the default, add `"env": {"SOCIAL_FARM_STATE_DIR": "/absolute/private/dir"}`
   to a server entry (in `codex.config.toml`: `env = { SOCIAL_FARM_STATE_DIR = "/absolute/private/dir" }`).

The arguments are passed straight to the program, not through a shell, so do not add quotes or `~`.

## Which transport

`instagram-mcp`, `linkedin-mcp` and `substack-mcp` all speak standard MCP stdio (one JSON message per line).
Claude Desktop, Claude Code, Codex CLI and other standard clients start them directly, with no bridge.

`mcp-bridge` exists only for clients that send Content-Length frames instead, such as Batbox.
`instagram-mcp` and `linkedin-mcp` already answer framed messages themselves; `substack-mcp` does not, which is
why only the Substack entry in `batbox.mcp.json` uses the bridge. Do not add the bridge for a standard client.

## Claude Code from the command line

```sh
claude mcp add ghost-in-the-box-instagram -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box instagram-mcp --account YOUR_HANDLE
claude mcp add ghost-in-the-box-linkedin -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box linkedin-mcp --account YOUR_HANDLE
claude mcp add ghost-in-the-box-substack -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box substack-mcp --account YOUR_SUBSTACK_HANDLE
```

Add `--scope project` to write the project's `.mcp.json` instead of your personal settings.

## Codex CLI from the command line

```sh
codex mcp add ghost-in-the-box-instagram -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box instagram-mcp --account YOUR_HANDLE
codex mcp add ghost-in-the-box-linkedin -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box linkedin-mcp --account YOUR_HANDLE
codex mcp add ghost-in-the-box-substack -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box substack-mcp --account YOUR_SUBSTACK_HANDLE
codex mcp list
```

Each command writes a `[mcp_servers.NAME]` table to `~/.codex/config.toml`, the same tables as
`codex.config.toml`.

## Check the connection

List every server's tools offline with `ghost-in-the-box mcp-tools` (add `--format md` for tables), or one
server's with `ghost-in-the-box instagram-mcp --list-tools` or `ghost-in-the-box linkedin-mcp --list-tools`.
Once connected, ask your agent to call `instagram_status` or `linkedin_status`: each reports the account,
whether its saved session is ready, and what to run if it is not.
