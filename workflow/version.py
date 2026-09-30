"""Single source of truth for workflow and cache format versions."""

from typing import Final, Literal, TypeAlias

SnapshotSchemaVersion: TypeAlias = Literal[1]
PlanVersion: TypeAlias = Literal[13]

CONFIG_SCHEMA_VERSION: Final[int] = 1
SNAPSHOT_SCHEMA_VERSION: Final[SnapshotSchemaVersion] = 1
PLAN_VERSION: Final[PlanVersion] = 13

# Cache paths keep their own format identifiers. Changing the plan schema does
# not silently invalidate source objects, while the work namespace is advanced
# when answer/session artifacts must not be restored.
SOURCE_CACHE_DIR: Final[str] = "sources-v8"
WORK_CACHE_DIR: Final[str] = "work-v11"
CACHE_NAMESPACE: Final[str] = "v11"
