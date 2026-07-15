from abc import ABC, abstractmethod

from puxti.models import Edge, Entity, FileDiff, SemanticChangeEvent


class BaseConnector(ABC):
    """Standard interface all connectors must implement.

    The core never calls connector internals directly — only through this interface.
    Connectors are isolated modules: no connector knows about another connector.
    """

    #: Connector name as used in .puxti.yml, Entity.source_connector, and
    #: FileDiff.connector. Producer connectors must override this.
    name: str = ""

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

    # ── Optional capabilities — producer connectors override what they support ──

    def get_project_name(self) -> str:
        """Human-readable namespace for the entities this connector owns.

        Used to detect cross-project changes. Connectors without a project
        concept return "".
        """
        return ""

    def get_model_sql_map(self) -> dict[str, str]:
        """Map of entity ID → raw source text for entities this connector can
        patch. Engines use it to give the LLM full context when generating
        definitions and diffs. Connectors with no patchable sources return {}.
        """
        return {}

    def find_model_path(self, entity_id: str) -> str | None:
        """Repo-relative file path of an entity's source file, or None when
        this connector cannot locate one. Engines must treat None as
        "skip this entity", never as an error.
        """
        return None
