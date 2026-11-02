<div align="center">
  <img src="docs/assets/ghost-in-the-box-garden.png" alt="Ghost in the Box: a friendly glowing ghost grows conversations in a midnight garden." width="100%">
  <h1>Ghost in the Box</h1>
  <p><strong>Your accounts. Your voice. Your garden.</strong></p>
  <p>A local social engine for thoughtful drafts, useful conversations, and publishing with receipts.</p>
  <p><a href="docs/getting-started.md">Plant your first seed (getting started)</a> · <a href="#browser-guided-logins">Browser-guided logins</a> · <a href="docs/commands.md">Every command</a> · <a href="docs/glossary.md">Plain terms</a> · <a href="docs/agent-workflow.md">Bring your Bats (agent workflow)</a> · <a href="CONTRIBUTING.md">Grow with us (contributing)</a> · <a href="docs/social-engine-plan.md">The next season (roadmap)</a></p>
  <p><strong>PYTHON 3.11+ · LINUX · macOS · WINDOWS (LOGIN AND SESSIONS) · MIT · COMMUNITY PROJECT</strong></p>
</div>

---

## Welcome to the garden 🌱 (what this is)

Good conversations need more than a publish button. Ghost in the Box connects the
work around it: understand the conversation, keep the sources, develop a draft,
choose the account, send the exact job, and remember what happened.

Built by [Digital Disconnections](https://digitaldisconnections.com/) for people
who want to own their tools and keep control of their work. Read our
[mission statement, It's Not 1997](https://digitaldisconnections.com/our-story.html).

New words such as brief, lane, frozen job or Bat are explained in
[plain terms](docs/glossary.md).

| In the garden | What it does |
| :--- | :--- |
| **Know your accounts** | Keep local references to your own saved sessions. |
| **Listen first** | Read a limited number of posts, comments, replies, and feed items into a local inbox. |
| **Keep your voice** | Import your own writing and build source-linked briefs. |
| **Make something worth sharing** | Prepare text, carousels, and visual research. |
| **Publish with receipts** | Freeze queued content, claim one job, record its provider ID and link. |
| **Recover honestly** | Keep uncertain sends visible for reconciliation instead of blindly repeating them. |

## Pick a plot (supported platforms)

| Connection | Available today |
| :--- | :--- |
| **Instagram** | Session registry, own-writing importer, inbox, comments, carousel drafts, exact-job publishing, and MCP tools. |
| **Threads** | Post and reply readers, inbox triage, queued posts and replies, cadence (post spacing and quiet hours), and publishing adapters. Account setup and provider authentication are required. Browser helpers are optional. |
| **Substack** | Optional SDK-backed MCP connector, persistent cookies, setup status, and publication receipts. |
| **LinkedIn** | Browser login, reads of your identity, a profile, your own posts, your conversation list and messages, and experimental text-post delivery through the outbox. |
| **Brief engine** | Source-linked briefs and draft files for Instagram, Threads, Substack, LinkedIn, Facebook, TikTok, and YouTube. |

Facebook, TikTok, and YouTube currently have **brief formats**, not
built-in publishing adapters. The outbox refuses them, so you post those yourself. Provider access changes, so mocked tests establish
local behavior rather than a guarantee that your account is connected.

## Plant your first seed (install and first run)

You need Python 3.11 or newer (`python3 --version`) and Git.

```sh
git clone https://github.com/Digital-Disconnections-LTD/ghost-in-the-box.git
cd ghost-in-the-box
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[recommended]'
python -m playwright install chromium
ghost-in-the-box doctor
```

`doctor` checks this machine and prints the exact command for anything missing.
It reads local files only. `ghost-in-the-box --help` lists every command by
group, `ghost-in-the-box config` shows where your files live, and
`ghost-in-the-box --version` prints the installed version. The
[command index](docs/commands.md) has one line per command.

Try the [offline example](docs/getting-started.md#try-it-without-an-account) before
connecting anything. It builds a brief, drafts and queues a post in a local outbox,
and previews the send with `publish --dry-run`. Nothing logs in and nothing is sent.

Then connect your chosen accounts with the [login wizard](docs/login.md) and use
the same `ghost-in-the-box` command for accounts, briefs, inboxes, and delivery.

### What each extra unlocks

The base install only needs Requests. Extras add optional features.

| Extra | Installs | Unlocks |
| :--- | :--- | :--- |
| `recommended` | `login` + `instagram` + `media` | The quickstart above. Most people want this. |
| `login` | Playwright | The browser login wizard (`login start`) for every platform, and the portable Threads transport. Run `python -m playwright install chromium` once after installing. |
| `instagram` | instagrapi, Pillow | Instagram login, inbox, import, carousels, publishing and the `instagram-mcp` tools that reach Instagram. |
| `media` | Pillow | Image checks for carousels and the meme library. |
| `browser` | Scrapling | The optional Scrapling transport for Threads reads and posts. |
| `substack` | python-substack (with MCP) | Substack login and the `substack-mcp` server. |
| `dev` | test and build tools | Running the test suite and building the package. |

Combine extras with commas, for example `'.[recommended,substack]'`.
LinkedIn reads and posts need only the base install once a session exists.

### Where it runs

| Area | Linux | macOS | Native Windows |
| :--- | :--- | :--- | :--- |
| Login wizard, session export and import, account registry | tested | tested | tested |
| LinkedIn reads, portable Threads transport | tested | tested | tested |
| Everything else (outbox, publish, inbox, MCP, brief, memes) | tested | tested | not tested in CI; use WSL |
| systemd timer examples, `xvfb-run` for headless browsers | yes | no | no |

"Tested" means the offline test suite runs there in CI. Mocked tests prove local
behaviour, not that a platform will accept your account today.

## Browser-guided logins

`ghost-in-the-box login start` opens the Chrome you already have (or Edge, Brave
or Chromium). It starts it as a normal program with its own profile folder under
the state dir, so you sign in the way you always do. Google sign-in works too.

A numbered menu shows which accounts are already connected. Pick one, sign in,
and the menu comes back until you say done. Instagram and Threads share one Meta
login, so after one you are offered the other with no second sign-in. You never
type your handle: the wizard reads it from the session and asks you to confirm.

What gets saved is a cookie session in files private to you. Nothing leaves your
machine unless you export it. `login forget-browser --platform PLATFORM` wipes a
browser profile, and `login export` and `login import` move the sessions you pick
to a server. Every step is in the [login guide](docs/login.md).

## Bring a Bat, or your favorite MCP client 👻 (agents and MCP)

Ghost in the Box works alongside [Batbox](https://github.com/Digital-Disconnections-LTD/batbox),
Claude Desktop, Claude Code and other MCP clients. The Instagram and Substack
servers speak standard MCP over stdio. Ready-made client configs live in
[examples/](examples/README.md). Threads and LinkedIn are CLI-only today; an agent
reaches them through the `ghost-in-the-box` command.

[Set up MCP →](docs/mcp.md) · [Connect the platforms →](docs/connectors.md) · [Follow the agent workflow →](docs/agent-workflow.md)

## Your garden stays yours (privacy and safety)

Accounts, cookies, drafts, inboxes, and receipts live in your local state
directory. Ghost in the Box has no project-operated telemetry collector or analytics
uploader. Social requests go to the platforms you connect; optional image
inspection sends selected images to your configured vision endpoint. Those
services control their own data practices.

[Configuration and state](docs/configuration.md) explains the paths and optional
helpers. Credentials and real account exports belong outside Git.

A few things to know before you connect a real account:

- **Sessions are credentials.** A saved session or an export bundle lets anyone who
  holds it act as you. Keep them private and out of Git, chats and screenshots.
- **Platform terms apply.** These tools use your own login, not an official
  developer API. Automated use may break a platform's terms and can get an account
  challenged or restricted. You decide what is acceptable for your account.
- **Nothing publishes by accident.** Login, import, brief, draft, queue and
  `publish --dry-run` never post. A post goes out only when you run
  `ghost-in-the-box publish JOB`, a timer you installed runs `publish --due`, you
  run `threads publish JOB` or `threads retry JOB`, you add `--now` to a `threads`
  command, or an MCP client calls a publishing tool such as `instagram_publish_job`.

## Research use only

This code is for research purposes only. Follow the rules: use it only with accounts you own, respect each platform's terms of service and rate limits, and take responsibility for anything you publish.

## Come grow with us (contributing)

This is a **community project**. Your fixes, improvements, testing, documentation,
and artwork are welcome. AI-assisted code is welcome with regression tests.

We would love help with more provider adapters, clearer account setup, better
conversation context, and a friendly interface over the existing inbox and outbox.
Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the [roadmap](docs/social-engine-plan.md).

Project-owned code uses the [MIT license](LICENSE). Required dependency attribution
is preserved in [Third-party notices](THIRD_PARTY_NOTICES.md).

<div align="center"><sub>Less noise. Better conversations. A little room to grow.</sub></div>
