# Come grow with us 🌱

Your fixes and improvements are welcome. A small bug report, a clearer sentence,
a provider fixture, or a better first-run experience can make a real difference.

## Find a patch of garden

- **Provider adapters:** add a lane that can demonstrate account identity,
  bounded reads, exact-job delivery, receipts, and restart recovery.
- **Conversation context:** make it easier for agents to understand a source post
  and its replies before drafting.
- **A friendly interface:** build over the existing inbox, account registry, and
  outbox rather than introducing parallel stores.
- **Reliable setup:** improve Linux/macOS installation and optional browser helper
  diagnostics; report the versions you actually exercised.
- **Good documentation and artwork:** help people understand what is available,
  what needs account setup, and what is still growing.

## AI code is welcome

AI-written and AI-assisted code are welcome with regression tests. Understand
what you submit and verify it yourself. A bug test should fail before the fix
and pass afterward; a feature should demonstrate its new behavior and failure
paths. Documentation and artwork need relevant link or visual checks.

## Make a pull request worth reading

1. Keep the change focused and explain the resulting behavior.
2. Reuse the current state authorities and provider boundaries.
3. Run `python -m unittest discover -s tests -v` in the development environment.
4. State the regression coverage, exact checks, and any limitations.
5. Include screenshots for interface changes and platform/version details for
   integration fixes.

Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md). For a larger feature,
open an issue with the problem and proposed result first. Small improvements can
arrive directly as a pull request. AI assistants should read [AGENTS.md](AGENTS.md).

Tests use fake sessions and isolated temporary state. Do not publish real posts,
contact real subscribers, or use personal accounts as a test fixture. Account
sessions, cookies, credentials, private messages, and provider exports stay out
of Git, issues, screenshots, and test logs.

Keep the project free of telemetry collectors and analytics uploads. Document
new provider requests and the data they send. Required dependency notices travel
with any reused material. Contributions use the project's [MIT license](LICENSE).

Be kind, be specific, and leave room for someone else to learn.
