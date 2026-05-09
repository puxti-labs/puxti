from puxti.connectors.base import BaseConnector
from puxti.models import PropagationResult, SemanticChangeEvent


class PropagationEngine:
    """Orchestrates change propagation across all registered connectors.

    Flow:
    1. Receive a SemanticChangeEvent from the capture step
    2. For each registered connector, call generate_changes()
    3. Collect non-empty diffs into PropagationResult objects
    4. Return results — one per connector that produced changes

    The engine never writes anything. Each PropagationResult holds file diffs
    that are handed to the PR creator (GitHub connector or similar).
    """

    def __init__(self, connectors: list[BaseConnector]) -> None:
        self._connectors = connectors

    async def propagate(self, event: SemanticChangeEvent) -> list[PropagationResult]:
        """Run propagation across all connectors for the given event.

        Args:
            event: A SemanticChangeEvent produced by SemanticCapture.

        Returns:
            One PropagationResult per connector that produced diffs.
            Empty list if no connector has changes to make.
        """
        results: list[PropagationResult] = []

        for connector in self._connectors:
            diffs, unverified = await connector.generate_changes(event)
            if not diffs and not unverified:
                continue

            results.append(
                PropagationResult(
                    change_event_id=event.change_event_id,
                    connector=diffs[0].connector if diffs else "dbt",
                    target_entity_id=event.entity_id,
                    diffs=diffs,
                    unverified_entity_ids=unverified,
                )
            )

        return results
