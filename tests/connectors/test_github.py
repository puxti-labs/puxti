import base64
import re

import pytest

from puxti.connectors.github import GitHubConnector, _companion_section, _pr_body, _pr_title, _needs_attention, _attention_reason
from puxti.models import ChangeType, FileDiff, PropagationResult, SemanticChangeEvent


# ── Fixtures ──────────────────────────────────────────────────────────────────

REPO = "acme/data"
TOKEN = "ghp_test"
BASE_SHA = "abc123def456"
FILE_SHA = "deadbeef1234"
PR_URL = "https://github.com/acme/data/pull/42"

EVENT = SemanticChangeEvent(
    change_event_id="evt-aabbccdd-1234",
    entity_id="model.jaffle_shop.orders.order_date",
    change_type=ChangeType.STRUCTURAL,
    semantic_context="order_date renamed to recorded_date to clarify it is the date the order was recorded, not the transaction date.",
    affected_entity_ids=["model.jaffle_shop.orders", "model.jaffle_shop.revenue"],
    reasoning="revenue references order_date via date_trunc — the rename cascades.",
    change={"before": {"name": "order_date"}, "after": {"name": "recorded_date"}},
)

ORDERS_DIFF = FileDiff(
    file_path="models/orders.sql",
    before="select order_date from stg_orders",
    after="select recorded_date from stg_orders",
    connector="dbt",
    description="Renamed order_date → recorded_date in orders",
)

REVENUE_DIFF = FileDiff(
    file_path="models/revenue.sql",
    before="select date_trunc('month', order_date) as month from orders",
    after="select date_trunc('month', recorded_date) as month from orders",
    connector="dbt",
    description="Renamed order_date → recorded_date in revenue",
)

RESULT = PropagationResult(
    change_event_id=EVENT.change_event_id,
    connector="dbt",
    target_entity_id=EVENT.entity_id,
    diffs=[ORDERS_DIFF, REVENUE_DIFF],
)

BRANCH = f"puxti/{EVENT.change_event_id[:8]}"


@pytest.fixture
def connector(httpx_mock) -> GitHubConnector:
    import httpx

    client = httpx.AsyncClient()
    return GitHubConnector(
        config={"repo": REPO, "token": TOKEN},
        client=client,
    )


# ── health_check ──────────────────────────────────────────────────────────────

async def test_health_check_passes_when_repo_accessible(httpx_mock, connector):
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}",
        status_code=200,
        json={"full_name": REPO, "permissions": {"push": True, "pull": True}},
    )
    assert await connector.health_check() is True


async def test_health_check_fails_when_no_push_permission(httpx_mock, connector):
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}",
        status_code=200,
        json={"full_name": REPO, "permissions": {"push": False, "pull": True}},
    )
    assert await connector.health_check() is False


async def test_health_check_fails_when_token_invalid(httpx_mock, connector):
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}",
        status_code=401,
        json={"message": "Bad credentials"},
    )
    assert await connector.health_check() is False


async def test_health_check_fails_when_repo_not_found(httpx_mock, connector):
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}",
        status_code=404,
        json={"message": "Not Found"},
    )
    assert await connector.health_check() is False


# ── open_pr — happy path ───────────────────────────────────────────────────────

def _register_happy_path(httpx_mock, *, file_exists: bool = True) -> None:
    """Register all GitHub API calls for a successful open_pr flow."""
    # 1. Get base branch SHA
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}/git/ref/heads/main",
        json={"object": {"sha": BASE_SHA}},
    )
    # 2. Create feature branch
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.github.com/repos/{REPO}/git/refs",
        status_code=201,
        json={"ref": f"refs/heads/{BRANCH}"},
    )
    # 3a. Get orders.sql SHA — URL has ?ref=<branch> query param, match by pattern
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"https://api\.github\.com/repos/{re.escape(REPO)}/contents/models/orders\.sql"),
        json={"sha": FILE_SHA} if file_exists else {},
        status_code=200 if file_exists else 404,
    )
    # 3b. Upsert orders.sql
    httpx_mock.add_response(
        method="PUT",
        url=f"https://api.github.com/repos/{REPO}/contents/models/orders.sql",
        status_code=200,
        json={},
    )
    # 3c. Get revenue.sql SHA
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"https://api\.github\.com/repos/{re.escape(REPO)}/contents/models/revenue\.sql"),
        json={"sha": FILE_SHA} if file_exists else {},
        status_code=200 if file_exists else 404,
    )
    # 3d. Upsert revenue.sql
    httpx_mock.add_response(
        method="PUT",
        url=f"https://api.github.com/repos/{REPO}/contents/models/revenue.sql",
        status_code=200,
        json={},
    )
    # 4. Create PR
    httpx_mock.add_response(
        method="POST",
        url=f"https://api.github.com/repos/{REPO}/pulls",
        status_code=201,
        json={"html_url": PR_URL},
    )


async def test_open_pr_returns_result_with_pr_url(httpx_mock, connector):
    _register_happy_path(httpx_mock)
    result = await connector.open_pr(RESULT.model_copy(), EVENT)
    assert result.pr_url == PR_URL


async def test_open_pr_sets_status_opened(httpx_mock, connector):
    _register_happy_path(httpx_mock)
    result = await connector.open_pr(RESULT.model_copy(), EVENT)
    assert result.status == "opened"


async def test_open_pr_creates_branch_from_base(httpx_mock, connector):
    _register_happy_path(httpx_mock)
    await connector.open_pr(RESULT.model_copy(), EVENT)

    create_ref_request = next(
        r for r in httpx_mock.get_requests()
        if r.method == "POST" and "/git/refs" in str(r.url)
    )
    body = create_ref_request.read()
    import json
    payload = json.loads(body)
    assert payload["ref"] == f"refs/heads/{BRANCH}"
    assert payload["sha"] == BASE_SHA


async def test_open_pr_commits_correct_content(httpx_mock, connector):
    _register_happy_path(httpx_mock)
    await connector.open_pr(RESULT.model_copy(), EVENT)

    put_requests = [
        r for r in httpx_mock.get_requests()
        if r.method == "PUT"
    ]
    assert len(put_requests) == 2

    import json
    orders_put = next(r for r in put_requests if "orders.sql" in str(r.url))
    payload = json.loads(orders_put.read())
    decoded = base64.b64decode(payload["content"]).decode()
    assert decoded == ORDERS_DIFF.after
    assert payload["branch"] == BRANCH
    assert payload["sha"] == FILE_SHA  # existing file — SHA must be included


async def test_open_pr_new_file_omits_sha(httpx_mock, connector):
    """When a file doesn't exist yet, PUT payload must not include sha."""
    _register_happy_path(httpx_mock, file_exists=False)
    await connector.open_pr(RESULT.model_copy(), EVENT)

    import json
    put_requests = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    for req in put_requests:
        payload = json.loads(req.read())
        assert "sha" not in payload


async def test_open_pr_pr_title_and_body(httpx_mock, connector):
    _register_happy_path(httpx_mock)
    await connector.open_pr(RESULT.model_copy(), EVENT)

    import json
    pr_request = next(
        r for r in httpx_mock.get_requests()
        if r.method == "POST" and "/pulls" in str(r.url)
    )
    payload = json.loads(pr_request.read())
    assert payload["title"] == "rename order_date → recorded_date"
    assert "models/orders.sql" in payload["body"]
    assert "models/revenue.sql" in payload["body"]
    assert EVENT.change_event_id in payload["body"]
    assert payload["base"] == "main"
    assert payload["head"] == BRANCH


async def test_open_pr_custom_base_branch(httpx_mock):
    import httpx as httpx_lib

    client = httpx_lib.AsyncClient()
    connector = GitHubConnector(
        config={"repo": REPO, "token": TOKEN, "base_branch": "develop"},
        client=client,
    )

    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}/git/ref/heads/develop",
        json={"object": {"sha": BASE_SHA}},
    )
    httpx_mock.add_response(method="POST", url=f"https://api.github.com/repos/{REPO}/git/refs", status_code=201, json={})
    httpx_mock.add_response(method="GET", url=re.compile(rf"https://api\.github\.com/repos/{re.escape(REPO)}/contents/models/orders\.sql"), status_code=404, json={})
    httpx_mock.add_response(method="PUT", url=f"https://api.github.com/repos/{REPO}/contents/models/orders.sql", json={})
    httpx_mock.add_response(method="GET", url=re.compile(rf"https://api\.github\.com/repos/{re.escape(REPO)}/contents/models/revenue\.sql"), status_code=404, json={})
    httpx_mock.add_response(method="PUT", url=f"https://api.github.com/repos/{REPO}/contents/models/revenue.sql", json={})
    httpx_mock.add_response(method="POST", url=f"https://api.github.com/repos/{REPO}/pulls", status_code=201, json={"html_url": PR_URL})

    result = await connector.open_pr(RESULT.model_copy(), EVENT)
    assert result.pr_url == PR_URL

    import json
    pr_request = next(r for r in httpx_mock.get_requests() if r.method == "POST" and "/pulls" in str(r.url))
    payload = json.loads(pr_request.read())
    assert payload["base"] == "develop"


# ── _pr_title ─────────────────────────────────────────────────────────────────

def test_pr_title_uses_change_data():
    title = _pr_title(EVENT)
    assert title == "rename order_date → recorded_date"


def test_pr_title_truncates_at_70_chars():
    long_event = EVENT.model_copy(update={
        "change": {
            "before": {"name": "a_very_long_source_column_name_that_exceeds_the_limit"},
            "after": {"name": "a_very_long_target_column_name_that_exceeds_the_limit"},
        }
    })
    title = _pr_title(long_event)
    assert len(title) <= 70
    assert title.endswith("...")


def test_pr_title_falls_back_to_semantic_context_when_no_change_names():
    event = EVENT.model_copy(update={"change": {}})
    title = _pr_title(event)
    # Falls back to first sentence of semantic_context
    assert "order_date" in title or "recorded_date" in title


# ── _pr_body ──────────────────────────────────────────────────────────────────

def test_pr_body_includes_semantic_context():
    body = _pr_body(EVENT, RESULT)
    assert EVENT.semantic_context in body


def test_pr_body_does_not_include_reasoning():
    body = _pr_body(EVENT, RESULT)
    assert EVENT.reasoning not in body


def test_pr_body_lists_changed_files_under_files_changed():
    body = _pr_body(EVENT, RESULT)
    assert "## Files changed" in body
    assert "models/orders.sql" in body
    assert "models/revenue.sql" in body


def test_pr_body_includes_change_event_id():
    body = _pr_body(EVENT, RESULT)
    assert EVENT.change_event_id in body


def test_pr_body_no_descriptions_in_changed_section():
    """Clean changed files must not show any description text — just the path."""
    body = _pr_body(EVENT, RESULT)
    for line in body.splitlines():
        if "models/orders.sql" in line or "models/revenue.sql" in line:
            assert " — " not in line


def test_pr_body_attention_files_show_reason():
    """Files needing review appear under 'Requires your attention' with a short reason."""
    conflict_diff = FileDiff(
        file_path="models/marts/customers.sql",
        before="select 1",
        after="-- PUXTI [naming conflict]\nselect 1",
        connector="dbt",
        description="Naming conflict in `customers` — manual resolution required",
    )
    result = RESULT.model_copy(update={"diffs": [conflict_diff]})
    body = _pr_body(EVENT, result)

    assert "## Requires your attention" in body
    assert "models/marts/customers.sql" in body
    assert "naming conflict" in body
    assert "## Files changed" not in body  # no clean-changed files in this result


def test_pr_body_separates_changed_and_attention_files():
    """When both clean and attention diffs exist, both sections appear."""
    conflict_diff = FileDiff(
        file_path="models/marts/customers.sql",
        before="select 1",
        after="-- PUXTI [naming conflict]\nselect 1",
        connector="dbt",
        description="Naming conflict in `customers` — manual resolution required",
    )
    result = RESULT.model_copy(update={"diffs": [ORDERS_DIFF, conflict_diff]})
    body = _pr_body(EVENT, result)

    assert "## Files changed" in body
    assert "## Requires your attention" in body
    # orders.sql is clean — must be in changed, not attention
    changed_section = body.split("## Requires your attention")[0]
    assert "models/orders.sql" in changed_section
    assert "models/marts/customers.sql" not in changed_section


# ── _needs_attention / _attention_reason ─────────────────────────────────────

def test_needs_attention_naming_conflict():
    assert _needs_attention("Naming conflict in `customers` — manual resolution required")

def test_needs_attention_manual_review():
    assert _needs_attention("Semantic redefinition — hop depth 4, manual review required")

def test_needs_attention_verify_carefully():
    assert _needs_attention("Semantic redefinition — hop depth 2, verify carefully")

def test_needs_attention_false_for_clean_diffs():
    assert not _needs_attention("Renamed order_date → recorded_date in orders")
    assert not _needs_attention("Pass through `order_segment` (upstream hop 1)")
    assert not _needs_attention("Semantic redefinition — hop depth 1, high confidence")

def test_attention_reason_naming_conflict():
    assert _attention_reason("Naming conflict in `x` — manual resolution required") == "naming conflict — see inline comment"

def test_attention_reason_verify_carefully():
    assert _attention_reason("hop depth 2, verify carefully") == "LLM-generated at hop 2 — verify logic"

def test_attention_reason_manual_review():
    assert _attention_reason("hop depth 4, manual review required") == "too deep to generate SQL — review for impact"


# ── _companion_section ────────────────────────────────────────────────────────

AIRFLOW_COMPANION = ("airflow", "acme/airflow", "https://github.com/acme/airflow/pull/3")
DBT_COMPANION = ("dbt", "acme/data", "https://github.com/acme/data/pull/12")


def test_companion_section_lists_companions():
    section = _companion_section([AIRFLOW_COMPANION], this_connector="dbt")
    assert "## Related PRs" in section
    assert "acme/airflow#3" in section
    assert "https://github.com/acme/airflow/pull/3" in section


def test_companion_section_dbt_shows_airflow_role():
    section = _companion_section([AIRFLOW_COMPANION], this_connector="dbt")
    assert "annotation only" in section


def test_companion_section_airflow_shows_dbt_role():
    section = _companion_section([DBT_COMPANION], this_connector="airflow")
    assert "SQL changes" in section


def test_companion_section_dbt_merge_order():
    section = _companion_section([AIRFLOW_COMPANION], this_connector="dbt")
    assert "Airflow annotation first" in section


def test_companion_section_airflow_merge_order():
    section = _companion_section([DBT_COMPANION], this_connector="airflow")
    assert "merge this annotation first" in section


def test_companion_section_multiple_companions():
    section = _companion_section([AIRFLOW_COMPANION, DBT_COMPANION], this_connector="dbt")
    assert "acme/airflow#3" in section
    assert "acme/data#12" in section


# ── _pr_body with companions ──────────────────────────────────────────────────

def test_pr_body_includes_companion_section():
    body = _pr_body(EVENT, RESULT, companions=[AIRFLOW_COMPANION])
    assert "## Related PRs" in body
    assert "acme/airflow#3" in body


def test_pr_body_no_companion_section_when_none():
    body = _pr_body(EVENT, RESULT, companions=None)
    assert "## Related PRs" not in body


def test_pr_body_companion_appears_before_footer():
    body = _pr_body(EVENT, RESULT, companions=[AIRFLOW_COMPANION])
    companion_pos = body.index("## Related PRs")
    footer_pos = body.index("---\n*Generated by Puxti")
    assert companion_pos < footer_pos


# ── add_companion_note ────────────────────────────────────────────────────────

AIRFLOW_PR_URL = "https://github.com/acme/airflow/pull/3"
EXISTING_BODY = "## What changed\nsome context\n\n---\n*Generated by Puxti — change ID: `evt-123`*"


async def test_add_companion_note_patches_pr(httpx_mock, connector):
    import json

    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}/pulls/3",
        json={"body": EXISTING_BODY},
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"https://api.github.com/repos/{REPO}/pulls/3",
        status_code=200,
        json={"html_url": AIRFLOW_PR_URL},
    )

    await connector.add_companion_note(AIRFLOW_PR_URL, [DBT_COMPANION], this_connector="airflow")

    patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
    payload = json.loads(patch_req.read())
    assert "## Related PRs" in payload["body"]
    assert "acme/data#12" in payload["body"]


async def test_add_companion_note_inserts_before_footer(httpx_mock, connector):
    import json

    httpx_mock.add_response(
        method="GET",
        url=f"https://api.github.com/repos/{REPO}/pulls/3",
        json={"body": EXISTING_BODY},
    )
    httpx_mock.add_response(
        method="PATCH",
        url=f"https://api.github.com/repos/{REPO}/pulls/3",
        status_code=200,
        json={"html_url": AIRFLOW_PR_URL},
    )

    await connector.add_companion_note(AIRFLOW_PR_URL, [DBT_COMPANION], this_connector="airflow")

    patch_req = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
    payload = json.loads(patch_req.read())
    body = payload["body"]
    companion_pos = body.index("## Related PRs")
    footer_pos = body.index("---\n*Generated by Puxti")
    assert companion_pos < footer_pos


async def test_open_pr_includes_companions_in_body(httpx_mock, connector):
    import json
    _register_happy_path(httpx_mock)

    result = await connector.open_pr(RESULT.model_copy(), EVENT, companions=[AIRFLOW_COMPANION])

    pr_req = next(r for r in httpx_mock.get_requests() if r.method == "POST" and "/pulls" in str(r.url))
    payload = json.loads(pr_req.read())
    assert "## Related PRs" in payload["body"]
    assert "acme/airflow#3" in payload["body"]
