# Plant your first seed (getting started)

This page takes you from install to a first queued post, then to a real account.
Unfamiliar words are in [plain terms](glossary.md). Every command is in the
[command index](commands.md).

> **Research use only.** This code is for research purposes only. Follow the rules: use it only with accounts you own, respect each platform's terms of service and rate limits, and take responsibility for anything you publish.

## Install

Use Python 3.11 or newer (`python3 --version`) and Git.

```sh
git clone https://github.com/Digital-Disconnections-LTD/ghost-in-the-box.git
cd ghost-in-the-box
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[recommended]'
python -m playwright install chromium
```

The second command downloads a Chromium browser (about 150 MB) for the login
wizard. The [README](../README.md#what-each-extra-unlocks) lists what each extra
adds. On Windows, see [login and transfer](login.md#install) for the PowerShell
version. The `ghost-in-the-box` command is on your PATH while the virtual
environment is active.

## Check your setup with doctor

```sh
ghost-in-the-box doctor
```

`doctor` reads local files only. It never opens a session or contacts a platform.
It prints one line per check:

- `ok` means the check passed.
- `WARN` is worth fixing. Each one is followed by a `next:` line with the exact
  command to run.
- `--` is an optional feature you have not installed. Ignore it unless you want
  that feature.

On a first run, `no accounts registered yet` is a normal warning. `doctor` exits
0 when nothing is broken and 1 when it found problems. Add `--strict` to treat
warnings as problems, or `--json` for the full report.

### Where your files live

Everything lives in one private folder, the state dir. The default is
`~/.batbox/social-farm-state`. The folder is created the first time a command
writes to it. To see the exact path, every file inside it and every setting:

```sh
ghost-in-the-box config
```

Set `SOCIAL_FARM_STATE_DIR` to use another folder. See
[configuration](configuration.md) for the full layout.

## Try it without an account

This walk-through uses only local files. Nothing logs in and nothing is sent.

**1. Make a brief.** A brief turns a topic pack (a small JSON file with your
topic, sources and target platforms) into a cited plan and one body file per
platform. Look at [examples/topic-pack.json](../examples/topic-pack.json) for a
working sample; `ghost-in-the-box brief --help` describes each field. Without
`--out`, brief files go to `./out/briefs` under the folder you run it from.

```sh
ghost-in-the-box brief --json examples/topic-pack.json --out out/demo --emit
```

It prints a receipt and then ready-to-run draft commands, like this (the date in
the file names is today's date in UTC):

```text
After reviewing a body file, save it as a local draft (nothing is sent):
  ghost-in-the-box outbox draft --platform threads --account YOUR_HANDLE --body-file out/demo/20261101-tools-you-can-own.threads.body --source-url https://digitaldisconnections.com/our-story.html
  ghost-in-the-box outbox draft --platform instagram --account YOUR_HANDLE --body-file out/demo/20261101-tools-you-can-own.instagram.body --media YOUR_IMAGE.jpg --source-url https://digitaldisconnections.com/our-story.html
    (Instagram needs an image; for 2-10 slides use: ghost-in-the-box carousel draft --help)
Then find its JOB_ID, preview exactly what would be sent (sends nothing), and queue only the draft you chose:
  ghost-in-the-box outbox list
  ghost-in-the-box publish --dry-run JOB_ID
  ghost-in-the-box outbox queue JOB_ID
```

**2. Draft.** Read the `.threads.body` file, then copy and run the printed
`--platform threads` line. Offline, `YOUR_HANDLE` can stay as it is: the outbox
accepts any account name, and only publishing needs a real registered account.
The result is JSON with the new job's `id`. Use that ID as `JOB_ID` below.

```sh
ghost-in-the-box outbox list
ghost-in-the-box outbox show JOB_ID
```

**3. Queue.** Queueing freezes the exact text (and any media) and schedules it.
After this, the words cannot change.

```sh
ghost-in-the-box outbox queue JOB_ID
```

**4. Preview the send.**

```sh
ghost-in-the-box publish --dry-run JOB_ID
```

This prints the frozen text, the target account and a `blockers` list. Here it
shows `account_not_registered`, because `YOUR_HANDLE` is not a real account. It
always reports `"network_requests": 0`.

`ghost-in-the-box publish JOB_ID` is the real send. With a registered account it
posts that exact job once and records the platform's receipt. Do not run it for
this demo.

**5. Clean up.** Cancel the demo job so no publisher ever picks it up:

```sh
ghost-in-the-box outbox cancel JOB_ID --reason "offline demo"
```

The demo draft lives in your normal outbox under the state dir. The brief files
are under `out/`, which Git ignores in this checkout.

## Connect your own account

### The login wizard (recommended)

The wizard opens a visible browser. You sign in yourself, with your usual
password, passkey or two-factor step. It then checks who you are and registers
the session. It supports LinkedIn, Instagram, Threads and Substack.

```sh
ghost-in-the-box login
```

It shows a numbered menu of the four platforms and which ones are already logged
in. Type a number (or `1 3` for several), sign in, and it brings you back to the
menu. Type `d` or press Enter when you are done.

Or choose one account directly:

```sh
ghost-in-the-box login start --platform instagram --account YOUR_HANDLE
ghost-in-the-box accounts list
ghost-in-the-box accounts show --platform instagram --account YOUR_HANDLE
```

[Login and transfer](login.md) covers every option, Windows, and moving sessions
to a server.

### Manual Instagram session (alternative)

Use this only if the wizard does not work for you. It needs your Instagram
password in a file.

1. Run `ghost-in-the-box config` and find the `instagram credentials` path. That
   is where `instagram-login` looks by default.
2. Put your own `{"username": "...", "password": "..."}` JSON there, or in any
   private file outside the checkout. Make it readable only by you
   (`chmod 600 FILE` on Linux and macOS).
3. Log in:

   ```sh
   ghost-in-the-box instagram-login --credentials /absolute/private/instagram-login.json
   ```

   If Instagram asks for a code, add `--verification-code-stdin` and type the
   current code into stdin. That keeps it out of your shell history.
4. The result prints the saved session path and a `next` command. Run it. It
   looks like this:

   ```sh
   ghost-in-the-box accounts register --platform instagram --account YOUR_HANDLE \
     --kind instagrapi_settings --session-file /absolute/private/instagram.json
   ghost-in-the-box accounts show --platform instagram --account YOUR_HANDLE
   ```

The registry stores the path to the session, not a copy of it. Instagram and
Threads accounts share the account key `meta:YOUR_HANDLE` by default, so one
person's Meta posts never go out at the same time. Platform challenges may need
you to recover the account in the app before another attempt.

### Which commands need a registered session

| No session needed | Needs a registered session for that account |
| :--- | :--- |
| `doctor`, `config`, `help`, `--version` | `publish JOB` and `publish --due` |
| `brief` | `instagram-import` |
| `outbox` (every subcommand) | `instagram-inbox sync` |
| `publish --dry-run` | `instagram-mcp` server start (an `--account` you name, or one registered Instagram account) and its tools that reach Instagram |
| `accounts`, `login list` | `threads` (every subcommand except `status`) |
| `instagram-inbox` and `threads-notifications` reads of the local inbox (`peek`, `pending`, `unseen`, `skip`, `draft-reply`) | `threads-notifications sync` |
| `threads status` | `linkedin` (every subcommand) |
| `instagram-mcp --list-tools` | `substack-mcp` (the wizard writes its connection file) |
| `memes`, `vision` (they contact public meme sites or your vision endpoint, not your accounts) | |

When a command needs a session you do not have, it fails with exit code 4 and a
`hint` naming the `login start` command to run.

## Read, draft, queue, deliver

With an Instagram session registered:

```sh
ghost-in-the-box instagram-import import --account YOUR_HANDLE --pages 1
ghost-in-the-box instagram-inbox --account YOUR_HANDLE sync --posts 3 --pages 1
```

The first imports your own captions as voice context. The second reads comments
on your three newest posts into the local inbox. Both read a limited number of
pages.

Then follow the [agent workflow](agent-workflow.md) to turn what you read into a
draft, queue the chosen job, and publish that exact ID. Commands never wait for
input; if a session needs a new login, they stop with a hint instead.

## For development

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m build
```

The offline suite does not prove a live platform connection. Optional browser
and Substack dependencies are described in [connections](connectors.md).
