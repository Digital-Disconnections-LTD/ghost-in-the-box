# Plant your first seed

Use Python 3.11 or newer on Linux or macOS. The core uses POSIX file locking;
Windows needs a Linux environment such as WSL. Git is needed to clone the source.

```sh
git clone https://github.com/Digital-Disconnections-LTD/ghost-in-the-box.git
cd ghost-in-the-box
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[instagram,media]'
ghost-in-the-box doctor
```

The `ghost-in-the-box` command is on PATH while this environment is activated.
`doctor` reports local prerequisites without opening sessions or making network
requests. Run `ghost-in-the-box --help` to find the available tools.

## Try it without an account

Generate a local, source-linked brief and draft body files:

```sh
ghost-in-the-box brief --json examples/topic-pack.json --out out/demo --emit
```

The output prints receipt paths and draft file paths. Review a `.threads.body`
file, then put it in a local outbox:

```sh
ghost-in-the-box outbox --db out/demo.sqlite3 draft \
  --platform threads --account example_creator \
  --body-file out/demo/YOUR-BRIEF.threads.body \
  --source-url https://digitaldisconnections.com/our-story.html
ghost-in-the-box outbox --db out/demo.sqlite3 list
```

Replace `YOUR-BRIEF.threads.body` with the actual emitted filename. This creates
a local draft, without logging in or sending anything. Files under `out/` are
ignored by Git.

## Connect your own account

Create a private session using the relevant provider's login flow, then register
the saved session reference.

If you need to create a session first, place your own `username` and `password`
in a private JSON file outside this checkout, make it readable only by you
(`chmod 600`), then run:

```sh
ghost-in-the-box instagram-login --credentials /absolute/private/instagram-login.json
```

The result reports the saved session path. When required, supply your current
authenticator code through stdin with `--verification-code-stdin`; keep it out
of command arguments and shell history. Provider challenges may need account
owner recovery before another attempt.

For an existing or newly saved session:

```sh
ghost-in-the-box accounts register --platform instagram --account YOUR_HANDLE \
  --kind instagrapi_settings --session-file /absolute/private/instagram.json
ghost-in-the-box accounts show instagram YOUR_HANDLE
```

The registry stores a path to the session, rather than copying its credentials.
Keep the session private (`chmod 600` on Linux/macOS). Instagram and Threads can
share an account key when they genuinely share the same authenticated identity.
See [connections](connectors.md) before configuring the linked account.

## Read, draft, queue, deliver

```sh
ghost-in-the-box instagram-import import --username YOUR_HANDLE --pages 1
ghost-in-the-box instagram-inbox --account YOUR_HANDLE sync --posts 3 --pages 1
```

Use the [agent workflow](agent-workflow.md) to turn those observations into a
draft, queue the selected job, and publish that exact ID. Headless commands are
noninteractive; login recovery is a separate operation.

For development:

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m build
```

The offline suite does not establish live provider connectivity. Optional browser
and Substack dependencies are described in [connections](connectors.md).
