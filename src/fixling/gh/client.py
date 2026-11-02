"""GitHub REST client over httpx (spec E19).

Only what Fixling needs. Rate limits are honoured (``Retry-After`` / ``x-ratelimit-reset``), the token only ever
travels in the ``Authorization`` header, and no error message contains request headers.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from fixling.errors import GitHubError

API = "https://api.github.com"
API_VERSION = "2022-11-28"
MAX_WAIT_S = 90.0
WRITE_ROLES = {"admin", "maintain", "write"}


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = API,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_retries: int = 3,
    ) -> None:
        self._token = token
        self._sleep = sleep
        self._clock = clock
        self._max_retries = max_retries
        self._http = client or httpx.Client(base_url=base_url, timeout=30.0)

    # ------------------------------------------------------------------ plumbing
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _wait_for(self, resp: httpx.Response) -> float | None:
        """Seconds to wait if ``resp`` is a rate-limit response, else ``None``."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                return max(0.0, float(reset) - self._clock()) + 1.0
        return 60.0 if resp.status_code == 429 else None

    def request(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201, 204), **kw: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._http.request(method, path, headers=self._headers(), **kw)
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise GitHubError(f"GitHub request {method} {path} failed: {type(exc).__name__}") from exc
                self._sleep(2.0**attempt)
                continue
            wait = self._wait_for(resp)
            if wait is not None and attempt < self._max_retries:
                if wait > MAX_WAIT_S:
                    raise GitHubError(f"GitHub rate limit: would have to wait {int(wait)}s (limit {int(MAX_WAIT_S)}s)")
                self._sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < self._max_retries:
                self._sleep(2.0**attempt)
                continue
            if resp.status_code in ok:
                return resp
            raise GitHubError(
                f"GitHub {method} {path} -> {resp.status_code}: {_message(resp)}", status=resp.status_code
            )
        raise GitHubError(f"GitHub {method} {path} failed after retries")  # pragma: no cover

    def _json(self, method: str, path: str, **kw: Any) -> Any:
        return self.request(method, path, **kw).json()

    # ------------------------------------------------------------------ issues
    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        data = self._json("GET", f"/repos/{repo}/issues/{number}")
        if not isinstance(data, dict):
            raise GitHubError("unexpected response for an issue")
        return data

    def get_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        data = self._json("GET", f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100})
        return list(data) if isinstance(data, list) else []

    def list_labeled_issues(self, repo: str, label: str) -> list[dict[str, Any]]:
        """Open issues (not PRs) carrying ``label``."""
        data = self._json("GET", f"/repos/{repo}/issues", params={"labels": label, "state": "open", "per_page": 50})
        return [i for i in data if isinstance(i, dict) and "pull_request" not in i] if isinstance(data, list) else []

    def issue_events(self, repo: str, number: int, per_page: int = 100) -> list[dict[str, Any]]:
        data = self._json("GET", f"/repos/{repo}/issues/{number}/events", params={"per_page": per_page})
        return list(data) if isinstance(data, list) else []

    def latest_label_event(self, repo: str, number: int, label: str) -> dict[str, Any] | None:
        """The most recent ``labeled`` event for ``label`` (who added it, and when)."""
        events = [
            e
            for e in self.issue_events(repo, number)
            if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label
        ]
        return max(events, key=lambda e: int(e.get("id", 0)), default=None)

    def get_permission(self, repo: str, user: str) -> str:
        """``admin`` / ``maintain`` / ``write`` / ``triage`` / ``read`` / ``none`` for ``user`` on ``repo``."""
        try:
            data = self._json("GET", f"/repos/{repo}/collaborators/{user}/permission")
        except GitHubError as exc:
            if exc.status in (403, 404):
                return "none"
            raise
        return str(data.get("role_name") or data.get("permission") or "none")

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.request("POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            self.request("DELETE", f"/repos/{repo}/issues/{number}/labels/{label}")
        except GitHubError as exc:
            if exc.status != 404:  # already gone is fine
                raise

    def comment(self, repo: str, number: int, body: str) -> str:
        data = self._json("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        return str(data.get("html_url", ""))

    def ensure_labels(self, repo: str, labels: dict[str, str]) -> None:
        """Create ``{name: color}`` labels, ignoring ones that already exist."""
        for name, color in labels.items():
            try:
                self.request("POST", f"/repos/{repo}/labels", json={"name": name, "color": color}, ok=(201,))
            except GitHubError as exc:
                if exc.status != 422:
                    raise

    # ------------------------------------------------------------------ repos and PRs
    def get_default_branch(self, repo: str) -> str:
        return str(self._json("GET", f"/repos/{repo}").get("default_branch", "main"))

    def create_pr(
        self, repo: str, head: str, base: str, title: str, body: str, *, draft: bool = True
    ) -> dict[str, Any]:
        """Open a PR; if one already exists for ``head`` (a retried run), return it instead of failing."""
        try:
            data = self._json(
                "POST",
                f"/repos/{repo}/pulls",
                json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
            )
            return dict(data)
        except GitHubError as exc:
            if exc.status != 422:
                raise
            owner = repo.split("/")[0]
            existing = self._json("GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{head}", "state": "open"})
            if isinstance(existing, list) and existing:
                return dict(existing[0])
            raise

    def close(self) -> None:
        self._http.close()


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        text = str(data.get("message", "")) if isinstance(data, dict) else ""
        errs = data.get("errors") if isinstance(data, dict) else None
        if errs:
            text += f" {errs}"
    except ValueError:
        text = resp.text[:200]
    return text[:300]
