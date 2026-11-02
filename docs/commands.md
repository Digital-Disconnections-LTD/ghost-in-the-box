# Every command (command index)

One line per command and subcommand, grouped the same way as
`ghost-in-the-box --help`. For full options run `ghost-in-the-box COMMAND --help`
or `ghost-in-the-box help COMMAND`. A typo gets a "did you mean" suggestion.
Terms are explained in [plain terms](glossary.md).

Commands print JSON results on stdout. `doctor` and `config` print text unless
you add `--json`, `brief` prints a text receipt, and `instagram-mcp --list-tools
--format md` prints Markdown. Exit codes and the
error format are in [configuration](configuration.md#errors-and-exit-codes).

`HANDLE` is a public account name without `@`. `JOB` is an outbox job ID.

## Top level

| Command | What it does |
| :--- | :--- |
| `ghost-in-the-box --help` | List every command by group. |
| `ghost-in-the-box --version` | Print the installed version. |
| `ghost-in-the-box help COMMAND` | Same as `COMMAND --help`. |

## Setup

| Command | What it does |
| :--- | :--- |
| `doctor` | Check Python, extras, browsers, the state dir, registered sessions and stuck outbox jobs. Local files only. |
| `doctor --json` | The full doctor report as JSON. |
| `doctor --strict` | Exit 1 on warnings too, not only on problems. |
| `config` / `config show` | Print the state dir, every path and every `SOCIAL_FARM_*` setting. |
| `config --json` | The same report as JSON. |
| `login` | Start the login wizard (same as `login start`). |
| `login start [--platform P]... [--account HANDLE] [--publication URL] [--browser auto\|chrome\|msedge\|brave\|chromium]` | Open your installed browser on a profile under the state dir for each chosen platform, read and confirm the signed-in handle, verify it, and register the session. `--account` needs exactly one `--platform` and skips the handle detection. |
| `login forget-browser --platform P [--yes]` | Delete that platform's login browser profile after asking. Instagram and Threads share one. Saved sessions stay. |
| `login list` | List registered accounts without reading their sessions. |
| `login export --account PLATFORM:HANDLE... --output FILE [--replace]` | Write the chosen sessions to one private bundle file. |
| `login import BUNDLE [--replace]` | Import a bundle and rewrite its paths for this machine. All or nothing. |
| `accounts list` | List every registration. |
| `accounts show --platform P --account HANDLE` | Show one registration and whether its session file is usable. Positional `PLATFORM HANDLE` also works. |
| `accounts register --platform P --account HANDLE --kind KIND --session-file PATH [--account-key KEY]` | Record where a saved session lives. `KIND` is `instagrapi_settings`, `browser_profile`, `oauth_file` or `cookie_jar`. |
| `accounts --registry PATH ...` | Use another registry file for one command. |

## Write and publish

| Command | What it does |
| :--- | :--- |
| `brief --json PACK [--out DIR] [--emit]` | Turn a topic pack into a cited brief. `--emit` also writes one `.body` file per lane and prints the draft commands. Output goes to `./out/briefs` under the current folder unless you pass `--out`. `brief --help` lists every topic-pack field; [examples/topic-pack.json](../examples/topic-pack.json) is a working sample. |
| `brief --title T --lane L [--source LABEL=URL] [--why ...] [--draft LANE=TEXT] ...` | Build a brief from flags instead of a JSON file. |
| `outbox draft --platform P --account HANDLE --body TEXT\|--body-file FILE [--action post\|reply] [--media F] [--target-id ID --target-url URL] [--source-url URL] [--idempotency-key K]` | Create a draft job. Sends nothing. `--body-file -` reads stdin. Platforms: `instagram`, `threads`, `linkedin`. Facebook, TikTok and YouTube are refused. |
| `outbox queue JOB [--at ISO-TIME]` | Freeze a draft's text and media and schedule it (default now). |
| `outbox list [--status S] [--limit N]` | Show active jobs (draft, queued, in flight, uncertain). `--status all` shows every job. |
| `outbox show JOB` | Show one job in full, including its text. |
| `outbox cancel JOB [--reason TEXT]` | Cancel a draft or queued job before any publisher claims it. |
| `outbox resolve JOB --outcome published\|queued\|failed --reason TEXT [--provider-id ID] [--permalink URL]` | Record what really happened to an uncertain job. |
| `outbox retry-failed JOB --reason TEXT` | Requeue a job that failed before sending or was clearly refused. Refused for jobs that may have been sent. |
| `outbox supersede-failed JOB --reason TEXT` | Retire a failed job but keep its record. |
| `outbox claim [--platform P] [--lease-seconds N]` | Low level: claim one due job for your own publisher. |
| `outbox finish JOB --lease-token T --outcome published\|uncertain\|failed [--provider-id ID] [--permalink URL] [--error TEXT]` | Low level: record your publisher's result for a claimed job. |
| `outbox --db PATH ...` | Use another outbox file for one command (put `--db` before the subcommand). |
| `publish JOB` | Send one queued job once it is due, then record its receipt. This is a real post. |
| `publish JOB --now` | Send a queued job even if it is scheduled for later. |
| `publish --dry-run JOB` | Show the frozen payload, target account and blockers. Sends nothing. |
| `publish --due [--platform P] [--account HANDLE] [--limit N]` | Claim and send every due job (at most N, 1 to 50). This is what the timers run. |
| `carousel draft --account HANDLE --body-file FILE --image IMG... [--crosspost-threads] [--source-url URL] [--idempotency-key K]` | Freeze 2 to 10 images into one Instagram carousel draft. |
| `memes [--root DIR] collect [--count N] [--pages N] [--query Q]` | Add new images from public meme sources, run OCR and refresh topic folders. |
| `memes organize [--redo-ocr]` | Refresh topic folders, OCR and indexes offline. |
| `memes search [QUERY] [--category C] [--limit N]` | Find saved images and return their paths and sources. |
| `memes prepare-carousel ID... --out DIR` | Pad 2 to 10 images into ordered 1080x1350 JPEG slides. |
| `memes import-local IMAGE [--source-url URL] [--title T] [--author A]` | Add a downloaded image with its source details. |
| `vision --image IMG [--question Q] [--endpoint URL] [--model M]` | Ask your configured vision model about one image. The image goes to that endpoint. |

## Instagram

| Command | What it does |
| :--- | :--- |
| `instagram-login [--credentials FILE] [--verification-code-stdin]` | Log in with a username and password file and save an Instagram session. Prints the `accounts register` command to run next. |
| `instagram-import import --account HANDLE [--pages N] [--older]` | Import your own captions as voice context. Needs a registered session. |
| `instagram-import status\|list\|show --account HANDLE` | Inspect the imported caption corpus. |
| `instagram-inbox --account HANDLE sync [--posts N] [--pages N] [--threads N] [--feed-pages N] [--older]` | Read comments on your newest posts (and optionally the feed) into the inbox. |
| `instagram-inbox --account HANDLE peek` | Show unread comments without claiming them. |
| `instagram-inbox --account HANDLE unseen [--worker W]` | Claim unread comments into your review queue. They stop being unread. |
| `instagram-inbox --account HANDLE pending [--worker W]` | Show claimed comments still waiting for a reply or a skip. |
| `instagram-inbox --account HANDLE feed` | Show timeline items saved by `sync --feed-pages`. |
| `instagram-inbox --account HANDLE draft-reply --comment-id ID --body TEXT\|--body-file FILE [--worker W]` | Save a reply to one comment as an outbox draft. Nothing is sent. |
| `instagram-inbox --account HANDLE seen --comment-id ID` | Mark one comment as read. |
| `instagram-inbox --account HANDLE skip --comment-id ID [--worker W]` | Mark one claimed comment as needing no reply. |
| `instagram-mcp [--account HANDLE]` | Run the Instagram MCP server over stdio. See [MCP tools](mcp.md). |
| `instagram-mcp --list-tools [--format json\|md\|mcp]` | Print the tool catalog without an account or Instagram. |

## Threads

All `threads` subcommands take `--account HANDLE` before the subcommand.

| Command | What it does |
| :--- | :--- |
| `threads --account HANDLE status` | Show registration, chosen transport and outbox counts. No network request. |
| `threads --account HANDLE check` | Ask Threads who each saved session belongs to. Posts nothing. |
| `threads --account HANDLE connect [--refresh]` | Reuse a wizard login, or link a saved Instagram session to a verified browser profile. |
| `threads --account HANDLE mobile-login [--native] [--verification-code-stdin] [--force-refresh]` | Mint the Threads mobile token from the Instagram password file. |
| `threads --account HANDLE read me\|own\|replies\|target\|search\|feed [--limit N] [--target-id ID] [--target-url URL] [--query Q]` | Read a limited number of items through the chosen transport. |
| `threads --account HANDLE post --body TEXT\|--body-file FILE [--now]` | Queue a post. `--now` publishes it immediately. `--body-file -` reads stdin. |
| `threads --account HANDLE reply --target-id ID --target-url URL --body TEXT\|--body-file FILE [--now]` | Queue a reply. `--now` publishes it immediately. |
| `threads --account HANDLE reply-inbox --comment-id ID --body TEXT\|--body-file FILE [--worker W] [--now]` | Queue a reply to a claimed inbox comment. |
| `threads --account HANDLE publish JOB` | Publish one queued job now, respecting quiet hours and post spacing. |
| `threads --account HANDLE job JOB` | Show one outbox job. Read only. |
| `threads --account HANDLE reconcile JOB` | Confirm an uncertain job from the platform's receipt. Never resends. |
| `threads --account HANDLE retry JOB` | Requeue and publish a job that failed before anything was sent. |
| `threads-setup --account HANDLE` | Install the optional pinned Node browser bridge in the state dir. |
| `threads-notifications --account HANDLE sync [--posts N] [--replies-per-post N] [--notify-agent NAME]` | Read recent replies to your posts into the inbox. |
| `threads-notifications --account HANDLE peek` | Show waiting comments without claiming them. |
| `threads-notifications --account HANDLE unseen [--worker W]` | Claim and show new comments for one worker. |
| `threads-notifications --account HANDLE pending [--worker W]` | Show comments a worker claimed and has not answered. |
| `threads-notifications --account HANDLE skip --comment-id ID --worker W` | Mark a claimed comment as needing no reply. |
| `threads-notifications --account HANDLE draft-reply --comment-id ID --worker W --body TEXT\|--body-file FILE` | Save a reply draft for a claimed comment. Nothing is sent. |

## LinkedIn

All `linkedin` subcommands take `--account HANDLE` before the subcommand. They
only read; posting goes through the outbox.

| Command | What it does |
| :--- | :--- |
| `linkedin --account HANDLE me` | Check who the saved session belongs to. |
| `linkedin --account HANDLE profile PUBLIC_HANDLE` | Read one profile. |
| `linkedin --account HANDLE posts [--limit N] [--start N]` | Read your own posts. |
| `linkedin --account HANDLE conversations [--limit N] [--start N]` | Read your conversation list. |
| `linkedin --account HANDLE messages CONVERSATION_ID [--limit N] [--start N]` | Read messages in one conversation. |

## Substack and MCP

| Command | What it does |
| :--- | :--- |
| `substack-mcp [--account HANDLE]` | Run the Substack MCP server. Needs the `substack` extra. |
| `mcp-bridge -- COMMAND [ARGS...]` | Connect a Content-Length framed client (Batbox) to a newline-JSON server. |

## Timer scripts

These are not subcommands. The [systemd units](configuration.md#optional-timers-systemd-linux)
run them by path.

| Script | What it does |
| :--- | :--- |
| `scripts/ig_publish_once.py [--db PATH]` | Same as `publish --due --platform instagram --limit 1`. |
| `scripts/threads_publish_once.py --account HANDLE [--db PATH]` | Same path as `publish --due --platform threads --account HANDLE --limit 1`, plus quiet hours, post spacing and auth cooldown. |
