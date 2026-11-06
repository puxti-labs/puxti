import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path

from puxti.models import (
    Edge,
    Entity,
    FileDiff,
    SemanticChangeEvent,
    SourceFingerprint,
    UnverifiedEntity,
)

logger = logging.getLogger(__name__)


def fingerprint_files(root: Path, files: Iterable[Path]) -> SourceFingerprint | None:
    """Fingerprint a set of input files: sha256 over each file's path relative to
    `root` and its bytes, in sorted path order, so adding, removing, renaming or
    editing any file changes the digest. None when `root` does not exist or a
    file cannot be read (the caller then rescans rather than guessing)."""
    if not root.exists():
        return None
    digest = hashlib.sha256()
    try:
        # Paths are hashed as given, not resolved: renaming a symlink changes
        # what a scan derives from the filename (an Airflow DAG's id), so it
        # must change the digest even though the target's bytes did not.
        for file in sorted(Path(f) for f in files):
            try:
                rel = file.relative_to(root).as_posix()
            except ValueError:
                rel = file.absolute().as_posix()
            digest.update(rel.encode() + b"\0")
            digest.update(hashlib.sha256(file.read_bytes()).digest())
    except OSError as exc:
        logger.warning("Cannot read %s for its scan fingerprint, will rescan: %s", root, exc)
        return None
    return SourceFingerprint(source=str(root.resolve()), digest=digest.hexdigest())


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
    async def generate_changes(
        self, event: SemanticChangeEvent
    ) -> tuple[list[FileDiff], list[UnverifiedEntity]]:
        """Given a semantic change event, generate required file changes.

        Returns (diffs, unverified_entities).
        - diffs: file changes to apply
        - unverified_entities: entities that were potentially affected but
          could not be safely propagated (e.g. transitive dependents whose
          column may come from a different upstream source). Must be reviewed
          manually. Each entry must include a reason and may include detail.

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

    def get_seed_definitions(self) -> dict[str, str]:
        """Map of entity ID → definition text the connector can state directly
        from its source, with no LLM (e.g. dbt metrics, whose YAML already says
        what they compute). Connectors with nothing to state return {}.
        """
        return {}

    def source_fingerprint(self) -> SourceFingerprint | None:
        """Content hash of every input this connector reads during a scan, for
        `puxti scan --if-changed`. None means "cannot tell", and the scan always
        runs. Override only when the hash covers everything extract_entities and
        extract_lineage read: a missed input means a skipped rescan.
        """
        return None

    def find_model_path(self, entity_id: str) -> str | None:
        """Repo-relative file path of an entity's source file, or None when
        this connector cannot locate one. Engines must treat None as
        "skip this entity", never as an error.
        """
        return None
