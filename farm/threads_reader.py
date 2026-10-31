"""Bounded Threads reads with a persistent browser fallback."""

from farm.adapters.threads import ThreadsAdapter, ThreadsReadFailure
from farm.threads_browser import BrowserThreadsAdapter


class ThreadsReader:
    def __init__(self, mobile=None, browser=None):
        self.mobile = mobile or ThreadsAdapter()
        self.browser = browser or BrowserThreadsAdapter()
        self._browser_only = False

    def _call(self, name, *args, **kwargs):
        if not self._browser_only:
            try:
                return getattr(self.mobile, name)(*args, **kwargs)
            except ThreadsReadFailure:
                self._browser_only = True
        return getattr(self.browser, name)(*args, **kwargs)

    def me(self, account: str) -> dict:
        return self._call("me", account)

    def own_posts(self, account: str, *, limit: int = 10, cursor: str = "") -> dict:
        return self._call("own_posts", account, limit=limit, cursor=cursor)

    def replies(self, account: str, post_id: str, *, limit: int = 10,
                cursor: str = "") -> dict:
        return self._call("replies", account, post_id, limit=limit, cursor=cursor)
