# The shape of the garden (architecture)

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

Terms such as lease, receipt and account key are defined in
[plain terms](docs/glossary.md).

## Modules

Core state:

- `farm/config.py` selects the shared state root and builds every other path from
  it. It also lists every `SOCIAL_FARM_*` setting for `ghost-in-the-box config`.
- `farm/accounts.py` owns account-to-session references and account keys.
  Instagram and Threads default to `meta:HANDLE`; legacy `instagram:` and
  `threads:` keys read back as `meta:`.
- `farm/locking.py` is the one file-locking shim: POSIX `flock`, with an `msvcrt`
  backend on native Windows. Several modules import it under the name `fcntl`.
- `farm/session_files.py` reads and writes private session files atomically, with
  owner-only modes on POSIX and an owner-only ACL on Windows. It never stores
  passwords or browser profiles.
- `farm/login.py` verifies browser sign-ins and exports/imports selected sessions.
  It writes the same registry and provider files; exports contain no browser
  profile. Import stages every file, writes the registry once, and rolls back on
  failure.
- `farm/inbox.py` owns observed posts, comments, cursors, worker claims and
  notification batches.
- `farm/outbox.py` owns drafts, frozen queued payloads, leases, provider receipts,
  and reconciliation. It is the delivery authority.

Providers:

- `farm/adapters/__init__.py` is the adapter contract (below).
- `farm/adapters/instagram.py` sends through the saved instagrapi session.
- `farm/adapters/threads.py` sends through the Threads private API using the
  registered Instagram session.
- `farm/adapters/linkedin.py` makes bounded Voyager API calls with a browser
  session: identity, profile, own posts, conversations, messages, and
  experimental text posts.
- `farm/instagram_session.py` is the one durable Instagram login shared with a
  linked Threads account. `farm/instagram_references.py` keeps research posts and
  downloads, separate from publishing.
- `farm/substack_connection.py` wraps the optional Substack SDK's cookie
  persistence and publication receipts. It is not an outbox adapter.

Threads:

- `farm/threads_transport.py` decides which Threads transport an account uses
  and says why: portable cookies, Scrapling, the mobile API, or the Node browser
  bridge. The order depends on the purpose (agent commands, `publish`, or reads).
- `farm/threads_portable.py` injects portable cookies into Playwright and reuses
  the Threads browser executor and attempt journal.
- `farm/threads_scrapling.py` is the Scrapling transport, used after a verified
  browser login.
- `farm/threads_browser.py` is the persistent Node browser fallback for one exact
  outbox job. `farm/threads_reader.py` does bounded reads with the same fallback.
- `farm/threads_auth.py` mints the mobile bearer from the linked Instagram login.
  `farm/threads_native_auth.py` is the experimental Threads-app login.
- `farm/threads_cadence.py` keeps per-account post spacing (1 to 2 hours), New
  York quiet hours and the auth cooldown.
- `farm/threads_notifications.py` stores Threads replies in the same inbox as
  Instagram.
- `farm/threads_cli.py` re-exports the shared terminal helpers from
  `farm/cli_output.py` for the Threads commands, including `attach_error`
  (error envelope) and `read_body` (`--body`, `--body-file PATH` or
  `--body-file -`).

Entry points:

- `farm/cli_output.py` is the terminal contract every command follows: one JSON
  document on stdout, the shared error envelope with a `hint`, and the stable
  exit codes (0, 1, 2, 3, 4, 130).
- `farm/cli.py` is the `ghost-in-the-box` command. It groups commands, suggests
  close matches for typos, and maps each command to a module in `scripts/`.
  It also implements `doctor` and `config`.
- `scripts/` keeps one module per command. `scripts/threads_agent.py` is the
  `threads` command. `scripts/social_publish_job.py` is `publish`.
  `scripts/ig_publish_once.py` and `scripts/threads_publish_once.py` are thin
  timer wrappers around `publish --due`; the Threads one adds cadence.
- `farm/instagram_mcp.py` exposes bounded Instagram and local media tools over
  MCP stdio.
- `scripts/mcp_stdio_bridge.py` adapts Content-Length framing to newline JSON
  without changing tool payloads.
- `buzz/buzz_brief.py` creates cited, dated briefs and draft body files. Formatting
  a lane does not establish that a publisher for that platform exists.

## The adapter contract

Every publishing adapter sends one claimed job once and returns
`{"provider_id", "permalink"}`. It reports problems with the shared exceptions in
`farm/adapters/__init__.py`, so one router decides what happens to the job:

| Exception | Meaning | What happens to the job |
| :--- | :--- | :--- |
| `AuthUnavailable` | The saved login could not be used. Nothing was sent. | Back to `queued`, unchanged. |
| `DependencyUnavailable` | An optional package or browser is missing. Nothing was sent. | Back to `queued`; the result carries an install hint. |
| `PreSendFailure` | A check failed before sending. | `failed` (can be retried). |
| `PublishRejected` | The platform clearly refused the write (a 4xx answer). | `failed` (can be retried). |
| `PublishUncertain` | A write may have reached the platform (timeout, 5xx, 408, 409, unreadable answer). | `uncertain`. Never resent automatically. |
| `ReadFailure` | A bounded read failed. | Not a publishing outcome. |

`DependencyUnavailable` subclasses `AuthUnavailable`, which subclasses
`PreSendFailure`, so older callers still keep the job queued. Adapters accept a
`before_send` callback that runs right before the first byte of a public write;
raising from it leaves nothing sent. Error text from providers passes through
`safe_reason`, which drops anything that looks like a token. Adapters that can
read back provider state implement `reconcile(job)` without sending anything.

## Outbox lifecycle

States: `draft`, `queued`, `in_flight`, `published`, `failed`, `uncertain`,
`superseded`, `cancelled`. Only `instagram`, `threads` and `linkedin` jobs are
accepted; Facebook, TikTok and YouTube are refused as brief-only.

- **Queue** freezes the body and media and stores their SHA-256 hashes. Publishing
  refuses a job whose payload no longer matches.
- **Claim** atomically moves one due job to `in_flight` with a random lease token
  and a lease deadline. `publish` uses a 30-minute lease. No job is claimed for an
  account key that already has a job `in_flight` or `uncertain`.
- **Finish** needs the lease token. `published` needs a provider ID (the receipt).
- **Lease expiry** turns an `in_flight` job into `uncertain` and keeps the old
  token as `expired_lease_token`. If the original sender then gets its receipt,
  it can still record `published`, with a note that the receipt arrived late.
  Any other holder gets `lease_lost`. If writing the receipt itself fails,
  `publish` reports `receipt_not_recorded` with exit code 3.
- **Resolve** settles an `uncertain` job as `published`, `queued` or `failed` with a
  reason. **Retry** requeues a `failed` job only when its `failure_kind` is
  `pre_send` or `rejected`; a job failed through reconciliation is refused with
  `possibly_sent`. **Cancel** works on drafts and queued jobs that nobody holds.

### Schema versions

The outbox database has a one-row `schema_version` table. Opening the database
runs forward-only migrations inside one transaction. Version 2 added the
`expired_lease_token` and `failure_kind` columns and back-filled `failure_kind`
for existing failed jobs. A database newer than the code is refused instead of
being changed. To add a migration, append `(version, function)` to `MIGRATIONS`
in `farm/outbox.py` and raise `SCHEMA_VERSION`; each function must tolerate
columns that already exist.

## Coordination

A task manager or agent coordinator remains responsible for its own Missions.
Ghost in the Box does not mirror its task state into the inbox or outbox. Batbox can
read source-linked records and call tools through its existing MCP interface.

SQLite transactions and account locks coordinate local workers. A provider call
can succeed while its response is lost; that delivery remains uncertain until
reconciled. A receipt or read-back is evidence of a send, not a generated brief.

See [the workflow](docs/agent-workflow.md), [configuration](docs/configuration.md),
and [contribution guide](CONTRIBUTING.md).
