# Sign in here, run here or on your server (login and session transfer)

The wizard supports LinkedIn, Instagram, Threads, and Substack on Python 3.11+
on macOS, desktop Linux, and native Windows. It opens your installed Chrome for
each selected provider, with a profile kept under the state dir. Complete your
normal password, SSO, email-link, passkey, or two-factor flow yourself, then
return to the terminal. Sign in with Google works there. A server only importing
sessions needs no desktop. Provider session acceptance can depend on the
server's location/IP.

Plain meanings of session, registry and account key are in the
[glossary](glossary.md).

These are reusable session credentials, rather than developer API keys. A normal
sign-in does not create an official LinkedIn developer API key. Passwords and OTPs
stay in the login browser. The tool captures only the selected provider's cookies
and verifies the signed-in identity before registering the session.

## Install

From the checkout, on macOS or Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[recommended]'
python -m playwright install chromium
```

On Windows PowerShell, activation is optional:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[recommended]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\ghost-in-the-box.exe login
```

`recommended` is `login` plus `instagram` plus `media`. Add `substack` for
Substack, for example `'.[recommended,substack]'`. For LinkedIn and Threads
alone, `.[login]` is enough. Run `ghost-in-the-box doctor` afterwards: it checks
the extras, names the browser the wizard will open and lists the login profiles that exist. On Linux, Playwright's browser system
libraries may need `python -m playwright install --with-deps chromium`.
Browser/runtime downloads use PyPI and Playwright's browser distribution servers.
See [Playwright browser installation](https://playwright.dev/python/docs/browsers).

## Choose accounts

```sh
ghost-in-the-box login
```

The wizard shows a numbered menu with what is already connected:

```text
Which account do you want to connect?
  1) Instagram      logged in as @YOUR_HANDLE (2026-11-01)
  2) Threads        not connected (same login as Instagram)
  3) Substack       not connected
  4) LinkedIn       not connected
  d) done
Pick a number (or d):
```

Type a number, several numbers (`1 3` or `1,3`), or a platform name. Substack
first asks for the publication's HTTPS origin. Picking a platform that is
already connected asks before it opens a browser.

You do not type your handle. After you sign in and press Enter, the wizard reads
it from the signed-in session in the same browser and asks you to confirm:

```text
Signed in as @YOUR_HANDLE
Use this handle? [Y/n]
```

Answer `n` to type a different one. If the session does not say, the wizard
prints `Could not read your handle from the session; type it` and asks. It makes
one request at most, from inside the provider's own page:

| Platform | Where the handle comes from |
| :--- | :--- |
| LinkedIn | The `voyager/api/me` call the LinkedIn reader already makes (`publicIdentifier`). |
| Instagram | `api/v1/accounts/current_user`, checked against the `ds_user_id` cookie. |
| Threads | Your Profile link on threads.com, the same link the Threads check reads. |
| Substack | `api/v1/user/profile/self`, the call the Substack SDK uses. | After each login, success or failure, it prints one line and shows the
menu again. Type `d`, `done` or press Enter to finish. The JSON printed at the end
lists every login from the session and the accounts now registered.
Bare `ghost-in-the-box login` is the same as `login start`. Each session is saved
as soon as its login completes. Press Ctrl+C to cancel; the wizard exits with
code 130 and keeps the sessions it already saved.
You can also choose explicitly, without the menu:

```sh
ghost-in-the-box login start --platform linkedin --account YOUR_PUBLIC_HANDLE
ghost-in-the-box login start --platform threads --platform instagram
ghost-in-the-box login start --platform substack --account YOUR_HANDLE --publication https://YOUR-PUBLICATION.substack.com
ghost-in-the-box login list
```

`--account` needs exactly one `--platform`. It skips the handle detection and
the confirmation.

Use the same account handle when reconnecting. Sessions live in the state dir
(default `~/.batbox/social-farm-state`, or `SOCIAL_FARM_STATE_DIR`), and the
account registry points to them. `ghost-in-the-box config` prints the exact paths.
Instagram and Threads both register with the account key `meta:HANDLE`, so one
person's Instagram and Threads jobs never publish at the same time. A re-login
keeps an account key you chose yourself. Instagram is stored as instagrapi settings;
LinkedIn and Threads use portable cookie jars; Substack uses the SDK cookie file
and connection configuration.
Threads portable sessions use the existing browser reader/publisher with Playwright,
including its durable attempt journal and quiet-hour behavior.

Only the selected provider's cookies are saved as the session. Google, Apple and
other identity-provider cookies stay in the browser profile and are never
copied into a session file. Nothing is published by login, export, import, or list. Provider reads and any
explicit later deliveries go to the connected provider; Substack membership
verification also contacts the selected publication through its SDK.

## Which browser opens

The wizard starts your installed Chrome itself, as a normal program. It passes
only `--user-data-dir`, `--remote-debugging-port`, `--no-first-run`,
`--no-default-browser-check` and the provider's login page. It then connects to
that browser to read cookies when you press Enter. When you finish, it closes the
browser it started. Your everyday Chrome window and profile are not touched.

Why this matters: a browser started by an automation tool carries automation
switches and a `navigator.webdriver` flag. Google refuses to sign in there and
shows "Couldn't sign you in. This browser or app may not be secure." A fresh
temporary profile also looks like incognito. Your own Chrome on its own profile
has neither, so Sign in with Google works, for example on LinkedIn.

By default the wizard picks the first browser it finds, in this order: Google
Chrome, Microsoft Edge, Brave, Chromium. On stderr it says which one, for
example `Opening Google Chrome...`. Choose one yourself with `--browser`:

```sh
ghost-in-the-box login start --platform linkedin --browser msedge
```

`--browser` takes `auto`, `chrome`, `msedge`, `brave` or `chromium`. The
`SOCIAL_FARM_LOGIN_BROWSER` setting takes the same values, and the flag wins over
it. A browser you name that is not installed is an error; the wizard does not
switch to another one. Use `auto` for that.

If none of those browsers is installed, the wizard falls back to Playwright's
own Chromium. It hides the automation flags there as far as it can, but Google
may still block sign-in. The wizard warns you with `Opening Playwright Chromium
(Google sign-in may be blocked in this browser; install Chrome or use the
site's own password form)`. Install Chrome, or sign in with the site's own
password form.

While the wizard runs, the browser's DevTools port listens on `127.0.0.1`.
Programs on this computer could use it to control that browser until the wizard
closes it.

## The login browser profile

Each login family keeps one browser profile under the state dir, in
`login-profiles/`. Instagram and Threads share `login-profiles/meta`. LinkedIn
and Substack each have their own, so a Google session used for LinkedIn does not
reach the others. The folders are owner-only.

Treat a profile like a credential. It holds a signed-in browser session,
including your Google or Apple sign-in if you used one. Anyone who copies the
folder can act as you on those sites. Keep it out of Git and backups you share.
It also means your next login is often one click, because the browser still
remembers you.

To start clean, delete one platform's profile:

```sh
ghost-in-the-box login forget-browser --platform linkedin
```

It asks before it deletes. Add `--yes` to skip the question. It refuses to run
without `--platform`. Deleting `instagram` or `threads` deletes the shared Meta
profile. Saved account sessions and the registry stay as they are.

## Instagram and Threads are one login

Instagram and Threads use the same Meta login, so you sign in once. After an
Instagram login the wizard asks:

```text
Threads uses the same Instagram login. Connect Threads now with it? [Y/n]
```

On yes, the same browser window opens the Threads login page. Threads usually
offers "Continue as @YOUR_HANDLE", and the wizard presses it. It waits up to 30
seconds for the Threads session, checks who signed in, and registers Threads
under the same handle with the account key `meta:YOUR_HANDLE`. It does not ask
for the handle again. If Threads does not continue in time, the wizard says so
and you finish the Threads sign-in yourself in that same window. A Threads login
offers Instagram the same way. Asking for both with `--platform instagram
--platform threads` connects the second without the question. Picking Threads in
the menu while Instagram is connected goes straight to the shared login.

## Move selected sessions

Select each account explicitly as `PLATFORM:HANDLE` (this is the platform and
handle, not the account key). The export contains the reusable credentials in
plain JSON. Keep it outside Git, transfer it over SSH, and treat it as account access.
POSIX files are owner-only; new Windows session/export files get an ACL granting
the current Windows user access, with inherited permissions removed. Windows
reads rely on Windows ACLs rather than POSIX mode bits. Protect received files
with your user account's ACL as well.

```sh
ghost-in-the-box login export --account linkedin:YOUR_PUBLIC_HANDLE --account threads:YOUR_THREADS_HANDLE --output ~/selected.ghostbox-sessions.json
scp ~/selected.ghostbox-sessions.json YOUR_SERVER:~/selected.ghostbox-sessions.json
```

On the destination, install Ghost in the Box and import:

```sh
chmod 600 ~/selected.ghostbox-sessions.json
ghost-in-the-box login import ~/selected.ghostbox-sessions.json
ghost-in-the-box linkedin --account YOUR_PUBLIC_HANDLE me
```

Windows also ships an OpenSSH `scp` client on supported installations; use an
explicit path under `$HOME` for the export. The same bundle imports on Windows.
Import rewrites all session and Substack cookie paths for the destination's state
directory, preserves account keys, and leaves unrelated accounts alone. Import is
offline: run an authenticated read afterward to verify provider acceptance.
Existing registrations and export files require `--replace` to overwrite.
Import is all or nothing: it stages every file first and writes the registry
once, so a failed import leaves your existing accounts untouched.

The server needs the provider extras for the accounts it will use. Threads also
needs `.[login]` and Playwright Chromium for headless operations; LinkedIn reads
and text delivery only need the base Requests dependency. Substack can run with
`ghost-in-the-box substack-mcp --account YOUR_HANDLE` after import.

Sessions expire and providers can require another challenge after a move. Re-run
login for the affected account, export it again, and import with `--replace`.

## Other Threads sign-in routes

The wizard is the normal route for Threads. The `threads` command also offers
`threads --account HANDLE connect` (link a saved Instagram session to a verified
browser profile), `threads --account HANDLE mobile-login` (mint the mobile token
from the Instagram password file) and `threads-setup --account HANDLE` (the
optional Node browser bridge). The older one-off Threads login scripts were
removed. See [connections](connectors.md#threads).

## What is tested on Windows

Native Windows CI runs the offline tests for the login wizard, session export and
import, the account registry and file locking, Instagram session files, LinkedIn
reads and the portable Threads transport. The outbox, publishing, inbox, MCP
servers, timers and the Scrapling and Node Threads helpers are tested on Linux
and macOS only. On Windows, use WSL for those.

Exit codes and the JSON error format are listed in
[configuration](configuration.md#errors-and-exit-codes).
