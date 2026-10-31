# Listen, make, send, remember

The inbox and outbox are shared authorities for people and agents. A Bat can use
MCP tools or the CLI while keeping its own Mission tracking in its coordinator.

1. **Connect:** register your account's existing private session. Verify that the
   provider identity matches the account you intend to use.
2. **Listen:** read a bounded set of source posts and replies. Keep provider IDs,
   URLs, authors, and timestamps with the observations.
3. **Make:** prepare a brief with sources and develop text in your own voice.
   An emitted body file is a draft, not a delivery receipt.
4. **Draft:** use `ghost-in-the-box outbox draft --platform PLATFORM --account HANDLE
   --body-file FILE`. Reply drafts also carry the provider's parent IDs.
5. **Queue:** inspect the draft, then run `ghost-in-the-box outbox queue JOB_ID`.
   Queueing freezes its text and media bytes.
6. **Deliver:** run `ghost-in-the-box publish JOB_ID` for the exact job. The worker
   claims it atomically and records its provider ID and permalink.
7. **Read back:** observe the resulting conversation and keep source-linked
   context for future work.

## When the response is lost

A provider may accept a post even if the process loses its response. That job
becomes `uncertain`. Inspect provider evidence before resolving it with
`ghost-in-the-box outbox resolve`; blindly retrying can create a duplicate.

The states distinguish `draft`, `queued`, `in_flight`, `published`, `failed`,
`uncertain`, and `superseded`. An account lease coordinates overlapping workers.
Pre-send failures and ambiguous sends have different recovery paths.

## Bring your own coordinator

The CLI and [MCP connections](connectors.md) work with Batbox and other clients.
Account authentication, social provider permissions, and an agent harness's
permission mode are separate concerns. Ghost in the Box commands do not wait for
approval dialogs; they return results or clear setup/authentication errors.

Only run a live publish for an account and content the user has authorized.
Offline tests use fake accounts and temporary state.
