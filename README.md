<div align="center">
  <img src="docs/assets/ghost-in-the-box-garden.png" alt="Ghost in the Box — a friendly glowing ghost grows conversations in a midnight garden." width="100%">
  <h1>Ghost in the Box</h1>
  <p><strong>Your accounts. Your voice. Your garden.</strong></p>
  <p>A local social engine for thoughtful drafts, useful conversations, and publishing with receipts.</p>
  <p><a href="docs/getting-started.md">Plant your first seed</a> · <a href="docs/agent-workflow.md">Bring your Bats</a> · <a href="CONTRIBUTING.md">Grow with us</a> · <a href="docs/social-engine-plan.md">The next season</a></p>
  <p><strong>PYTHON 3.11+ · LINUX · macOS · MIT · COMMUNITY PROJECT</strong></p>
</div>

---

## Welcome to the garden 🌱

Good conversations need more than a publish button. Ghost in the Box connects the
work around it: understand the conversation, keep the sources, develop a draft,
choose the account, send the exact job, and remember what happened.

Built by [Digital Disconnections](https://digitaldisconnections.com/) for people
who want to own their tools and keep control of their work. Read our
[mission statement — It's Not 1997](https://digitaldisconnections.com/our-story.html).

| In the garden | What it does |
| :--- | :--- |
| **Know your accounts** | Keep local references to your own saved sessions. |
| **Listen first** | Read bounded posts, comments, replies, and feeds into a local inbox. |
| **Keep your voice** | Import your own writing and build source-linked briefs. |
| **Make something worth sharing** | Prepare text, carousels, and visual research. |
| **Publish with receipts** | Freeze queued content, claim one job, record its provider ID and link. |
| **Recover honestly** | Keep uncertain sends visible for reconciliation instead of blindly repeating them. |

## Pick a plot

| Connection | Available today |
| :--- | :--- |
| **Instagram** | Session registry, own-writing importer, inbox, comments, carousel drafts, exact-job publishing, and MCP tools. |
| **Threads** | Posts and reply readers, inbox triage, drafts, cadence, and publishing adapters. Account setup and provider authentication are required. Browser helpers are optional. |
| **Substack** | Optional SDK-backed MCP connector, persistent cookies, setup status, and publication receipts. |
| **Brief engine** | Source-linked briefs and draft files for Instagram, Threads, Substack, LinkedIn, Facebook, TikTok, and YouTube. |

LinkedIn, Facebook, TikTok, and YouTube currently have **brief formats**, not
built-in publishing adapters. Provider access changes, so mocked tests establish
local behavior rather than a guarantee that your account is connected.

## Plant your first seed

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[instagram,media]'
ghost-in-the-box doctor
ghost-in-the-box --help
```

Try the [offline example](docs/getting-started.md#try-it-without-an-account) before
connecting anything. It generates a brief and a local outbox draft without
logging in or sending a post.

Connect your chosen account using the [first-seed guide](docs/getting-started.md),
then use the same `ghost-in-the-box` command for accounts, briefs, inboxes, and delivery.
Linux and macOS use POSIX file locks; Windows users need a Linux environment such
as WSL.

## Bring a Bat, or your favorite MCP client 🦉

Ghost in the Box works alongside [Batbox](https://github.com/Digital-Disconnections-LTD/batbox)
and other MCP clients. Instagram tools speak framed or newline JSON over stdio;
the included bridge connects framed clients to the optional Substack server.

[Connect the tools →](docs/connectors.md) · [Follow the agent workflow →](docs/agent-workflow.md)

## Your garden stays yours

Accounts, cookies, drafts, inboxes, and receipts live in your local state
directory. Ghost in the Box has no project-operated telemetry collector or analytics
uploader. Social requests go to the platforms you connect; optional image
inspection sends selected images to your configured vision endpoint. Those
services control their own data practices.

[Configuration and state](docs/configuration.md) explains the paths and optional
helpers. Credentials and real account exports belong outside Git.

## Come grow with us

This is a **community project**. Your fixes, improvements, testing, documentation,
and artwork are welcome. AI-assisted code is welcome with regression tests.

We would love help with more provider adapters, clearer account setup, better
conversation context, and a friendly interface over the existing inbox and outbox.
Start with [CONTRIBUTING.md](CONTRIBUTING.md) and the [roadmap](docs/social-engine-plan.md).

Project-owned code uses the [MIT license](LICENSE). Required dependency attribution
is preserved in [Third-party notices](THIRD_PARTY_NOTICES.md).

<div align="center"><sub>Less noise. Better conversations. A little room to grow.</sub></div>
