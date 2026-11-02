# Listen, make, send, remember (agent workflow)

The inbox and outbox are shared authorities for people and agents. A Bat (an AI
agent) can use MCP tools or the CLI while keeping its own Mission (task) tracking
in its coordinator. Other terms are in [plain terms](glossary.md).

1. **Connect:** sign in with `ghost-in-the-box login start --platform P --account
   HANDLE`, or register an existing private session with `accounts register`.
   Verify that the provider identity matches the account you intend to use.
2. **Listen:** read a bounded set of source posts and replies. Keep provider IDs,
   URLs, authors, and timestamps with the observations.
3. **Make:** prepare a brief with sources and develop text in your own voice.
   An emitted body file is a draft, not a delivery receipt.
4. **Draft:** use `ghost-in-the-box outbox draft --platform PLATFORM --account HANDLE
   --body-file FILE`. Reply drafts also carry the provider's parent IDs. For
   Threads, `ghost-in-the-box threads --account HANDLE post --body-file FILE`
   drafts and queues in one step and sends nothing.
5. **Queue:** inspect the draft, then run `ghost-in-the-box outbox queue JOB_ID`.
   Queueing freezes its text and media bytes.
6. **Deliver:** preview with `ghost-in-the-box publish --dry-run JOB_ID`, then run
   `ghost-in-the-box publish JOB_ID` for the exact job. The worker claims it
   atomically and records its provider ID and permalink. A job scheduled for
   later is refused until it is due, unless you add `--now`. Installed timers
   run `publish --due` instead, which sends every due job.
7. **Read back:** observe the resulting conversation and keep source-linked
   context for future work.

## When the response is lost

A provider may accept a post even if the process loses its response. That job
becomes `uncertain`, and `publish` exits with code 3. Inspect provider evidence
before resolving it with `ghost-in-the-box outbox resolve JOB_ID --outcome
published|queued|failed --reason "..."`; blindly retrying can create a duplicate.

The states distinguish `draft`, `queued`, `in_flight`, `published`, `failed`,
`uncertain`, `superseded`, and `cancelled`. While one job for an account key is
`in_flight` or `uncertain`, no other job for that key is sent. Instagram and
Threads share the key `meta:HANDLE`.

Pre-send failures and ambiguous sends have different recovery paths. A job that
failed before sending, or that the platform clearly refused, can be requeued with
`outbox retry-failed JOB_ID --reason "..."`. A job settled as failed through
`resolve` may have been sent, so `retry-failed` refuses it with `possibly_sent`.
Cancel a draft or queued job you no longer want with `outbox cancel JOB_ID`.

## Bring your own coordinator

The CLI and [MCP tools](mcp.md) work with Batbox, Claude Desktop, Claude Code
and other clients. Threads and LinkedIn are reachable through the CLI only.
Account authentication, social provider permissions, and an agent harness's
permission mode are separate concerns. Ghost in the Box commands do not wait for
approval dialogs; they return results or clear setup/authentication errors in
the [JSON error format](configuration.md#errors-and-exit-codes), with a `hint`
naming the next command.

Only run a live publish for an account and content the user has authorized.
Offline tests use fake accounts and temporary state.
