from __future__ import annotations

import httpx
import pytest
import respx

from fixling.errors import GitHubError
from fixling.gh.client import GitHubClient

TOKEN = "ghp_" + "T" * 36
BASE = "https://api.github.com"


@pytest.fixture
def gh() -> tuple[GitHubClient, list[float]]:
    sleeps: list[float] = []
    return GitHubClient(TOKEN, sleep=sleeps.append, clock=lambda: 1000.0), sleeps


@respx.mock
def test_requests_carry_auth_and_api_version(gh: tuple[GitHubClient, list[float]]) -> None:
    route = respx.get(f"{BASE}/repos/o/r/issues/3").respond(200, json={"number": 3, "title": "t"})
    assert gh[0].get_issue("o/r", 3)["title"] == "t"
    req = route.calls[0].request
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["x-github-api-version"] == "2022-11-28"


@respx.mock
def test_list_labeled_issues_skips_pull_requests(gh: tuple[GitHubClient, list[float]]) -> None:
    route = respx.get(f"{BASE}/repos/o/r/issues").respond(
        200, json=[{"number": 1}, {"number": 2, "pull_request": {"url": "x"}}, {"number": 3}]
    )
    assert [i["number"] for i in gh[0].list_labeled_issues("o/r", "fixling")] == [1, 3]
    assert route.calls[0].request.url.params["labels"] == "fixling"
    assert route.calls[0].request.url.params["state"] == "open"


@respx.mock
def test_latest_label_event_picks_the_newest_matching_event(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.get(f"{BASE}/repos/o/r/issues/3/events").respond(
        200,
        json=[
            {"id": 10, "event": "labeled", "label": {"name": "fixling"}, "actor": {"login": "old"}},
            {"id": 30, "event": "labeled", "label": {"name": "other"}, "actor": {"login": "x"}},
            {"id": 20, "event": "labeled", "label": {"name": "fixling"}, "actor": {"login": "new"}},
            {"id": 40, "event": "unlabeled", "label": {"name": "fixling"}, "actor": {"login": "y"}},
        ],
    )
    ev = gh[0].latest_label_event("o/r", 3, "fixling")
    assert ev is not None and ev["actor"]["login"] == "new"
    respx.get(f"{BASE}/repos/o/r/issues/4/events").respond(200, json=[])
    assert gh[0].latest_label_event("o/r", 4, "fixling") is None


@respx.mock
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"permission": "write", "role_name": "write"}, "write"),
        ({"permission": "write", "role_name": "maintain"}, "maintain"),
        ({"permission": "read"}, "read"),
    ],
)
def test_permission_prefers_role_name(
    gh: tuple[GitHubClient, list[float]], payload: dict[str, str], expected: str
) -> None:
    respx.get(f"{BASE}/repos/o/r/collaborators/u/permission").respond(200, json=payload)
    assert gh[0].get_permission("o/r", "u") == expected


@respx.mock
def test_permission_for_a_non_collaborator_is_none(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.get(f"{BASE}/repos/o/r/collaborators/stranger/permission").respond(404, json={"message": "Not Found"})
    assert gh[0].get_permission("o/r", "stranger") == "none"
    respx.get(f"{BASE}/repos/o/r/collaborators/forbidden/permission").respond(403, json={"message": "no"})
    assert gh[0].get_permission("o/r", "forbidden") == "none"


@respx.mock
def test_labels_and_comments(gh: tuple[GitHubClient, list[float]]) -> None:
    add = respx.post(f"{BASE}/repos/o/r/issues/3/labels").respond(200, json=[])
    gh[0].add_labels("o/r", 3, ["fixling:running"])
    assert add.calls[0].request.content == b'{"labels":["fixling:running"]}'
    respx.delete(f"{BASE}/repos/o/r/issues/3/labels/fixling").respond(404, json={"message": "Label does not exist"})
    gh[0].remove_label("o/r", 3, "fixling")  # already gone: not an error
    respx.post(f"{BASE}/repos/o/r/issues/3/comments").respond(
        201, json={"html_url": "https://github.com/o/r/issues/3#c1"}
    )
    assert gh[0].comment("o/r", 3, "hello").endswith("#c1")


@respx.mock
def test_ensure_labels_ignores_existing(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.post(f"{BASE}/repos/o/r/labels").mock(
        side_effect=[httpx.Response(201, json={}), httpx.Response(422, json={"message": "Validation Failed"})]
    )
    gh[0].ensure_labels("o/r", {"fixling": "0e8a16", "fixling:verified": "1d76db"})


@respx.mock
def test_create_pr_is_a_draft_and_idempotent(gh: tuple[GitHubClient, list[float]]) -> None:
    create = respx.post(f"{BASE}/repos/o/r/pulls").mock(
        side_effect=[
            httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7"}),
            httpx.Response(
                422, json={"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]}
            ),
        ]
    )
    pr = gh[0].create_pr("o/r", "fixling/issue-3-abc", "main", "title", "body")
    assert pr["number"] == 7 and b'"draft":true' in create.calls[0].request.content
    respx.get(f"{BASE}/repos/o/r/pulls").respond(200, json=[{"number": 7, "html_url": "https://github.com/o/r/pull/7"}])
    again = gh[0].create_pr("o/r", "fixling/issue-3-abc", "main", "title", "body")
    assert again["number"] == 7


@respx.mock
def test_primary_rate_limit_waits_until_reset(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.get(f"{BASE}/repos/o/r").mock(
        side_effect=[
            httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}, json={"message": "rate limit"}
            ),
            httpx.Response(200, json={"default_branch": "trunk"}),
        ]
    )
    client, sleeps = gh
    assert client.get_default_branch("o/r") == "trunk"
    assert sleeps == [31.0]  # reset (1030) - now (1000) + 1


@respx.mock
def test_secondary_rate_limit_honours_retry_after(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.get(f"{BASE}/repos/o/r").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json={}),
        ]
    )
    client, sleeps = gh
    client.get_default_branch("o/r")
    assert sleeps == [7.0]


@respx.mock
def test_absurd_rate_limit_waits_are_refused(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.get(f"{BASE}/repos/o/r").respond(429, headers={"retry-after": "3600"}, json={"message": "later"})
    with pytest.raises(GitHubError, match="rate limit"):
        gh[0].get_default_branch("o/r")


@respx.mock
def test_server_errors_are_retried_then_raised(gh: tuple[GitHubClient, list[float]]) -> None:
    route = respx.get(f"{BASE}/repos/o/r").respond(502, json={"message": "bad gateway"})
    with pytest.raises(GitHubError, match="502"):
        gh[0].get_default_branch("o/r")
    assert route.call_count == 4 and gh[1] == [1.0, 2.0, 4.0]


@respx.mock
def test_errors_never_contain_the_token(gh: tuple[GitHubClient, list[float]]) -> None:
    respx.get(f"{BASE}/repos/o/r/issues/1").respond(401, json={"message": "Bad credentials"})
    with pytest.raises(GitHubError) as ei:
        gh[0].get_issue("o/r", 1)
    assert TOKEN not in str(ei.value) and ei.value.status == 401
    respx.get(f"{BASE}/repos/o/r/issues/2").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(GitHubError) as ei2:
        gh[0].get_issue("o/r", 2)
    assert TOKEN not in str(ei2.value)
