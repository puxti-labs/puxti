from abc import ABC, abstractmethod

from puxti.models import Edge, Entity, FileDiff, SemanticChangeEvent


class BaseConnector(ABC):
    """Standard interface all connectors must implement.

    The core never calls connector internals directly — only through this interface.
    Connectors are isolated modules: no connector knows about another connector.
    """

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    async def health_check(self) -> bool:
        """Verify connection and required permissions."""

    @abstractmethod
    async def extract_entities(self) -> list[Entity]:
        """Read all entities owned by this connector.
        For warehouse connectors: tables and columns.
        For dbt: models and their columns.
        For Airflow: DAGs and tasks.
        """

    @abstractmethod
    async def extract_lineage(self) -> list[Edge]:
        """Read the dependency graph for entities in this connector."""

    @abstractmethod
    async def generate_changes(self, event: SemanticChangeEvent) -> tuple[list[FileDiff], list[str]]:
        """Given a semantic change event, generate required file changes.

        Returns (diffs, unverified_entity_ids).
        - diffs: file changes to apply
        - unverified_entity_ids: entities that were potentially affected but
          could not be safely propagated (e.g. transitive dependents whose
          column may come from a different upstream source). Must be reviewed
          manually.

        MUST NOT write anything — returns diffs only.
        Warehouse connectors must return ([], []).
        """

    def supports_change_type(self, change_type: str) -> bool:
        """Override to restrict which change types this connector handles."""
        return True
