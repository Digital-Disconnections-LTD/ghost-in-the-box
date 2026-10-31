# Working on Ghost in the Box

Read [CONTRIBUTING.md](CONTRIBUTING.md), [ARCHITECTURE.md](ARCHITECTURE.md), and
[the social engine plan](docs/social-engine-plan.md) before changing the code.

- Preserve user edits. Use `gh` for GitHub operations.
- Keep changes focused and reuse the account registry, inbox, outbox, and provider
  adapters. Do not add a second queue, task tracker, or account authority.
- Preserve existing state by default; configuration selects alternate paths.
  Keep optional helpers separate from the base install.
- Include regression tests for substantive code changes and run the affected
  checks. Add a test that fails before a bug fix and passes after it.
- Tests must stay offline, use temporary state and fake accounts, and never send
  a live social post or newsletter. A live delivery needs the user's authorization
  for that account and content. Honor authorization already given without asking
  for it repeatedly.
- Do not commit credentials, cookies, personal account records, provider exports,
  private operational notes, generated posting receipts, or local database files.
- Keep project code free of telemetry collection and third-party analytics.
  Document the network boundaries of providers and optional helpers.
- Preserve required copyright, license, and attribution notices.
- After a substantive code improvement, suggest a pull request to the user with
  a concrete title and reviewable description. Follow existing publication
  instructions when the user has already authorized creating or merging it.

Use [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md): explain
the problem and final behavior, relevant implementation choices, regression
coverage and actual test results, and platform/provider compatibility limits.
Describe the final change for someone who has not read the conversation.
