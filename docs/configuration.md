# A comfortable place for your state (configuration)

Ghost in the Box keeps everything in one private folder, the state dir. This page
covers where it is, what is inside, every setting, exit codes, the error format
and the optional systemd timers.

## The state dir

The default is `~/.batbox/social-farm-state/`. Set `SOCIAL_FARM_STATE_DIR` to
choose another folder. A per-file setting or command flag (such as `--db`) wins
over the state dir. The folder is created the first time a command writes.

```sh
ghost-in-the-box config          # human-readable
ghost-in-the-box config --json   # the same report as JSON
```

`config` prints the effective state dir, every path inside it (with `missing`
for files that do not exist yet) and every `SOCIAL_FARM_*` setting, marked `set`
or `default`. A `SOCIAL_FARM_*` variable that Ghost in the Box does not know is
flagged so you can catch typos. Passwords inside URL values are masked.

The `SOCIAL_FARM_*` names and the `.batbox` folder come from the project's older
name and are kept so existing installs keep their sessions. See
[plain terms](glossary.md).

### What is inside

| Path under the state dir | Holds |
| :--- | :--- |
| `accounts.json` | The account registry: platform, handle, account key and session path. |
| `outbox.sqlite3` | Every draft, queued, sent, failed and uncertain job. |
| `inbox.sqlite3` | Comments, replies and feed items that were read, plus notification batches. |
| `ig/HANDLE.json` | Instagram sessions (instagrapi settings). |
| `linkedin/HANDLE.json`, `threads/HANDLE.json` | Cookie-jar sessions saved by the login wizard. |
| `substack/HANDLE.json`, `substack/HANDLE-cookies.json` | Substack connection and cookies saved by the login wizard. |
| `substack/connection.json` | Default Substack connection file when you set one up by hand. |
| `ig-corpus/HANDLE/` | Your own captions imported by `instagram-import`. |
| `instagram-references/` | Saved research posts and downloaded images from the MCP research tools. |
| `carousels/` | Frozen carousel slides. |
| `threads-work/` | Threads agent working files, including the `threadspost` post marker. |
| `threads-auth/` | Threads mobile bearer tokens. |
| `threads-cadence/` | Per-account Threads post spacing and auth cooldown. |
| `threads-portable/` | Portable Threads browser transport state. |
| `threads-scrapling/` | Scrapling Threads transport state and profile. |
| `threads-browser-fallback/` | Node browser bridge state; its `source/` holds the pinned Threads MCP build. |
| `login-profiles/meta/` | The login wizard's browser profile for Instagram and Threads, which share one Meta login. Holds a signed-in browser session; treat it as a credential. |
| `login-profiles/linkedin/`, `login-profiles/substack/` | The login wizard's browser profiles for LinkedIn and Substack, one each. Delete one with `login forget-browser --platform P`. |

Two locations stay at their historical place when you use the default state dir,
and move inside it only when you set `SOCIAL_FARM_STATE_DIR`:

| What | Default state dir | Custom state dir |
| :--- | :--- | :--- |
| Instagram password file for `instagram-login` | `~/.batbox/social-farm-creds/ig.json` | `<state>/credentials/ig.json` |
| Meme library (visual reference shelf) | `~/.batbox/media-reference/tech-memes/` | `<state>/media-reference/tech-memes/` |

If you set a custom state dir but a file already exists at the old place, the old
place keeps being used so nothing is lost. `config` shows which one applies.

The Threads post marker moved from `$HOME/threadspost` to
`<state>/threads-work/threadspost`. Nothing is written loose in your home folder
any more.

Account sessions, cookies, draft databases, media downloads and receipts are
private. Ghost in the Box writes its session files atomically with owner-only
permissions (an owner-only ACL on Windows). Keep the state dir and any
credentials out of Git.

## Settings

Every setting is optional.

| Setting | What it sets | Default | Used by |
| :--- | :--- | :--- | :--- |
| `SOCIAL_FARM_STATE_DIR` | The state dir. | `~/.batbox/social-farm-state` | every command |
| `SOCIAL_FARM_ACCOUNTS_FILE` | The account registry file. | `<state>/accounts.json` | `accounts`, `login`, `publish` |
| `SOCIAL_FARM_OUTBOX_DB` | The outbox database. | `<state>/outbox.sqlite3` | `outbox`, `publish` |
| `SOCIAL_FARM_SUBSTACK_CONFIG` | A Substack connection file. | `<state>/substack/connection.json` | `substack-mcp` |
| `SOCIAL_FARM_IG_CREDENTIALS` | The Instagram username and password JSON. | see the table above | `instagram-login`, `threads` |
| `SOCIAL_FARM_MEMES_DIR` | The meme library folder. | see the table above | `memes` |
| `SOCIAL_FARM_THREADS_WORK_DIR` | Threads agent working files. | `<state>/threads-work` | `threads` |
| `SOCIAL_FARM_THREADS_MCP_SOURCE` | A built Threads MCP source checkout for the Node bridge. | `<state>/threads-browser-fallback/source` | `threads`, `threads-setup` |
| `SOCIAL_FARM_NODE` | The Node.js executable for the Threads browser bridges. | `node` on PATH | `threads`, `threads-setup` |
| `SOCIAL_FARM_SCRAPLING_PYTHON` | The Python interpreter that has Scrapling installed. | the running Python | `threads` |
| `SOCIAL_FARM_THREADS_SCRAPLING_STATE` | Scrapling bridge state. Set internally for subprocesses; you rarely need it. | `<state>/threads-scrapling` | `threads` |
| `SOCIAL_FARM_LOGIN_BROWSER` | The browser the login wizard opens: `auto`, `chrome`, `msedge`, `brave` or `chromium`. `login start --browser` wins over it. | `auto` (Chrome, Edge, Brave, Chromium, then Playwright Chromium) | `login` |
| `SOCIAL_FARM_IG_ACCOUNT` | Default account for the Instagram MCP server. | none | `instagram-mcp` |
| `SOCIAL_FARM_IG_PYTHON` | The Python interpreter that has instagrapi installed. | the running Python | `instagram-mcp`, `publish` |
| `SOCIAL_FARM_LINKEDIN_ACCOUNT` | Default account for the LinkedIn MCP server. | none | `linkedin-mcp` |
| `SOCIAL_FARM_VISION_URL` | An OpenAI-compatible vision endpoint. | `http://127.0.0.1:8001/v1/chat/completions` | `vision`, the `instagram_inspect_slide` tool |
| `SOCIAL_FARM_VISION_MODEL` | The vision model name. | `your-vision-model` | `vision`, the `instagram_inspect_slide` tool |
| `SOCIAL_FARM_CHAT_PATH` | A script that delivers Threads notification batches (see below). | none | `threads-notifications` |
| `SOCIAL_FARM_THREADS_NOTIFY_AGENT` | Default `--notify-agent` recipient for `threads-notifications sync`. | none | `threads-notifications` |
| `SOCIAL_FARM_ACCOUNT` | The Threads account for the Threads publish systemd unit. Only systemd reads it. | none | systemd units |
| `SOCIAL_FARM_LINKEDIN_SHARE_QUERY_ID` | The GraphQL query id LinkedIn's web client uses to create a post. LinkedIn rotates it; set the full `voyagerContentcreationDashShares.<hash>` or just the 32-character hash. | `voyagerContentcreationDashShares.279996efa5064c01775d5aff003d9377` | `publish` |

`ghost-in-the-box config` prints this same list with your current values.

Image inspection sends the selected image to the endpoint you configure. Meme OCR
uses an optional `tesseract` executable.

## Errors and exit codes

Every command prints its result as one JSON document on stdout. A failure looks
like this:

```json
{"ok": false, "error": {"code": "account_not_registered", "message": "instagram:nobody is not registered in /path/to/accounts.json", "hint": "ghost-in-the-box login start --platform instagram --account nobody"}}
```

- `code` is stable and safe to match in scripts.
- `message` explains what went wrong.
- `hint` is a concrete next step, usually the exact command to run. When there is
  nothing more specific, it is `ghost-in-the-box doctor`.

Some commands also keep older top-level keys such as `status` and `error_type`
for scripts written before this format. A mistyped flag or missing argument is
the exception: argparse prints its usage message on stderr (not JSON) and exits
with code 2. Note that `error` is now an object
where it used to be a string in `accounts` and `outbox` output.

| Exit code | Meaning |
| :--- | :--- |
| `0` | Success. |
| `1` | Runtime failure (for example a database or file error). For `doctor`, problems were found. |
| `2` | Usage error or refused request (bad flags, unknown job, a job in the wrong state). |
| `3` | Uncertain send. The post may exist on the platform. Check it, then run `outbox resolve`. |
| `4` | Login or dependency problem: no registered session, a session the platform rejected, or a missing extra. The hint names the login or install command. |
| `130` | You cancelled the login wizard (Ctrl+C). |

A missing extra prints the exact `pip install` command for your install, and a
publish job that hits a missing extra stays queued.

## Notification delivery

Threads inbox updates are durable local records. Without `SOCIAL_FARM_CHAT_PATH`,
`threads-notifications sync --notify-agent RECIPIENT` returns the pending batch for
you to inspect. With it set, Ghost in the Box runs
`python CHAT_PATH send-agent RECIPIENT CONTENT --id BATCH_ID`. The script must
print JSON containing the same `id` and `"durable": true` before the batch is
marked sent. Other coordinators can read the inbox directly.

`threads-notifications sync` stays quiet from 23:55 to 06:00
`America/New_York`. Reading the local inbox works at any time.

## Optional timers (systemd, Linux)

The `systemd/` folder has user units. They assume a checkout at
`~/ghost-in-the-box` with its `.venv/` inside. Nothing is installed or started by
`pip install`. Read each unit's command, account and schedule before you enable it.

| Unit | What it runs | Schedule |
| :--- | :--- | :--- |
| `ghost-in-the-box-ig-publish.timer` | Publishes at most one due Instagram job. | every 2 minutes |
| `ghost-in-the-box-threads-publish.timer` | Publishes at most one due Threads job for `SOCIAL_FARM_ACCOUNT`. | every 2 minutes |
| `ghost-in-the-box-ig-inbox@HANDLE.timer` | Reads comments and feed into the inbox. Read only. | every 15 minutes |
| `ghost-in-the-box-ig-corpus@HANDLE.timer` | Imports your newest captions. Read only. | every 4 hours |
| `ghost-in-the-box-threads-notifications@HANDLE.timer` | Reads Threads replies into the inbox. Read only. | about every 12 to 20 minutes |

The two publish timers run `scripts/ig_publish_once.py` and
`scripts/threads_publish_once.py`. These are now thin wrappers around the same
code as `ghost-in-the-box publish --due --platform instagram --limit 1` and
`publish --due --platform threads --account HANDLE --limit 1`. The Threads one
also applies quiet hours (midnight to 06:00 New York), a random 1 to 2 hour gap
between new posts, and a cooldown after an auth failure. Replies go before posts.

Only jobs you queued are published. A job whose scheduled time is still in the
future waits.

To install, from the checkout:

```sh
mkdir -p ~/.config/systemd/user ~/.config/ghost-in-the-box
cp systemd/ghost-in-the-box-ig-publish.* systemd/ghost-in-the-box-ig-inbox@.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ghost-in-the-box-ig-publish.timer
systemctl --user enable --now ghost-in-the-box-ig-inbox@YOUR_HANDLE.timer
```

Template units (names with `@`) take your handle after the `@`. The Threads
publisher reads its account from `~/.config/ghost-in-the-box/runtime.env`, which
you create:

```sh
printf 'SOCIAL_FARM_ACCOUNT=YOUR_HANDLE\n' > ~/.config/ghost-in-the-box/runtime.env
chmod 600 ~/.config/ghost-in-the-box/runtime.env
cp systemd/ghost-in-the-box-threads-publish.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ghost-in-the-box-threads-publish.timer
```

The same file can hold other `SOCIAL_FARM_*` settings for the Threads units. No
personal account is bundled with the project.

Check and stop timers:

```sh
systemctl --user list-timers 'ghost-in-the-box*'
journalctl --user -u ghost-in-the-box-ig-publish.service -n 20
systemctl --user disable --now ghost-in-the-box-ig-publish.timer
```

User timers stop when you log out unless lingering is on:
`loginctl enable-linger "$USER"`.
