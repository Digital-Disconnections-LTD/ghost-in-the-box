# A comfortable place for your state

The default is `~/.batbox/social-farm-state/`. Set `SOCIAL_FARM_STATE_DIR` to
choose another root. Explicit per-tool paths keep precedence.

The `SOCIAL_FARM_*` configuration names and default state directory are retained
for compatibility with existing local sessions.

| Setting | Purpose |
| --- | --- |
| `SOCIAL_FARM_STATE_DIR` | Shared registry, inbox, outbox, and provider state root. |
| `SOCIAL_FARM_ACCOUNTS_FILE` | Explicit account registry file. |
| `SOCIAL_FARM_OUTBOX_DB` | Explicit delivery database. |
| `SOCIAL_FARM_SUBSTACK_CONFIG` | Private Substack connection JSON. |
| `SOCIAL_FARM_SCRAPLING_PYTHON` | Python environment containing Scrapling; defaults to the running interpreter. |
| `SOCIAL_FARM_IG_PYTHON` | Optional explicit Instagram Python interpreter. |
| `SOCIAL_FARM_IG_ACCOUNT` | Default account for the Instagram MCP launcher. |
| `SOCIAL_FARM_THREADS_MCP_SOURCE` | Built optional Threads MCP source checkout. |
| `SOCIAL_FARM_NODE` | Explicit Node executable for the optional browser bridge. |
| `SOCIAL_FARM_VISION_URL` / `SOCIAL_FARM_VISION_MODEL` | Optional image-inspection endpoint and model. |
| `SOCIAL_FARM_CHAT_PATH` | Optional durable notification delivery hook. |

Account/session data, cookies, draft databases, media downloads, and publication
receipts belong in private local state. Ghost in the Box uses private file modes and
atomic writes for its session metadata. Keep state and credentials out of Git.

The local visual reference shelf defaults to
`~/.batbox/media-reference/tech-memes/`; commands accepting a root path can select
another location. OCR uses an optional `tesseract` executable. Image inspection
sends the selected image to the endpoint you configure.

## Notification delivery

Threads inbox updates remain durable local records. Without a configured chat
hook, notification requests return a pending batch for the operator to inspect.
An explicit hook receives `send-agent RECIPIENT CONTENT --id BATCH_ID` and must
return JSON containing the same `id` and `durable: true` before the batch is
marked sent. Other coordinators can read the inbox directly.
The notification refresh helper observes quiet hours from 23:55 to 06:00 in
`America/New_York`; local inbox inspection remains available at any time.

## Optional user services

The `systemd/` examples target a checkout at `~/ghost-in-the-box` with `.venv/` inside
it. Read-only refresh services are separate from publishers. Review the command,
account, and schedule before installing a timer. No service starts during a
package installation.

The Threads publisher example reads `SOCIAL_FARM_ACCOUNT` from
`~/.config/ghost-in-the-box/runtime.env`. Supply your own account; no personal account
is bundled with the project.
