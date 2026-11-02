"""Interactive browser login and explicitly selected portable account sessions."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

from farm.accounts import AccountRegistry, check_handle, default_account_key
from farm.config import (STATE_ROOT, chromium_install_command, login_family, login_profile_dir,
                         login_profiles_dir)
from farm.session_files import account_name, read_session, stage_session, write_session

class LoginError(ValueError):
    """A login or transfer failure with a stable code and the next command to run."""

    def __init__(self, code, message, hint=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_error(self):
        return {"code": self.code, "message": self.message, "hint": self.hint}


PROVIDERS = {
    "linkedin": ("https://www.linkedin.com/login", ("linkedin.com",)),
    "instagram": ("https://www.instagram.com/accounts/login/", ("instagram.com",)),
    "threads": ("https://www.threads.com/login", ("threads.com",)),
    "substack": ("https://substack.com/sign-in", ("substack.com",)),
}


def publication_url(value):
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("publication must be an HTTPS origin, e.g. https://example.substack.com")
    return "https://" + parsed.netloc.lower()


def provider_cookies(platform, cookies, publication=None):
    domains = PROVIDERS[platform][1]
    if platform == "substack" and publication:
        domains += (urlparse(publication_url(publication)).hostname,)
    selected = [c for c in cookies if isinstance(c, dict) and
                any(c.get("domain", "").lstrip(".") == host or
                    c.get("domain", "").lstrip(".").endswith("." + host) for host in domains)]
    if not selected or any(not isinstance(c.get("name"), str) or not isinstance(c.get("value"), str)
                           for c in selected):
        raise ValueError("no usable provider session cookies were captured")
    return selected


def substack_handle(profile):
    """The handle in a Substack user/profile/self document, or None."""
    if not isinstance(profile, dict):
        return None
    handle = profile.get("handle") or (profile.get("user") or {}).get("handle")
    return handle if isinstance(handle, str) and handle else None


def verify_browser(platform, account, context, *, publication=None):
    """Only authenticated reads; no password, OTP or SSO token interception."""
    cookies = provider_cookies(platform, context.cookies(), publication)
    page = context.pages[0]
    agent = page.evaluate("() => navigator.userAgent")
    session = {"schema": 1, "platform": platform, "account": account,
               "cookies": cookies, "user_agent": agent,
               "verified_at": datetime.now(timezone.utc).isoformat()}
    # Third-party clients sometimes print response bodies on failures.
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet), redirect_stderr(quiet):
        if platform == "linkedin":
            from farm.adapters.linkedin import LinkedInAdapter
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "session.json"
                write_session(path, session)
                registry = AccountRegistry(Path(folder) / "accounts.json")
                registry.register(platform=platform, account=account, kind="cookie_jar", session_file=path)
                adapter = LinkedInAdapter(registry)
                try:
                    adapter.me(account)
                    session = read_session(path)
                finally:
                    adapter.http.close()
        elif platform == "instagram":
            from instagrapi import Client
            token = next((c["value"] for c in cookies if c["name"] == "sessionid"), "")
            client = Client()
            try:
                client.login_by_sessionid(token)
                if client.account_info().username.casefold() != account.casefold():
                    raise ValueError("signed-in Instagram account differs from the selected handle")
                session = client.get_settings()
            finally:
                client.private.close()
                client.public.close()
        elif platform == "threads":
            from scripts.threads_scrapling_login import verify_identity
            valid, final = verify_identity(page, account)
            if not valid:
                raise ValueError("Threads identity could not be verified")
            session["cookies"] = provider_cookies(platform, final)
        else:
            from substack import Api
            if not publication:
                raise ValueError("a Substack publication URL is required")
            publication = publication_url(publication)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "cookies.json"
                write_session(path, {c["name"]: c["value"] for c in cookies})
                api = Api(cookies_path=str(path), publication_url=publication, timeout=30)
                try:
                    profile = api.get_user_profile()
                    # The SDK constructor verifies publication membership as well.
                    handle = substack_handle(profile)
                    if not handle or handle.casefold() != account.casefold():
                        raise ValueError("Substack handle does not match the signed-in account")
                    session["publication_url"] = publication
                    session["cookies"] = [{"name": c.name, "value": c.value,
                                            "domain": c.domain or ".substack.com", "path": c.path,
                                            "secure": c.secure, "expires": c.expires or -1}
                                           for c in api._session.cookies]
                finally:
                    api._session.close()
    return session


def _stderr_prompt(text):
    # Callers print one JSON document on stdout; prompts must not mix into it.
    print(text, end="", file=sys.stderr, flush=True)
    return input()


BROWSER_ENV = "SOCIAL_FARM_LOGIN_BROWSER"
BROWSERS = ("chrome", "msedge", "brave", "chromium")
BROWSER_CHOICES = ("auto",) + BROWSERS
BROWSER_LABELS = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "brave": "Brave", "chromium": "Chromium"}
BUNDLED_LABEL = "Playwright Chromium"
BUNDLED_NOTICE = ("Opening Playwright Chromium (Google sign-in may be blocked in this browser; "
                  "install Chrome or use the site's own password form)")
# Only the bundled fallback is launched by Playwright, so only it needs these.
LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check")
WEBDRIVER_PATCH = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
DEVTOOLS_WAIT_SECONDS = 30
STOP_GRACE_SECONDS = 5
LINKED_WAIT_SECONDS = 30
LINKED = {"instagram": "threads", "threads": "instagram"}
LABELS = {"instagram": "Instagram", "threads": "Threads", "substack": "Substack", "linkedin": "LinkedIn"}
INSTAGRAM_WEB_APP_ID = "936619743392459"

_MAC_APPS = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "brave": "Brave Browser", "chromium": "Chromium"}
_WINDOWS_PATHS = {"chrome": r"Google\Chrome\Application\chrome.exe",
                  "msedge": r"Microsoft\Edge\Application\msedge.exe",
                  "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
                  "chromium": r"Chromium\Application\chrome.exe"}
_LINUX_COMMANDS = {"chrome": ("google-chrome", "google-chrome-stable"),
                   "msedge": ("microsoft-edge", "microsoft-edge-stable"),
                   "brave": ("brave-browser", "brave"),
                   "chromium": ("chromium", "chromium-browser")}
_LINUX_PATHS = {"chrome": ("/opt/google/chrome/chrome",), "msedge": ("/opt/microsoft/msedge/msedge",),
                "brave": ("/opt/brave.com/brave/brave",), "chromium": ()}


def browser_paths(name, *, system=None, environ=None):
    """Where an installed Chrome-family browser's executable usually lives on this OS."""
    if name not in BROWSERS:
        raise ValueError(f"browser must be one of: {', '.join(BROWSERS)}")
    system = system or sys.platform
    environ = os.environ if environ is None else environ
    if system == "darwin":
        app = _MAC_APPS[name]
        return [str(base / f"{app}.app" / "Contents" / "MacOS" / app)
                for base in (Path("/Applications"), Path.home() / "Applications")]
    if system == "win32":
        return [ntpath.join(environ[variable], _WINDOWS_PATHS[name])
                for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if environ.get(variable)]
    found = [path for path in (shutil.which(command) for command in _LINUX_COMMANDS[name]) if path]
    return found + list(_LINUX_PATHS[name])


def find_browser(name):
    """The installed executable for a Chrome-family browser, or None."""
    for path in browser_paths(name):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def installed_browsers():
    """Installed Chrome-family browsers in the order the wizard prefers them."""
    return {name: path for name in BROWSERS if (path := find_browser(name))}


def login_browser_choice(flag=None):
    """The browser choice and where it came from: --browser beats the environment beats auto."""
    if flag:
        if flag not in BROWSER_CHOICES:
            raise LoginError("invalid_browser", f"--browser must be one of: {', '.join(BROWSER_CHOICES)}",
                             "ghost-in-the-box login start --help")
        return flag, "--browser"
    value = os.environ.get(BROWSER_ENV, "").strip().lower()
    if not value:
        return "auto", "default"
    if value not in BROWSER_CHOICES:
        raise LoginError("invalid_browser",
                         f"{BROWSER_ENV}={value!r} is not one of: {', '.join(BROWSER_CHOICES)}",
                         f"set {BROWSER_ENV} to one of {', '.join(BROWSER_CHOICES)}, or unset it")
    return value, BROWSER_ENV


def choose_browser(choice, installed=None):
    """(browser, executable) the wizard opens; executable None means bundled Playwright Chromium."""
    installed = installed_browsers() if installed is None else installed
    names = BROWSERS if choice == "auto" else (choice,)
    for name in names:
        if installed.get(name):
            return name, installed[name]
    if "chromium" in names:
        return "chromium", None
    label = BROWSER_LABELS[choice]
    raise LoginError("dependency_unavailable", f"{label} was not found on this computer",
                     f"install {label}, or run with --browser auto to use another installed browser")


def prepare_profile_dir(platform):
    """Create the platform's persistent login profile, owner-only."""
    profile = login_profile_dir(platform)
    for directory in (login_profiles_dir(), profile):
        if directory.is_symlink():
            raise LoginError("local_state_unavailable",
                             f"login profile path is a symbolic link: {directory}",
                             f"remove {directory}, then run the login again")
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(directory, 0o700)
        except OSError as exc:
            raise LoginError("local_state_unavailable",
                             f"cannot create login profile {directory}: {exc.strerror or exc}",
                             "ghost-in-the-box doctor") from None
    return profile


def forget_login_profile(platform):
    """Delete one platform's persistent login profile; saved account sessions stay."""
    if platform not in PROVIDERS:
        raise ValueError("unsupported login provider")
    profile = login_profile_dir(platform)
    family = login_family(platform)
    shared = sorted(name for name in PROVIDERS if login_family(name) == family)
    result = {"platform": platform, "profile_dir": str(profile), "shared_with": shared}
    if profile.is_symlink() or (profile.exists() and not profile.is_dir()):
        raise LoginError("profile_not_directory", f"login profile path is not a directory: {profile}",
                         f"inspect and remove {profile} yourself")
    if not profile.exists():
        return {**result, "deleted": False, "status": "absent"}
    shutil.rmtree(profile)
    return {**result, "deleted": True, "status": "deleted"}


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _devtools_version(port):
    """The browser's /json/version document, or None while it is not answering."""
    # A configured HTTP proxy must never see the local DevTools port.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            data = json.loads(response.read(65536))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _wait_for_devtools(port, process, label, profile):
    deadline = time.monotonic() + DEVTOOLS_WAIT_SECONDS
    while True:
        if process.poll() is not None:
            raise LoginError("browser_start_failed",
                             f"{label} closed before it was ready; another {label} window may already "
                             f"be using {profile}",
                             "close that browser window and run the login again, or reset the profile with "
                             "ghost-in-the-box login forget-browser --platform PLATFORM")
        version = _devtools_version(port)
        endpoint = (version or {}).get("webSocketDebuggerUrl")
        if isinstance(endpoint, str) and endpoint.startswith((f"ws://127.0.0.1:{port}/", f"ws://localhost:{port}/")):
            return endpoint
        if time.monotonic() >= deadline:
            raise LoginError("browser_start_failed",
                             f"{label} did not open its DevTools port within {DEVTOOLS_WAIT_SECONDS} seconds",
                             "close other windows of that browser and run the login again, or try --browser auto")
        time.sleep(0.25)


def _own_process_group():
    # Ctrl+C in the terminal reaches only the wizard, which then closes the browser itself.
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _stop_process(process, grace=None):
    """SIGTERM lets the browser flush its profile; kill only if it ignores that."""
    grace = STOP_GRACE_SECONDS if grace is None else grace
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


_HOSTS = {"instagram": {"instagram.com", "www.instagram.com"}, "threads": {"threads.com", "www.threads.com"},
          "linkedin": {"linkedin.com", "www.linkedin.com"}, "substack": {"substack.com"}}

_FETCH_JSON = """async ({url, headers}) => {
  const response = await fetch(url, {credentials: 'include', headers, redirect: 'error'});
  if (!response.ok) return null;
  try { return await response.json(); } catch { return null; }
}"""


def _open_pages(context):
    return [page for page in context.pages if not page.is_closed()]


def _provider_page(context, platform):
    hosts = _HOSTS[platform]
    for page in _open_pages(context):
        parsed = urlparse(page.url)
        if parsed.scheme == "https" and parsed.hostname in hosts:
            return page
    return None


def _cookie_values(context, platform, name):
    url = PROVIDERS[platform][0]
    hosts = PROVIDERS[platform][1]
    return {c.get("value") for c in context.cookies([url])
            if c.get("name") == name and isinstance(c.get("value"), str) and
            any(c.get("domain", "").lstrip(".") == host or c.get("domain", "").lstrip(".").endswith("." + host)
                for host in hosts)}


def _detect_raw_handle(platform, context):
    """One in-page read of who is signed in; provider bodies never leave this function."""
    if platform == "threads":
        from scripts.threads_scrapling_login import PROFILE_HANDLES
        page = _provider_page(context, platform)
        if page is None:
            page = _open_pages(context)[0]
            page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=45000)
        handles = {h for h in page.evaluate(PROFILE_HANDLES) or () if isinstance(h, str)}
        return handles.pop() if len(handles) == 1 else None
    page = _provider_page(context, platform)
    if page is None:
        return None
    if platform == "instagram":
        ids = {v for v in _cookie_values(context, platform, "ds_user_id") if v.isdecimal()}
        if len(ids) != 1:
            return None
        data = page.evaluate(_FETCH_JSON, {"url": "/api/v1/accounts/current_user/?edit=true",
                                           "headers": {"X-IG-App-ID": INSTAGRAM_WEB_APP_ID}})
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            return None
        user_id = str(user.get("pk") or user.get("pk_id") or user.get("strong_id__") or "")
        return user.get("username") if user_id in ids else None
    if platform == "linkedin":
        from farm.adapters.linkedin import identity
        tokens = _cookie_values(context, platform, "JSESSIONID")
        if len(tokens) != 1:
            return None
        data = page.evaluate(_FETCH_JSON, {"url": "/voyager/api/me", "headers": {
            "Csrf-Token": tokens.pop().strip('"'), "X-Restli-Protocol-Version": "2.0.0",
            "Accept": "application/vnd.linkedin.normalized+json+2.1"}})
        return identity(data)["username"] if isinstance(data, dict) else None
    data = page.evaluate(_FETCH_JSON, {"url": "/api/v1/user/profile/self", "headers": {}})
    return substack_handle(data)


def detect_handle(platform, context):
    """The signed-in public handle, or None when the session does not say."""
    try:
        return account_name(check_handle(platform, _detect_raw_handle(platform, context)))
    except Exception:
        # Detection is a convenience; any failure falls back to typing the handle.
        return None


def choose_handle(platform, detected, prompt):
    """Confirm a detected handle, or ask for one."""
    if detected:
        print(f"Signed in as @{detected}", file=sys.stderr, flush=True)
        if prompt("Use this handle? [Y/n] ").strip().lower() in ("", "y", "yes"):
            return detected
    else:
        print("Could not read your handle from the session; type it", file=sys.stderr, flush=True)
    while True:
        typed = prompt(f"Your {platform} public handle (without @): ").strip().lstrip("@")
        try:
            return account_name(check_handle(platform, typed))
        except ValueError as exc:
            print(f"{exc}.", file=sys.stderr, flush=True)


def _linked_signed_in(platform, context):
    sessions = {v for v in _cookie_values(context, platform, "sessionid") if len(v) > 8}
    ids = {v for v in _cookie_values(context, platform, "ds_user_id") if v.isdecimal()}
    return len(sessions) == 1 and len(ids) == 1


_CONTINUE = ('button:has-text("Continue as")', '[role="button"]:has-text("Continue as")',
             'a:has-text("Continue as")')


def _click_continue(page):
    """Press the provider's own continue-with-Instagram control on a provider page."""
    from scripts.threads_scrapling_login import CHOICE, trusted, visible
    if not trusted(page):
        return False
    button = visible(page, [*_CONTINUE, *CHOICE])
    if not button:
        return False
    button.click(timeout=10000)
    return True


class LoginWindow:
    """One visible browser on a login profile, opened on first use and reusable within its profile."""

    def __init__(self, browser=None):
        self.choice = browser
        self.browser = None
        self.mode = None
        self.profile_dir = None
        self.family = None
        self.command = None
        self.context = None
        self._manager = None
        self._process = None
        self._remote = None
        self._at = None

    def open(self, platform):
        family = login_family(platform)
        if self.context is not None:
            if family != self.family:
                raise ValueError("this browser window uses another platform's login profile")
            return
        choice, _source = login_browser_choice(self.choice)
        name, executable = choose_browser(choice)
        profile = prepare_profile_dir(platform)
        from playwright.sync_api import sync_playwright
        self.family, self.browser, self.profile_dir = family, name, profile
        self.mode = "attach" if executable else "launch"
        self._manager = sync_playwright()
        playwright = self._manager.__enter__()
        if executable:
            self._attach(playwright, platform, executable, profile)
        else:
            self._launch(playwright, choice, profile)

    def _attach(self, playwright, platform, executable, profile):
        label = BROWSER_LABELS[self.browser]
        port = _free_port()
        self.command = [executable, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                        "--no-first-run", "--no-default-browser-check", PROVIDERS[platform][0]]
        print(f"Opening {label}...", file=sys.stderr, flush=True)
        try:
            self._process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, **_own_process_group())
        except OSError as exc:
            raise LoginError("browser_start_failed", f"{label} could not be started: {exc.strerror or exc}",
                             "run with --browser auto, or reinstall the browser") from None
        endpoint = _wait_for_devtools(port, self._process, label, profile)
        self._remote = playwright.chromium.connect_over_cdp(endpoint, timeout=30000)
        if not self._remote.contexts:
            raise LoginError("browser_start_failed", f"{label} opened without a browser window",
                             "run the login again")
        self.context = self._remote.contexts[0]
        self._at = platform

    def _launch(self, playwright, choice, profile):
        from playwright.sync_api import Error as BrowserError
        print(BUNDLED_NOTICE, file=sys.stderr, flush=True)
        try:
            self.context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False, args=list(LAUNCH_ARGS), ignore_default_args=["--enable-automation"])
        except BrowserError as exc:
            if "playwright install" in str(exc) or "Executable doesn't exist" in str(exc):
                searched = "no installed Chrome, Edge, Brave or Chromium was found and " if choice == "auto" else ""
                raise LoginError("dependency_unavailable",
                                 f"{searched}Playwright Chromium is not installed",
                                 f"install Google Chrome, or run: {chromium_install_command()}") from None
            raise LoginError("browser_start_failed", "Playwright Chromium could not start",
                             "close other windows using the login profile, then run the login again") from None
        self.context.add_init_script(WEBDRIVER_PATCH)

    def page(self):
        pages = _open_pages(self.context)
        return pages[0] if pages else self.context.new_page()

    def _goto(self, platform):
        page = self.page()
        if self._at != platform:
            page.goto(PROVIDERS[platform][0], wait_until="domcontentloaded", timeout=60000)
        self._at = None
        return page

    def sign_in(self, platform, account, *, publication=None, prompt=_stderr_prompt):
        self._goto(platform)
        who = f" as {account}" if account else ""
        prompt(f"Finish {platform} sign-in{who} in the browser, including any security prompts.\n"
               "Leave the browser open, then press Enter here to verify and save: ")
        if not account:
            account = choose_handle(platform, detect_handle(platform, self.context), prompt)
        return account, verify_browser(platform, account, self.context, publication=publication)

    def continue_linked(self, platform, account, *, prompt=_stderr_prompt):
        """Reuse the Meta login already in this profile; fall back to a normal sign-in in the same window."""
        page = self._goto(platform)
        deadline = time.monotonic() + LINKED_WAIT_SECONDS
        clicks = 0
        while True:
            if _linked_signed_in(platform, self.context):
                try:
                    return account, verify_browser(platform, account, self.context)
                except Exception:
                    break
            if time.monotonic() >= deadline:
                break
            if clicks < 3:
                try:
                    clicks += _click_continue(page)
                except Exception:
                    clicks = 3
            page.wait_for_timeout(500)
        label = LABELS[platform]
        print(f"{label} did not continue automatically. Finish the {label} sign-in in the same browser window.",
              file=sys.stderr, flush=True)
        self._at = platform
        return self.sign_in(platform, account, prompt=prompt)

    def close(self):
        remote, context, process, manager = self._remote, self.context, self._process, self._manager
        self._remote = self.context = self._process = self._manager = None
        try:
            # Disconnecting from an attached browser leaves it running until it is stopped below.
            if remote is not None:
                remote.close()
            elif context is not None:
                context.close()
        except Exception:
            pass
        finally:
            try:
                if process is not None:
                    _stop_process(process)
            finally:
                if manager is not None:
                    try:
                        manager.__exit__(None, None, None)
                    except Exception:
                        pass


def browser_login(platform, account=None, *, publication=None, browser=None, prompt=_stderr_prompt,
                  window=None, linked=False):
    """Sign in in a visible browser and return (account, verified session).

    Without an account the handle is read from the signed-in session and confirmed.
    `linked` reuses the Meta login already in the shared profile and needs the account.
    """
    if platform not in PROVIDERS:
        raise ValueError("unsupported login provider")
    if account is not None:
        account_name(check_handle(platform, account))
    elif linked:
        raise ValueError("a linked login needs the account handle")
    if platform == "substack":
        publication = publication_url(publication or "")
    own = window is None
    window = window or LoginWindow(browser)
    try:
        window.open(platform)
        if linked:
            return window.continue_linked(platform, account, prompt=prompt)
        return window.sign_in(platform, account, publication=publication, prompt=prompt)
    finally:
        if own:
            window.close()


def validate_session(platform, account, session):
    account_name(check_handle(platform, account))
    if platform not in PROVIDERS or not isinstance(session, dict):
        raise ValueError("unsupported account session")
    if platform == "instagram":
        auth = session.get("authorization_data") or {}
        if not auth.get("sessionid") or not str(auth.get("ds_user_id", "")).isdigit():
            raise ValueError("incomplete Instagram session")
    else:
        if session.get("schema") != 1 or session.get("platform") != platform or session.get("account") != account:
            raise ValueError("session identity metadata does not match the account")
        if not isinstance(session.get("user_agent"), str) or not session["user_agent"]:
            raise ValueError("session has no browser identity")
        publication = publication_url(session.get("publication_url", "")) if platform == "substack" else None
        cookies = session.get("cookies")
        if not isinstance(cookies, list) or provider_cookies(platform, cookies, publication) != cookies:
            raise ValueError("session includes unexpected cookie domains")
        values = {c["name"]: c["value"] for c in cookies}
        required = {"linkedin": ("li_at", "JSESSIONID"), "threads": ("sessionid", "ds_user_id"),
                    "substack": ("substack.sid",)}[platform]
        if any(not values.get(key) for key in required):
            raise ValueError("incomplete provider session")


def _session_documents(platform, account, session, state_root):
    """Kind, registered path and every private file one login writes."""
    root = Path(state_root)
    path = root / ("ig" if platform == "instagram" else platform) / f"{account}.json"
    if platform == "substack":
        cookies_path = root / "substack" / f"{account}-cookies.json"
        config = {"account": account, "publication_url": session["publication_url"],
                  "cookies_path": str(cookies_path.resolve()), "ready": True, "timezone": "UTC"}
        documents = [(cookies_path, {c["name"]: c["value"] for c in session["cookies"]}), (path, config)]
        return "oauth_file", path, documents
    kind = "instagrapi_settings" if platform == "instagram" else "cookie_jar"
    return kind, path, [(path, session)]


def save_login(platform, account, session, *, registry=None, state_root=STATE_ROOT):
    validate_session(platform, account, session)
    registry = registry or AccountRegistry()
    kind, path, documents = _session_documents(platform, account, session, state_root)
    for target, data in documents:
        write_session(target, data)
    existing = registry.get(platform, account)
    # A re-login keeps a deliberately chosen shared key.
    account_key = existing["account_key"] if existing else default_account_key(platform, account)
    return registry.register(platform=platform, account=account, kind=kind, session_file=path,
                             account_key=account_key)


def export_accounts(selectors, destination, *, registry=None, replace=False):
    registry = registry or AccountRegistry()
    entries = []
    if not selectors:
        raise LoginError("no_accounts_selected", "select at least one platform:account to export",
                         "ghost-in-the-box login list")
    destination = Path(destination).expanduser()
    if destination.exists() and not replace:
        raise LoginError("export_exists", f"export file exists: {destination}",
                         "choose another --output, or add --replace to overwrite it")
    protected_sources = {registry.path.resolve()}
    for selector in dict.fromkeys(selectors):
        platform, separator, account = selector.partition(":")
        if not separator or platform not in PROVIDERS:
            raise LoginError("invalid_selector", f"accounts must be selected as platform:handle, not {selector!r}",
                             "for example: --account instagram:YOUR_HANDLE")
        entry = registry.get(platform, account)
        if not entry or entry["kind"] not in {"cookie_jar", "instagrapi_settings", "oauth_file"}:
            raise LoginError("no_portable_session", f"{selector} has no portable session to export",
                             f"ghost-in-the-box login start --platform {platform} --account {account}")
        source = Path(entry["session_file"])
        protected_sources.add(source.resolve())
        session = read_session(source)
        if platform == "substack":
            cookie_path = Path(session["cookies_path"])
            protected_sources.add(cookie_path.resolve())
            cookies = read_session(cookie_path)
            session = {"schema": 1, "platform": platform, "account": account,
                       "publication_url": session["publication_url"], "user_agent": "GhostInTheBox/0.1",
                       "cookies": [{"name": k, "value": v, "domain": ".substack.com", "path": "/"}
                                   for k, v in cookies.items()]}
        validate_session(platform, account, session)
        entries.append({"platform": platform, "account": account, "account_key": entry["account_key"],
                        "session": session})
    if destination.resolve() in protected_sources:
        raise LoginError("export_overwrites_state", f"export cannot overwrite account state: {destination}",
                         "choose an --output outside the state directory")
    write_session(destination, {"schema": 1, "format": "ghost-in-the-box-sessions", "accounts": entries})
    return {"path": str(destination.resolve()), "accounts": [f'{e["platform"]}:{e["account"]}' for e in entries]}


def import_accounts(source, *, registry=None, state_root=STATE_ROOT, replace=False):
    registry = registry or AccountRegistry()
    source = Path(source).expanduser()
    if not source.exists():
        raise LoginError("bundle_missing", f"session bundle not found: {source}",
                         "export one on the other machine: ghost-in-the-box login export "
                         "--account PLATFORM:HANDLE --output FILE")
    try:
        bundle = read_session(source)
    except (ValueError, OSError) as exc:
        hint = f"chmod 600 '{source}'" if "private" in str(exc) else "export the bundle again"
        raise LoginError("bundle_unreadable", f"cannot read session bundle {source}: {exc}", hint) from None
    if bundle.get("schema") != 1 or bundle.get("format") != "ghost-in-the-box-sessions":
        raise LoginError("bundle_unrecognized", f"{source} is not a ghost-in-the-box session bundle",
                         "create one with: ghost-in-the-box login export --account PLATFORM:HANDLE --output FILE")
    entries = bundle.get("accounts")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("bundle needs 1..100 accounts")
    seen = set()
    existing = {(e["platform"], e["account"].casefold()) for e in registry.list()}
    # Validate the whole bundle and conflicts before writing any session.
    for entry in entries:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("platform", "account")):
            raise ValueError("bundle entries need platform, account and session")
        platform, account = entry["platform"], entry["account"]
        validate_session(platform, account, entry.get("session"))
        if not isinstance(entry.get("account_key"), str) or not 1 <= len(entry["account_key"]) <= 200:
            raise ValueError("invalid account key")
        key = (platform, account.casefold())
        if key in seen:
            raise ValueError("duplicate account in bundle")
        seen.add(key)
        target = Path(state_root) / ("ig" if platform == "instagram" else platform) / f"{account}.json"
        if not replace and (key in existing or target.exists()):
            raise LoginError("already_registered", f"{platform}:{account} is already registered here",
                             "add --replace to overwrite the existing session")
    planned = []
    for entry in entries:
        kind, path, documents = _session_documents(entry["platform"], entry["account"], entry["session"], state_root)
        planned.append((entry, kind, path, documents))
    staged = []   # (temporary, target)
    replaced = []  # (target, backup or None)
    try:
        for _entry, _kind, _path, documents in planned:
            for target, data in documents:
                staged.append((stage_session(target, data), Path(target).expanduser()))
        for temporary, target in staged:
            backup = None
            if target.exists():
                backup = target.with_name(f".{target.name}.import-backup-{os.getpid()}")
                os.replace(target, backup)
            replaced.append((target, backup))
            os.replace(temporary, target)
        registry.register_many([{"platform": entry["platform"], "account": entry["account"], "kind": kind,
                                 "session_file": path, "account_key": entry["account_key"]}
                                for entry, kind, path, _documents in planned])
    except BaseException:
        # Put every session file back the way it was; the registry was written once or not at all.
        for target, backup in reversed(replaced):
            try:
                if backup is not None:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                pass
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)
        raise
    for _target, backup in replaced:
        if backup is not None:
            backup.unlink(missing_ok=True)
    results = [f'{entry["platform"]}:{entry["account"]}' for entry, *_ in planned]
    return {"accounts": results, "status": "imported", "network_requests": 0}
