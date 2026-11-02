# The next season 🌙 (roadmap)

Ghost in the Box is a community project for local, source-linked social workflows.
The same loop connects each lane (target platform): account reference → observed
conversation → draft → frozen job → delivery receipt → read-back. Terms are in
[plain terms](glossary.md).

## Available foundation

- Local account registry, inbox, and SQLite outbox with account leases and
  uncertain-send reconciliation.
- Instagram own-writing import, feed/comment readers, exact-job delivery,
  carousel drafts, visual reference tools, and MCP stdio transport.
- Threads readers, notification triage, cadence, text/reply adapters, and
  optional persistent browser transports.
- Substack SDK/MCP integration with persistent session cookies and compact
  publication receipts.
- A source-linked brief engine with platform-specific formats.
- One CLI with grouped help, `doctor` and `config`, isolated offline tests, and a
  configurable local state root.
- Exact-job publishing with `publish --dry-run` previews and `publish --due` for
  timers; Threads posts and replies queue by default.
- Instagram and Substack MCP servers for Claude Desktop, Claude Code, Batbox and
  other standard clients.
- Cross-platform browser sign-in and selected-session export/import; experimental
  LinkedIn identity/reads and text-only outbox delivery.

Provider authentication is established by the operator's own live account
checks. This plan contains no saved accounts or claims about a private
deployment's current connectivity.

## What we would love to grow

1. **A clearer first connection.** Better diagnostics and setup for each provider,
   including account identity, expired sessions, and recovery.
2. **A friendly conversation workspace.** Show inbox context, drafts, account
   state, delivery receipts, and uncertain jobs over the existing stores.
3. **More adapters.** Add Facebook, TikTok, and YouTube delivery paths
   one at a time with provider receipts and restart recovery tests. Their brief
   formats already exist; their publishers do not. Verify and extend the
   experimental LinkedIn adapter against real provider accounts.
4. **Better context for agents.** Improve source-linked voice corpora, reply
   context, and the handoff to Batbox or another coordinator. Threads is
   CLI-only today; MCP tools for it would help agents, as `linkedin-mcp` does
   for LinkedIn.
5. **More real platform acceptance.** Test fresh installations, optional browser
   dependencies, account setup, and provider changes on supported systems.

These are outcomes, not promised dates. Useful small fixes are welcome alongside
the larger ideas. [Join the garden →](../CONTRIBUTING.md)
