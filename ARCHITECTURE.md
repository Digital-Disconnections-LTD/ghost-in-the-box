# The shape of the garden

Ghost in the Box is a local Python application with explicit provider boundaries.
The `ghost-in-the-box` CLI calls the same modules used by its scripts and MCP tools.

```text
account/session reference → provider reader → inbox source record
                                             ↓
source-linked brief → reviewed text/media → outbox draft → queued frozen job
                                                        ↓
                                              one provider delivery
                                                        ↓
                                               receipt / uncertain
                                                        ↓
                                               read-back and context
```

- `farm/config.py` selects the shared local state root.
- `farm/accounts.py` owns account-to-session references and file locking.
- `farm/inbox.py` owns observed posts, comments, cursors, and notification batches.
- `farm/outbox.py` owns drafts, immutable queued payloads, leases, provider receipts,
  and reconciliation. It is the delivery authority.
- `farm/adapters/` contains Instagram and Threads provider operations. Browser and
  Scrapling transports implement the same Threads boundary.
- `farm/instagram_mcp.py` exposes bounded Instagram and local media tools.
- `farm/substack_connection.py` wraps the optional Substack SDK's cookie persistence
  and publication receipts. It does not claim to be a general outbox adapter.
- `buzz/buzz_brief.py` creates cited, dated briefs and draft body files. Formatting
  a lane does not establish that a publisher for that platform exists.
- `farm/cli.py` routes command-line operations; `scripts/` preserves direct entry points.
- `scripts/mcp_stdio_bridge.py` adapts MCP framing without changing tool payloads.

A task manager or agent coordinator remains responsible for its own Missions.
Ghost in the Box does not mirror its task state into the inbox or outbox. Batbox can
read source-linked records and call tools through its existing MCP interface.

SQLite transactions and account locks coordinate local workers. A provider call
can succeed while its response is lost; that delivery remains uncertain until
reconciled. A receipt or read-back is evidence of a send, not a generated brief.

See [the workflow](docs/agent-workflow.md), [configuration](docs/configuration.md),
and [contribution guide](CONTRIBUTING.md).
