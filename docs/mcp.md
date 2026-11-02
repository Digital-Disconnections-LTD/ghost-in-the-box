# Bring a Bat (MCP tools)

MCP (Model Context Protocol) lets an AI client such as Claude Desktop, Claude
Code, Codex CLI or Batbox list and call local tools. Ghost in the Box ships
three MCP servers and one adapter:

| Command | What it is |
| :--- | :--- |
| `ghost-in-the-box instagram-mcp` | 24 Instagram tools: research, media, the comment inbox, drafts, queueing and exact-job publishing. |
| `ghost-in-the-box linkedin-mcp` | 15 LinkedIn tools: identity, profile, own posts and messages reads, plus outbox drafts (text and one image with alt text), queueing, dry runs and exact-job publishing. |
| `ghost-in-the-box substack-mcp` | The python-substack MCP tools plus `connection_status`. Needs the `substack` extra. |
| `ghost-in-the-box mcp-bridge -- COMMAND...` | Batbox only. Lets a client that sends Content-Length frames talk to a newline-JSON server. |

**Threads is CLI-only today.** It has no MCP tools. An agent that can run shell
commands reaches it through `ghost-in-the-box threads` (see the
[command index](commands.md)).

## Before you connect a client

1. Install with the `instagram` extra (it is part of `recommended`) for the
   Instagram server. The LinkedIn server needs only the base install. Add
   `substack` if you want the Substack server.
2. Register each account with the login wizard:
   `ghost-in-the-box login start --platform instagram --account YOUR_HANDLE`, or
   `--platform linkedin` with your LinkedIn public handle (the part after
   `linkedin.com/in/`).
3. Find the full path of the command. Desktop apps do not read your shell
   `PATH`. With the virtual environment active, run `command -v ghost-in-the-box`
   on macOS or Linux, or `where.exe ghost-in-the-box` on Windows.
4. Check the tools offline: `ghost-in-the-box mcp-tools` lists every server's
   tools; `instagram-mcp --list-tools` and `linkedin-mcp --list-tools` list one.

The ready-made client files and their placeholders are explained in
[examples/README.md](../examples/README.md). In every file, replace
`/absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box` with your path
and `YOUR_HANDLE` with your handle. Arguments go straight to the program, not
through a shell, so do not add quotes or `~`. To use another state dir, add
`"env": {"SOCIAL_FARM_STATE_DIR": "/absolute/private/dir"}` to a server entry.

## Claude Desktop

Merge the `mcpServers` entries from
[examples/claude-desktop.mcp.json](../examples/claude-desktop.mcp.json) into
`claude_desktop_config.json`, then restart Claude Desktop:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json` (write each `\` in a
  JSON path as `\\`)

Remove the entries you do not use, and the Substack entry if you did not
install the `substack` extra.

## Claude Code

Either save [examples/claude-code.mcp.json](../examples/claude-code.mcp.json) as
`.mcp.json` in your project root, or add each server from the command line:

```sh
claude mcp add ghost-in-the-box-instagram -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box instagram-mcp --account YOUR_HANDLE
claude mcp add ghost-in-the-box-linkedin -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box linkedin-mcp --account YOUR_HANDLE
claude mcp add ghost-in-the-box-substack -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box substack-mcp --account YOUR_SUBSTACK_HANDLE
```

The default scope is this project, for you only. Add `--scope project` to write
the shared `.mcp.json`, or `--scope user` for all your projects. Add
`-e SOCIAL_FARM_STATE_DIR=/absolute/private/dir` after the server name to pick a
state dir.

## Codex CLI

Codex CLI reads MCP servers from `~/.codex/config.toml`, one
`[mcp_servers.NAME]` table per server with `command`, `args` and an optional
`env`. Merge the tables from
[examples/codex.config.toml](../examples/codex.config.toml), for example:

```toml
[mcp_servers.ghost-in-the-box-linkedin]
command = "/absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box"
args = ["linkedin-mcp", "--account", "YOUR_HANDLE"]
env = { SOCIAL_FARM_STATE_DIR = "/absolute/private/dir" }  # optional
```

Or add each server from the command line, then check the list:

```sh
codex mcp add ghost-in-the-box-instagram -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box instagram-mcp --account YOUR_HANDLE
codex mcp add ghost-in-the-box-linkedin -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box linkedin-mcp --account YOUR_HANDLE
codex mcp add ghost-in-the-box-substack -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box substack-mcp --account YOUR_SUBSTACK_HANDLE
codex mcp list
```

Codex speaks newline JSON, so no bridge is needed.

## Batbox

Merge [examples/batbox.mcp.json](../examples/batbox.mcp.json) into
`~/.batbox/mcp.json`. Batbox sends Content-Length frames. `instagram-mcp` and
`linkedin-mcp` understand those themselves, but `substack-mcp` does not, so the
Batbox file runs Substack through `mcp-bridge`:

```sh
ghost-in-the-box mcp-bridge -- /absolute/path/to/ghost-in-the-box/.venv/bin/ghost-in-the-box substack-mcp
```

Do not add the bridge for Claude Desktop, Claude Code, Codex CLI or any other
standard client. They speak newline JSON, which every server understands
directly.

## Which Instagram account the server uses

The server serves exactly one Instagram account per process. It picks it in
this order:

1. `--account HANDLE` on the command line.
2. The `SOCIAL_FARM_IG_ACCOUNT` setting.
3. The only registered Instagram account, if there is exactly one.

If none of these gives an account, or several accounts are registered and none
was chosen, the server refuses to start. It exits with code 2 and a hint such as
`ghost-in-the-box login start --platform instagram --account YOUR_HANDLE`. It
also refuses the documentation placeholders `YOUR_HANDLE` and `example_creator`
(unless you really registered one of those names) and anything that is not a
valid Instagram handle. A client shows that refusal as a server that failed to
start; run the same command in a terminal to read the hint.

## Which LinkedIn account the server uses

`linkedin-mcp` also serves exactly one account per process, chosen the same way:

1. `--account HANDLE` on the command line: your public handle, the part after
   `linkedin.com/in/` (letters, digits, `-`, `_` and `.`).
2. The `SOCIAL_FARM_LINKEDIN_ACCOUNT` setting.
3. The only registered LinkedIn account, if there is exactly one.

Otherwise it exits with code 2 and a hint such as
`ghost-in-the-box login start --platform linkedin --account YOUR_HANDLE`. It
refuses the placeholders `YOUR_HANDLE`, `YOUR_PUBLIC_HANDLE` and
`example_creator` unless you really registered that name.

## Check the connection

Ask your agent to call `instagram_status` or `linkedin_status`. Each reports the
account, whether the saved session is ready, the job counts, and what to run if
something is missing. `linkedin_status` makes no network request. Tools that
reach Instagram need the `instagram` extra and a registered session; the
catalog itself works without either.

The servers negotiate MCP protocol versions 2025-06-18, 2025-03-26 and
2024-11-05. Errors from a tool call come back as structured results,
`{"ok": false, "error": {"code", "message", "hint"}}`, and the hint names the
tool to call next.

## Working rules for agents

- `instagram_publish_job` makes a real public post. It publishes the exact
  queued job you name, once. Queue with `instagram_queue_job` first.
- If a publish result is uncertain, do not publish again. Look at the account,
  then call `instagram_reconcile_job` with what you saw.
- `instagram_claim_comments` holds comments for one hour for a `worker` name you
  choose. Use the same name for `instagram_draft_reply` and
  `instagram_skip_comment`.
- `instagram_draft_reply` only saves text. Replies are not sent by this tool.
- `linkedin_publish_job` makes a real public LinkedIn post through the same
  publisher as `ghost-in-the-box publish`. Draft with `linkedin_draft_post`,
  queue with `linkedin_queue_job` and check `linkedin_dry_run` first. A job
  scheduled for later is refused as `not_due` unless you pass `now=true`.
- If a LinkedIn publish is uncertain, do not draft it again. Check the feed,
  then call `linkedin_resolve_uncertain` with what you saw (for `published`, the
  post's `urn:li:share:N` from its link).
- `linkedin_messages` and `linkedin_conversations` only read. No tool sends a
  LinkedIn message.
- Substack publishing tools can send a live newsletter. Use them only for content
  the account owner approved.

See the [agent workflow](agent-workflow.md) for the full draft, queue, publish
and reconcile loop.

## Tool catalog

The tables below are the output of
`ghost-in-the-box mcp-tools --format md`, which covers every server. To refresh
them after upgrading, run that command and replace everything between the
`GENERATED` comment markers with its output, moving each heading down one level.
Use `--format json` for a short machine-readable list, or `--format mcp` for the
full tool definitions with input schemas. Your MCP client also lists the
upstream python-substack tools after it connects.

<!-- BEGIN GENERATED: ghost-in-the-box mcp-tools --format md -->
### Generated catalog

Generated by `ghost-in-the-box mcp-tools --format md` from Ghost in the Box 0.1.0. Supported MCP protocol versions: 2025-06-18, 2025-03-26, 2024-11-05.

### ghost-in-the-box-instagram

Start: `ghost-in-the-box instagram-mcp --account HANDLE`

| Tool | Title | What it does | Required | Optional | Access |
|---|---|---|---|---|---|
| `instagram_status` | Instagram account status | Check configured Instagram account, local session readiness and job counts. |  | `live_check` | reads the provider |
| `instagram_own_posts` | Read own Instagram posts | Read one complete page (up to 33) of the connected account's own Instagram posts. |  | `cursor` | reads the provider |
| `instagram_media` | Read an Instagram post | Read any accessible Instagram post's caption, author and slide URLs. |  | `media_id`, `url` | reads the provider |
| `instagram_discover` | Discover posts by hashtag | Discover topic posts with one bounded Instagram hashtag page using the saved session. | `hashtag` | `sort`, `limit` | writes; contacts the provider |
| `instagram_references` | Browse saved research posts | Browse saved Instagram research candidates and downloaded image paths locally. |  | `state`, `hashtag`, `query`, `limit`, `offset` | local read |
| `instagram_curate` | Curate a saved research post | Mark a saved Instagram research candidate new, selected or skipped and retain a note. | `media_id`, `state` | `note` | local write |
| `instagram_download_media` | Download post images | Download images from one Instagram post or carousel, including other authors' posts. |  | `media_id`, `url`, `max_images`, `start_index` | writes; contacts the provider |
| `instagram_inspect_slide` | Inspect a downloaded image | See one downloaded Instagram image using the existing local vision model. | `image_path` | `question`, `max_tokens` | local read |
| `instagram_search_memes` | Search the meme library | Search the cached technology meme library by topic and words in titles or images. |  | `query`, `category`, `limit`, `offset`, `min_score` | local read |
| `instagram_import_meme` | Add an image to the meme library | Add one image already returned by download_media to the categorized meme library. | `image_path` |  | local write |
| `instagram_prepare_meme_carousel` | Prepare meme carousel slides | Prepare 2–10 cached meme IDs or paths as ordered 1080×1350 JPEG slides. | `items` |  | local write |
| `instagram_comments` | Read comments on own post | Read one complete comment page on your own Instagram media and retain it in the inbox. | `media_id` | `comment_id`, `cursor` | writes; contacts the provider |
| `instagram_sync_inbox` | Refresh the comment inbox | Refresh a bounded selection of own-media comments and optional feed. |  | `posts`, `pages`, `threads`, `feed_pages`, `older` | writes; contacts the provider |
| `instagram_inbox` | Read the cached inbox | Read cached unread comments, pending reviews, or feed items without consuming them. |  | `view`, `limit`, `worker` | local read |
| `instagram_claim_comments` | Claim comments for review | Claim unread comments for review for one hour. | `worker` | `limit` | local write |
| `instagram_skip_comment` | Skip a claimed comment | Finish a comment review claimed with claim_comments without replying. | `comment_id`, `worker` |  | local write |
| `instagram_draft_reply` | Draft a comment reply | Save the exact reply text for a claimed inbox comment. | `comment_id`, `text`, `worker` |  | local write |
| `instagram_draft_carousel` | Draft a carousel post | Snapshot 2–10 ordered local images and an exact caption into one Instagram draft. | `caption`, `images`, `idempotency_key` | `crosspost_threads`, `source_urls` | local write |
| `instagram_queue_job` | Queue a draft job | Freeze and queue one exact Instagram draft. | `job_id` | `at` | local write |
| `instagram_publish_job` | Publish a queued job | Publish exactly one queued Instagram job now, including a future-scheduled job. | `job_id` |  | writes; contacts the provider |
| `instagram_jobs` | List Instagram jobs | List bounded Instagram job summaries for this configured account. |  | `status`, `limit` | local read |
| `instagram_job` | Inspect one Instagram job | Inspect one Instagram job's exact text, frozen assets, target and durable outcome. | `job_id` |  | local read |
| `instagram_reconcile_job` | Reconcile an uncertain job | Resolve an uncertain Instagram job using observed evidence. | `job_id`, `outcome`, `reason` | `provider_id` | writes; contacts the provider |
| `instagram_retry_job` | Retry a failed job | Requeue a failed-before-send Instagram job after correcting the failure. | `job_id`, `reason` |  | local write |

### ghost-in-the-box-linkedin

Start: `ghost-in-the-box linkedin-mcp --account HANDLE`

Uses LinkedIn's unofficial web routes through your saved browser session; they may change. Posting is experimental and goes through the outbox.

| Tool | Title | What it does | Required | Optional | Access |
|---|---|---|---|---|---|
| `linkedin_status` | LinkedIn account status | Check the configured LinkedIn account without any network request. |  |  | local read |
| `linkedin_me` | Confirm the LinkedIn identity | Confirm which LinkedIn member the saved session belongs to, with one bounded read. |  |  | reads the provider |
| `linkedin_profile` | Read a LinkedIn profile | Read one LinkedIn member's profile by public handle (the part after linkedin.com/in/). | `handle` |  | reads the provider |
| `linkedin_posts` | Read own LinkedIn posts | Read one bounded page of the connected account's own recent LinkedIn posts. |  | `limit`, `start` | reads the provider |
| `linkedin_conversations` | List LinkedIn conversations | List one bounded page of the connected account's recent LinkedIn message conversations. |  | `limit`, `start` | reads the provider |
| `linkedin_messages` | Read a LinkedIn conversation | Read one bounded page of messages in one LinkedIn conversation. | `conversation_id` | `limit`, `start` | reads the provider |
| `linkedin_draft_post` | Draft a LinkedIn post | Save exact post text, with at most one local image and its alt text, as a LinkedIn outbox draft for this account. | `text` | `source_url`, `media_path`, `alt_text`, `idempotency_key` | local write |
| `linkedin_list_jobs` | List LinkedIn jobs | List this account's LinkedIn outbox jobs, newest first. |  | `status`, `limit` | local read |
| `linkedin_show_job` | Inspect one LinkedIn job | Inspect one LinkedIn job's exact text, image, alt text, schedule, last error and receipt. | `job_id` |  | local read |
| `linkedin_queue_job` | Queue a LinkedIn draft | Freeze a LinkedIn draft's exact text and image hash and schedule it. | `job_id` | `scheduled_at` | local write |
| `linkedin_cancel_job` | Cancel a LinkedIn job | Cancel a LinkedIn draft or queued job before it is published. | `job_id` | `reason` | local write |
| `linkedin_dry_run` | Preview a LinkedIn publish | Preview what linkedin_publish_job would send for one job, without any network request. | `job_id` | `now` | local read |
| `linkedin_publish_job` | Publish a LinkedIn job | Publish exactly one queued LinkedIn job through the outbox publisher; this makes a public LinkedIn post. | `job_id` | `now` | writes; contacts the provider |
| `linkedin_retry_failed` | Retry a failed LinkedIn job | Requeue a LinkedIn job that failed before sending, once you corrected the cause. | `job_id`, `reason` |  | local write |
| `linkedin_resolve_uncertain` | Resolve an uncertain LinkedIn job | Record what really happened to an uncertain LinkedIn job after checking the account's feed. | `job_id`, `outcome`, `reason` | `provider_id` | local write |

### ghost-in-the-box-substack

Start: `ghost-in-the-box substack-mcp --account HANDLE`

Needs the substack extra. Also exposes the upstream python-substack tools, which your MCP client lists after connecting.

| Tool | Title | What it does | Required | Optional | Access |
|---|---|---|---|---|---|
| `connection_status` | Connection status | Read local connection setup and the latest publication receipt. |  |  |  |
<!-- END GENERATED -->
