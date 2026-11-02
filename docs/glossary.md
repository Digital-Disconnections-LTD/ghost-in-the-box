# Plain terms (glossary)

Ghost in the Box uses a few garden words and a few terms of art. This page says
what each one means in concrete terms. The [command index](commands.md) lists
every command.

## Project words

| Term | Plain meaning |
| :--- | :--- |
| **Garden** | The project and its community. "In the garden" headings list features. |
| **Seed** | Your first run. "Plant your first seed" means install and try the offline example. |
| **Plot** | A connection to one platform, such as Instagram or LinkedIn. "Pick a plot" lists the platforms. |
| **Season** | A roadmap period. "The next season" is the roadmap. |
| **Bat** | An AI agent that uses these tools. The name comes from Batbox. |
| **Batbox** | A separate agent client from the same team. It runs Bats and talks to tools over MCP. See [Batbox on GitHub](https://github.com/Digital-Disconnections-LTD/batbox). |
| **Mission** | A task an agent is working on. Missions live in the agent's own coordinator, never in the inbox or outbox. |
| **farm** | The project's older internal name. It survives in the `farm/` Python package, the `SOCIAL_FARM_*` settings and the default state folder name, so existing installs keep working. |
| **`.batbox`** | The folder in your home directory that holds the default state dir. It is shared with Batbox for historical reasons. You do not need Batbox installed. |
| **MCP** | Model Context Protocol. A standard way for an AI client to list and call tools over stdin/stdout. See [MCP tools](mcp.md). |

## Writing words

| Term | Plain meaning |
| :--- | :--- |
| **Brief** | A Markdown file made by `ghost-in-the-box brief`. It records the topic, dated "why now" facts, sources and per-platform limits. It is a writing aid, not a post. |
| **Topic pack** | The JSON input to `brief --json`. It lists the title, `why`, `angle`, `hooks`, `sources`, `lanes`, `voice` and optional `drafts`. `brief --help` describes each field, and [examples/topic-pack.json](../examples/topic-pack.json) is a working sample. |
| **Lane** | A target platform inside a brief: `threads`, `instagram`, `linkedin`, `facebook`, `tiktok`, `youtube` or `substack`. Each lane has a format and a character limit. A lane does not mean a publisher exists. |
| **Body file** | A plain UTF-8 text file holding the exact words of one post. `brief --emit` writes one per lane, named `*.LANE.body`. |
| **Brief-only platform** | Facebook, TikTok and YouTube. `brief` can format text for them, but the outbox refuses them with the error code `brief_only_platform`. You post those yourself. |

## Delivery words

| Term | Plain meaning |
| :--- | :--- |
| **Outbox** | The local SQLite database of posts you plan to send (`<state>/outbox.sqlite3`). It is the only record of what was sent. |
| **Inbox** | The local SQLite database of comments and replies that were read from a platform (`<state>/inbox.sqlite3`). |
| **Job** | One row in the outbox: one post or one reply, for one account, on one platform. It has an ID. |
| **Draft** | A job you can still change or cancel. Nothing is frozen yet. |
| **Queue / queued** | `outbox queue JOB` freezes a draft and gives it a scheduled time. A queued job waits for `publish`. |
| **Frozen payload** | The exact text and media bytes of a queued job, with their SHA-256 hashes. Publishing checks the hashes and refuses if anything changed. |
| **Exact job** | You publish one job by its ID (`publish JOB`). The tool never picks "the latest draft" for you. |
| **Due** | A queued job whose scheduled time has passed. `publish JOB` refuses a job that is not due unless you add `--now`. `publish --due` sends every due job. |
| **Dry run** | `publish --dry-run JOB` prints the frozen payload, the target account and anything that would block the send. It makes no network request. |
| **Claim** | A publisher atomically marks a queued job `in_flight` so no other process can send it. |
| **Lease** | How long a claim is valid. `publish` holds a 30-minute lease. If the lease runs out before a result is recorded, the job becomes `uncertain`. |
| **Receipt** | Proof that a send happened: the platform's post ID and, when known, its link. A job is `published` only with a receipt. A receipt that arrives after the lease ran out is still recorded. |
| **Uncertain** | Bytes may have reached the platform, but the result is unknown (timeout, server error, lost answer). The tool never resends an uncertain job by itself. You check the platform and run `outbox resolve`. |
| **Failed** | Nothing was published. Either a check failed before sending, or the platform clearly refused the post (a 4xx answer). `outbox retry-failed` can requeue it. |
| **Reconcile / resolve** | Decide what really happened to an uncertain job, using evidence from the platform, and record it. |
| **Idempotency key** | A stable label for "this one intended message". Drafting again with the same key and the same content returns the existing job instead of a new one. The same key with different content is refused. A repeated command therefore never creates a duplicate post. |

## Account words

| Term | Plain meaning |
| :--- | :--- |
| **Session** | Saved login cookies or tokens for one account. A session is a reusable credential: anyone holding it can act as you. |
| **Account registry** | `<state>/accounts.json`. It maps each platform and handle to the path of its session file. It does not copy the credentials. |
| **Account key** | The name used to make jobs for one person run one at a time. While one job for a key is `in_flight` or `uncertain`, no other job for that key is sent. Instagram and Threads both default to `meta:HANDLE`, so one person's Meta posts never overlap. Older `instagram:` and `threads:` keys are read as `meta:`. Other platforms use `PLATFORM:HANDLE`. |
| **Register** | Record a session in the account registry, with `login start` (automatic) or `accounts register` (manual). |
| **State dir** | The private folder that holds sessions, the registry, the inbox, the outbox and platform files. Default `~/.batbox/social-farm-state`. Set `SOCIAL_FARM_STATE_DIR` to move it. `ghost-in-the-box config` prints it. See [configuration](configuration.md). |
| **Worker** | A name you choose (for example `review-bot`) when claiming inbox comments. Claims last one hour, so two reviewers do not answer the same comment. |

## Platform plumbing

| Term | Plain meaning |
| :--- | :--- |
| **Adapter** | The code that sends one job to one platform and reports published, failed or uncertain (`farm/adapters/`). |
| **Transport** | How Threads is reached: a portable cookie session in Playwright, Scrapling, the mobile API, or the Node browser bridge. `threads --account HANDLE status` shows which one is used and why. |
| **Extra** | An optional install group, such as `.[login]`. See the table in the [README](../README.md#what-each-extra-unlocks). |
| **Error envelope** | The JSON shape of every failure: `{"ok": false, "error": {"code", "message", "hint"}}`. See [configuration](configuration.md#errors-and-exit-codes). |
