# Connect your tools

## Instagram and Batbox

Install the Instagram extra, register your private session, and confirm the
offline tool catalog:

```sh
python -m pip install -e '.[instagram]'
ghost-in-the-box instagram-mcp --list-tools
```

Merge the [example MCP configuration](../examples/batbox.mcp.json) into your own
Batbox `~/.batbox/mcp.json`. Replace the absolute executable paths and account
handle. MCP arguments are executable arguments, rather than shell commands.

The server accepts Content-Length frames and newline JSON. It exposes local
status, source/context tools, drafts, and explicit delivery tools; starting the
server does not start a publisher timer.

## Threads

For a linked Instagram/Threads identity, register the saved session for both
platforms using the same `--account-key meta:YOUR_HANDLE`. Verify each provider
independently. A working Instagram session is not proof that Threads accepts it.

The optional Scrapling path uses:

```sh
python -m pip install -e '.[browser]'
```

Scrapling's browser dependencies and a signed-in local profile are required.
`scripts/threads_scrapling_login.py --help` describes the bounded login helper.
Authentication can require the account owner to complete a provider challenge.

The alternative browser transport uses a separately installed, pinned
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

Standard newline-JSON MCP clients can run `ghost-in-the-box substack-mcp` directly.
For a framed client such as Batbox, the example configuration runs:

```sh
ghost-in-the-box mcp-bridge -- /absolute/path/to/ghost-in-the-box substack-mcp
```

`connection_status` reports local setup and the last receipt. Publishing tools
can send live newsletter content; use them only for the selected publication
and content you intend to send. Substack cookies and receipts stay outside Git.

## Web context and models

Use [Scrapling](https://github.com/D4Vinci/Scrapling) or your
[SearXNG](https://github.com/searxng/searxng) instance alongside the social tools
in Batbox. Ghost in the Box's optional image inspection accepts your configured
vision endpoint and model. Connected services retain their own data policies.
