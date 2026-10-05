"""Read-only configuration, migration, schema and static release checks."""
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import storages
from django.db import DatabaseError, connections, models
from django.db.migrations.exceptions import (
    BadMigrationError, CircularDependencyError, InconsistentMigrationHistory,
    InvalidMigrationPlan, NodeNotFoundError,
)
from django.db.migrations.executor import MigrationExecutor

from mysite.configuration import validate_production
from mysite.runtime import validate_runtime


FAILURES = {
    "configuration_invalid": "Effective application configuration is invalid.",
    "database_unavailable": "The release database could not be inspected.",
    "migrations_pending": "Required migrations are unapplied.",
    "migration_history_invalid": "Migration history or dependencies are inconsistent.",
    "schema_incomplete": "Required physical schema is missing or inconsistent.",
    "static_unavailable": "Collected static assets or their manifest are incomplete.",
}
STATIC_ASSETS = (
    "css/style.css", "images/proposallogo.png", "admin/css/base.css", "admin/js/core.js",
)


class OperationalFailure(Exception):
    """Only bounded categories and fixed messages leave the inspection boundary."""
    def __init__(self, category):
        self.category = category if category in FAILURES else "schema_incomplete"
        super().__init__(FAILURES[self.category])


def check_configuration():
    try:
        if settings.APP_ENV not in ("development", "production"):
            raise ImproperlyConfigured()
        if type(settings.DEBUG) is not bool or not settings.SECRET_KEY.strip():
            raise ImproperlyConfigured()
        validate_production(settings)
        validate_runtime(settings.DEPLOYMENT_RUNTIME)
    except (ImproperlyConfigured, AttributeError, KeyError, TypeError, ValueError):
        raise OperationalFailure("configuration_invalid") from None


def _physical_schema(database):
    """Check managed tables, columns and required key/index/constraint metadata."""
    with database.cursor() as cursor:
        tables = set(database.introspection.table_names(cursor))
        for model in apps.get_models(include_auto_created=True):
            meta = model._meta
            if not meta.managed or meta.proxy or meta.swapped:
                continue
            table = meta.db_table
            if table not in tables:
                raise OperationalFailure("schema_incomplete")
            columns = {item.name for item in database.introspection.get_table_description(cursor, table)}
            fields = meta.local_concrete_fields
            if not {field.column for field in fields}.issubset(columns):
                raise OperationalFailure("schema_incomplete")
            constraints = database.introspection.get_constraints(cursor, table)
            for field in fields:
                matching = [item for item in constraints.values() if item["columns"] == [field.column]]
                if field.primary_key and not any(item["primary_key"] for item in matching):
                    raise OperationalFailure("schema_incomplete")
                if field.unique and not field.primary_key and not any(item["unique"] for item in matching):
                    raise OperationalFailure("schema_incomplete")
                if field.is_relation and getattr(field, "db_constraint", False):
                    target = (field.target_field.model._meta.db_table, field.target_field.column)
                    if not any(item["foreign_key"] == target for item in matching):
                        raise OperationalFailure("schema_incomplete")
            for constraint in meta.constraints:
                actual = constraints.get(constraint.name, {})
                if isinstance(constraint, models.CheckConstraint) and not actual.get("check"):
                    raise OperationalFailure("schema_incomplete")
                if isinstance(constraint, models.UniqueConstraint):
                    expected = [meta.get_field(name).column for name in constraint.fields]
                    if not actual.get("unique") or actual.get("columns") != expected:
                        raise OperationalFailure("schema_incomplete")
            for index in meta.indexes:
                actual = constraints.get(index.name, {})
                expected = [meta.get_field(name.lstrip("-")).column for name in index.fields]
                if not actual.get("index") or actual.get("columns") != expected:
                    raise OperationalFailure("schema_incomplete")
            # Exercise actual column selection/permissions without reading rows.
            quoted = ", ".join(database.ops.quote_name(field.column) for field in fields)
            cursor.execute(f"SELECT {quoted} FROM {database.ops.quote_name(table)} LIMIT 0")


def check_readiness():
    """Never call migrate(), ensure_schema(), recovery or any provider."""
    check_configuration()
    database = connections["default"]
    try:
        with database.cursor() as cursor:
            cursor.execute("SELECT 1")
        executor = MigrationExecutor(database)
        executor.loader.check_consistent_history(database)
        if executor.loader.detect_conflicts():
            raise OperationalFailure("migration_history_invalid")
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise OperationalFailure("migrations_pending")
        _physical_schema(database)
    except OperationalFailure:
        raise
    except (BadMigrationError, CircularDependencyError, InconsistentMigrationHistory,
            InvalidMigrationPlan, NodeNotFoundError):
        raise OperationalFailure("migration_history_invalid") from None
    except DatabaseError:
        raise OperationalFailure("database_unavailable") from None
    except Exception:
        # Unexpected inspection failures must not become a public debug traceback.
        raise OperationalFailure("schema_incomplete") from None


def check_static():
    """Inspect a fresh local manifest; do not collect assets or trust cached paths."""
    try:
        definition = settings.STORAGES["staticfiles"]
        if (not settings.STATIC_ROOT or definition.get("BACKEND") !=
                "whitenoise.storage.CompressedManifestStaticFilesStorage"):
            raise OperationalFailure("static_unavailable")
        storage = storages.create_storage(definition)
        if not storage.manifest_storage.exists(storage.manifest_name):
            raise OperationalFailure("static_unavailable")
        for asset in STATIC_ASSETS:
            stored = storage.stored_name(asset)
            if not storage.exists(stored):
                raise OperationalFailure("static_unavailable")
    except OperationalFailure:
        raise
    except Exception:
        # Malformed manifest structures and paths must also fail safely.
        raise OperationalFailure("static_unavailable") from None


def check_release():
    check_readiness()
    check_static()
