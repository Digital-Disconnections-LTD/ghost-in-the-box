# Connect your tools (platform connections)

Use the [cross-platform login wizard](login.md) to create sessions and move
selected accounts between your desktop and server. MCP client setup (Claude
Desktop, Claude Code, Batbox) is in [MCP tools](mcp.md). Every command is in the
[command index](commands.md).

| Platform | CLI | MCP |
| :--- | :--- | :--- |
| Instagram | yes | yes (`instagram-mcp`) |
| Substack | setup only | yes (`substack-mcp`) |
| Threads | yes | no, CLI only today |
| LinkedIn | yes | no, CLI only today |

## LinkedIn

```sh
ghost-in-the-box login start --platform linkedin --account YOUR_PUBLIC_HANDLE
ghost-in-the-box linkedin --account YOUR_PUBLIC_HANDLE me
ghost-in-the-box linkedin --account YOUR_PUBLIC_HANDLE posts --limit 5
ghost-in-the-box linkedin --account YOUR_PUBLIC_HANDLE conversations --limit 5
ghost-in-the-box linkedin --account YOUR_PUBLIC_HANDLE messages CONVERSATION_ID --limit 5
```

These use the browser session with LinkedIn's unofficial Voyager API. That API
is undocumented, so its request and response shapes can change without notice.
Each read validates the account.
Cookie rotation is persisted, redirects are disabled, and reads use bounded pages.

Text-only posts use `outbox draft --platform linkedin`, `outbox queue JOB_ID`,
then `publish JOB_ID`. Delivery validates the frozen body and signed-in identity,
sends once through the share call LinkedIn's current web client uses (Voyager
GraphQL `voyagerContentcreationDashShares`), and requires a share URN receipt.
LinkedIn rotates that call's query id; when posts start failing with an invalid
query, set `SOCIAL_FARM_LINKEDIN_SHARE_QUERY_ID` (see [configuration](configuration.md)).
LinkedIn reports some refusals as HTTP 200 with an error list; those fail the job
as rejected. A failed job keeps the HTTP status and a short sanitised excerpt of
LinkedIn's answer in `last_error`. Missing/ambiguous receipts remain uncertain for
reconciliation. Media, comment writes, reactions, message sends, and organization
publishing are not implemented.
`posts` reads your own posts, not a feed. Profile, post, conversation and
message commands return provider JSON to your terminal;
redirect it only to private files when retaining personal content.

Network boundary: LinkedIn requests carry the selected session to
`https://www.linkedin.com/voyager/api/`. The login browser can visit LinkedIn and
the identity provider you choose; only LinkedIn cookies are retained.

## Instagram

The `instagram` extra (included in `recommended`) and a registered session are
needed. Sign in with the wizard, then check the account:

```sh
ghost-in-the-box login start --platform instagram --account YOUR_HANDLE
ghost-in-the-box accounts show --platform instagram --account YOUR_HANDLE
ghost-in-the-box instagram-inbox --account YOUR_HANDLE sync --posts 3 --pages 1
```

Posts go through the outbox (`outbox draft`, `outbox queue`, `publish JOB`), or
`carousel draft` for a 2 to 10 image carousel. For agents, `instagram-mcp`
exposes 24 tools covering research, the inbox, drafts and exact-job publishing.
Setup and the full tool list are in [MCP tools](mcp.md). Starting the server
does not start a publisher timer.

## Threads

Threads is CLI-only today. Sign in with the wizard, then check which transport
will be used and whether the session belongs to you:

```sh
ghost-in-the-box login start --platform threads --account YOUR_HANDLE
ghost-in-the-box threads --account YOUR_HANDLE status
ghost-in-the-box threads --account YOUR_HANDLE check
```

`status` makes no network request. `check` asks Threads who each saved session
belongs to and posts nothing.

`threads post`, `threads reply` and `threads reply-inbox` **queue** a job by
default and send nothing. Add `--now` to publish immediately, or publish the
queued job later with `ghost-in-the-box publish JOB` or `threads publish JOB`.
The idempotency key is derived from the action, target and text, so sending
the same text twice needs an explicit `--idempotency-key`.

```sh
ghost-in-the-box threads --account YOUR_HANDLE post --body-file draft.txt
ghost-in-the-box threads --account YOUR_HANDLE read own --limit 5
ghost-in-the-box threads-notifications --account YOUR_HANDLE sync
```

Instagram and Threads register with the shared account key `meta:YOUR_HANDLE`
by default, so their jobs never publish at the same time. Verify each platform
on its own. A working Instagram session is not proof that Threads accepts it.

The optional Scrapling transport uses:

```sh
python -m pip install -e '.[browser]'
ghost-in-the-box threads --account YOUR_HANDLE connect
```

`connect` reuses a wizard login when there is one. Otherwise it links your saved
Instagram session to a Scrapling browser profile with one bounded login. Add
`--refresh` to log in again. Scrapling's browser dependencies are required.
Authentication can require the account owner to complete a provider challenge.

The other browser transport uses a separately installed, pinned
[Threads MCP](https://github.com/bintangtimurlangit/threads-mcp) build and Node.
`ghost-in-the-box threads-setup --account YOUR_HANDLE` uses installed Node and
npm; without them, it can bootstrap the pinned runtime on Linux x86-64.
Other hosts need Node and npm installed first. An existing source build can be
selected with `SOCIAL_FARM_THREADS_MCP_SOURCE`. Browser execution needs a native desktop
display or `xvfb-run` on a headless Linux host. The core inbox and outbox do not
require Node.

## Substack

```sh
python -m pip install -e '.[substack]'
```

Create a private connection file following
[the example](../examples/substack-connection.example.json), point
`SOCIAL_FARM_SUBSTACK_CONFIG` at it, and supply the SDK cookie export from your
own sign-in. Confirm the selected publication with an authenticated read before
setting `ready`.

The login wizard creates this configuration automatically after checking the
signed-in handle and publication membership. Select it with
`ghost-in-the-box substack-mcp --account YOUR_SUBSTACK_HANDLE`. Existing
`SOCIAL_FARM_SUBSTACK_CONFIG` configuration continues to work without `--account`.

Standard MCP clients such as Claude Desktop and Claude Code run
`ghost-in-the-box substack-mcp` directly. Only Batbox, which sends Content-Length
frames, needs `mcp-bridge` in front of it. See [MCP tools](mcp.md).

`connection_status` reports local setup and the last receipt. Publishing tools
can send live newsletter content; use them only for the selected publication
and content you intend to send. Substack cookies and receipts stay outside Git.

## Web context and models

Use [Scrapling](https://github.com/D4Vinci/Scrapling) or your
[SearXNG](https://github.com/searxng/searxng) instance alongside the social tools
in Batbox or another MCP client. Ghost in the Box's optional image inspection accepts your configured
vision endpoint and model. Connected services retain their own data policies.
