"""`puxti redefine` — validation, dependents, dry-run, PR-failure regression."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from puxti.cli import app
from tests.cli._helpers import plain, runner

# ── redefine ──────────────────────────────────────────────────────────────────

def test_redefine_shows_help():
    result = runner.invoke(app, ["redefine", "--help"])
    assert result.exit_code == 0
    assert "--entity" in plain(result.output)
    assert "--description" in plain(result.output)
    assert "--repo" in plain(result.output)


def test_redefine_exits_when_no_dbt_project_dir():
    with patch("puxti.cli.redefine.settings") as mock_settings:
        mock_settings.dbt_project_dir = None
        mock_settings.github_token = "ghp_test"
        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])
    assert result.exit_code == 1


def test_redefine_exits_when_no_github_token():
    with patch("puxti.cli.redefine.settings") as mock_settings:
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = None
        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])
    assert result.exit_code == 1
    assert "github token" in result.output.lower() or "GITHUB_TOKEN" in result.output


def test_redefine_exits_cleanly_when_no_dependents_at_all():
    """No semantic and no structural dependents — should exit 0 with a message."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ])

    assert result.exit_code == 0
    assert "nothing" in result.output.lower() or "no dependents" in result.output.lower()


def test_redefine_falls_back_to_structural_dependents():
    """No semantic dependents but structural ones exist — should surface them."""

    structural_dep = MagicMock()
    structural_dep.id = "model.demo_shop.reports"
    structural_dep.name = "reports"
    structural_dep.type = MagicMock(value="model")

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[])
    mock_graph.get_structural_dependents = AsyncMock(return_value=[structural_dep])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    from puxti.models import FileDiff, PropagationResult

    real_diff = FileDiff(
        file_path="models/reports.sql",
        before="select * from customers",
        after="-- PUXTI [manual review required]\nselect * from customers",
        connector="dbt",
        description="manual review",
    )

    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    mock_redefiner.generate_diffs = AsyncMock(return_value=[real_diff])

    pr_result = PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.demo_shop.orders.gross_revenue",
        diffs=[real_diff],
        pr_url="https://github.com/acme/data/pull/9",
        status="opened",
    )

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=pr_result)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.redefine.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ], input="y\n")

    assert result.exit_code == 0, result.output
    assert "reports" in result.output
    assert "structural" in result.output.lower() or "lineage" in result.output.lower()


def test_redefine_happy_path_prints_pr_url():
    from puxti.models import (
        FileDiff,
        PropagationResult,
    )

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[
        (
            MagicMock(
                id="model.demo_shop.customers", name="customers", type=MagicMock(value="model"),
            ),
            1,
        ),
    ])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    diff = FileDiff(
        file_path="models/marts/customers.sql",
        before="select * from orders",
        after="-- PUXTI [high confidence]\nselect * from orders where not is_refund",
        connector="dbt",
        description="Semantic redefinition",
    )

    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    mock_redefiner.generate_diffs = AsyncMock(return_value=[diff])

    pr_result = PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.demo_shop.orders.gross_revenue",
        diffs=[diff],
        pr_url="https://github.com/acme/data/pull/7",
        status="opened",
    )
    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=pr_result)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.redefine.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ], input="y\n")

    assert result.exit_code == 0, result.output
    assert "https://github.com/acme/data/pull/7" in result.output
    mock_graph.upsert_definition.assert_awaited_once()
    mock_graph.save_change_event.assert_awaited_once()


def _run_redefine_with_diff(
    *, extra_args=(), input_="", repo_args=("--repo", "acme/data"), github_token="ghp_test",
    before="select * from orders",
    after="-- PUXTI [high confidence]\nselect * from orders where not is_refund",
    ancestors=(), extra_sql=None, passthrough_diffs=(),
):
    """Run `puxti redefine` with one hop-1 dependent whose generated diff is `before` → `after`.

    `ancestors` are (entity, depth) upstream models, `extra_sql` adds entries to the
    dbt SQL map, and `passthrough_diffs` is what the passthrough step returns.

    Returns (cli_result, graph_mock, redefiner_mock, github_class_mock, github_mock).
    """
    from puxti.models import FileDiff, PropagationResult

    dep = MagicMock(id="model.demo_shop.customers", type=MagicMock(value="model"))
    dep.name = "customers"

    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[(dep, 1)])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=list(ancestors))
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    mock_dbt = MagicMock()
    mock_dbt.get_model_sql_map = MagicMock(return_value={dep.id: before, **(extra_sql or {})})

    mock_backend = MagicMock()
    mock_backend.count_input_tokens = AsyncMock(return_value=MagicMock(tokens=300, exact=True))
    mock_backend.input_cost_per_mtok = 3.0
    mock_backend.output_cost_per_mtok = 15.0

    diff = FileDiff(
        file_path="models/marts/customers.sql",
        before=before,
        after=after,
        connector="dbt",
        description="Semantic redefinition",
    )
    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=list(passthrough_diffs))
    mock_redefiner.generate_diffs = AsyncMock(return_value=[diff])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(return_value=PropagationResult(
        change_event_id="evt-001",
        connector="dbt",
        target_entity_id="model.demo_shop.orders.gross_revenue",
        diffs=[diff],
        pr_url="https://github.com/acme/data/pull/7",
        status="opened",
    ))
    gh_cls = MagicMock(return_value=mock_gh)

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", gh_cls),
        patch("puxti.cli.redefine.DbtConnector", return_value=mock_dbt),
        patch("puxti.cli.redefine.get_backend", return_value=mock_backend),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = github_token

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            *repo_args,
            *extra_args,
        ], input=input_)

    result.backend = mock_backend
    return result, mock_graph, mock_redefiner, gh_cls, mock_gh


def test_redefine_dry_run_shows_estimate_then_diffs_and_writes_nothing():
    result, graph, redefiner, gh_cls, mock_gh = _run_redefine_with_diff(extra_args=("--dry-run",))

    assert result.exit_code == 0, result.output
    output = plain(result.output)
    estimate_at = output.index("Dry run — cost estimate")
    assert "300" in output
    assert estimate_at < output.index("+select * from orders where not is_refund")
    assert "--- a/models/marts/customers.sql" in output
    assert "1 pull request would be opened. Nothing was written." in output
    assert "Open 1 pull request?" not in output
    redefiner.generate_diffs.assert_awaited_once()
    graph.upsert_definition.assert_not_called()
    graph.save_change_event.assert_not_called()
    gh_cls.assert_not_called()  # not even the write-access health check
    mock_gh.open_pr.assert_not_called()


def test_redefine_dry_run_needs_no_repo_or_github_token():
    result, _, _, gh_cls, _ = _run_redefine_with_diff(
        extra_args=("--dry-run",), repo_args=(), github_token=None,
    )

    assert result.exit_code == 0, result.output
    assert "+select * from orders where not is_refund" in plain(result.output)
    gh_cls.assert_not_called()


@pytest.mark.parametrize("answer", ["n", "", "yes please"])
def test_redefine_declining_pr_confirmation_writes_nothing(answer):
    result, graph, _, _, mock_gh = _run_redefine_with_diff(input_=f"{answer}\n")

    assert result.exit_code == 0, result.output
    assert "No PR opened" in plain(result.output)
    mock_gh.open_pr.assert_not_called()
    graph.upsert_definition.assert_not_called()
    graph.save_change_event.assert_not_called()


def test_redefine_previews_diff_as_literal_text_before_confirming():
    result, graph, _, _, mock_gh = _run_redefine_with_diff(
        input_="y\n",
        before="select '[red]' as label from orders",
        after="-- PUXTI [high confidence]\nselect '[red]' as label from orders where not is_refund",
    )

    assert result.exit_code == 0, result.output
    output = plain(result.output)
    prompt_at = output.index("Open 1 pull request?")
    assert output.index("-select '[red]' as label from orders") < prompt_at
    assert output.index("+-- PUXTI [high confidence]") < prompt_at
    mock_gh.open_pr.assert_awaited_once()
    graph.upsert_definition.assert_awaited_once()


def test_redefine_dry_run_estimate_counts_passthrough_calls():
    """The estimate counts every call the dry run then makes: one passthrough call
    for the entity and one per upstream model with SQL, plus the hop-1 semantic one."""
    upstream = MagicMock(id="model.demo_shop.stg_orders", type=MagicMock(value="model"))
    upstream.name = "stg_orders"
    result, graph, redefiner, _, _ = _run_redefine_with_diff(
        extra_args=("--dry-run",),
        ancestors=[(upstream, 1)],
        extra_sql={
            "model.demo_shop.orders.gross_revenue": "select gross_revenue from stg_orders",
            upstream.id: "select * from raw_orders",
        },
    )

    assert result.exit_code == 0, result.output
    output = plain(result.output)
    assert "3  (2 upstream passthrough, 1 downstream hop 1–2)" in output
    assert "900" in output  # 3 calls × 300 tokens
    assert result.backend.count_input_tokens.await_count == 3

    # The passthrough prompts that were counted are the ones sent: built once,
    # with one definition read per target, and handed to generation as-is.
    counted = [c.args[0] for c in result.backend.count_input_tokens.await_args_list[:2]]
    sent = redefiner.generate_passthrough_diffs.await_args.kwargs["prompts"]
    assert [message for _, _, _, message in sent] == counted
    assert graph.get_latest_definition.await_count == 1 + len(sent)  # existing + per target


def test_redefine_preview_labels_upstream_and_downstream_diffs():
    from puxti.models import FileDiff

    passthrough = FileDiff(
        file_path="models/staging/stg_orders.sql",
        before="select id from raw_orders",
        after="-- PUXTI [passthrough — upstream hop 1]\nselect id, refund from raw_orders",
        connector="dbt",
        description="Pass through refund",
    )
    result, _, _, _, _ = _run_redefine_with_diff(
        extra_args=("--dry-run",), passthrough_diffs=[passthrough],
    )

    assert result.exit_code == 0, result.output
    output = plain(result.output)
    upstream_at = output.index("upstream passthrough diffs:")
    downstream_at = output.index("downstream semantic diffs:")
    assert upstream_at < output.index("+select id, refund from raw_orders") < downstream_at
    assert downstream_at < output.index("+select * from orders where not is_refund")


def test_redefine_closed_input_at_pr_prompt_cancels_cleanly():
    result, graph, _, _, mock_gh = _run_redefine_with_diff(input_="")

    assert result.exit_code == 0, result.output
    assert "No PR opened" in plain(result.output)
    assert "Unexpected error" not in result.output
    mock_gh.open_pr.assert_not_called()
    graph.upsert_definition.assert_not_called()


# ── regression: redefine PR failure leaves graph unchanged ────────────────────

def test_redefine_pr_failure_leaves_graph_unchanged():
    """If open_pr() raises, upsert_definition and save_change_event must not have been called."""
    mock_graph = MagicMock()
    mock_graph.connect = AsyncMock()
    mock_graph.close = AsyncMock()
    mock_graph.get_latest_definition = AsyncMock(return_value=None)
    mock_graph.get_semantic_dependents_with_depth = AsyncMock(return_value=[
        (
            MagicMock(
                id="model.demo_shop.customers", name="customers", type=MagicMock(value="model"),
            ),
            1,
        ),
    ])
    mock_graph.get_structural_ancestors = AsyncMock(return_value=[])
    mock_graph.upsert_definition = AsyncMock()
    mock_graph.save_change_event = AsyncMock()

    from puxti.models import FileDiff
    diff = FileDiff(
        file_path="models/customers.sql",
        before="select * from orders",
        after="-- PUXTI [high confidence]\nselect * from orders where not is_refund",
        connector="dbt",
        description="Semantic redefinition",
    )
    mock_redefiner = MagicMock()
    mock_redefiner.generate_passthrough_diffs = AsyncMock(return_value=[])
    mock_redefiner.generate_diffs = AsyncMock(return_value=[diff])

    mock_gh = MagicMock()
    mock_gh.health_check = AsyncMock(return_value=True)
    mock_gh.open_pr = AsyncMock(side_effect=RuntimeError("GitHub API unavailable"))

    with (
        patch("puxti.cli.redefine.settings") as mock_settings,
        patch("puxti.cli.redefine.KnowledgeGraph", return_value=mock_graph),
        patch("puxti.cli.redefine.SemanticRedefiner", return_value=mock_redefiner),
        patch("puxti.cli.redefine.GitHubConnector", return_value=mock_gh),
        patch("puxti.cli.redefine.DbtConnector"),
    ):
        mock_settings.dbt_project_dir = "/some/dbt"
        mock_settings.github_token = "ghp_test"

        result = runner.invoke(app, [
            "redefine",
            "--entity", "model.demo_shop.orders.gross_revenue",
            "--description", "now excludes refunds",
            "--repo", "acme/data",
        ], input="y\n")

    assert result.exit_code == 1
    mock_gh.open_pr.assert_awaited_once()
    mock_graph.upsert_definition.assert_not_called()
    mock_graph.save_change_event.assert_not_called()
