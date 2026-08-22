"""Alembic machinery shared by the services that own a schema (FR-BE-26).

Four repos own an Alembic history — `mib-identity`, `mib-ai`, `mib-ingestion`,
`mib-rag` — and their `migrations/env.py` files were byte-identical except for
which schema and version table they name. That duplication is what this replaces:
an `env.py` becomes

    from mib_shared.migrations import run_migrations
    from app.tables import metadata

    run_migrations(metadata=metadata)

Mechanics only (§8.3). Nothing here knows what a user, a regulation or a
subscription is — it holds naming rules and Alembic options. **Table definitions
must never move here.** They are domain, and putting them in a shared library
would make every schema change a shared release plus a pin bump in seven repos.

**Import this module explicitly; `mib_shared/__init__.py` does not.** It needs
SQLAlchemy and Alembic, which the three services with no migrations do not
install. Declare `mib-shared[migrations]` when you use it.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy import MetaData

# Patterns that match what PostgreSQL names things itself: `users_pkey`,
# `sso_sessions_user_id_fkey`, `plans_code_key`. Two consequences, both wanted:
#
# - Adopting metadata in a service with existing hand-written DDL renames
#   nothing, so a regenerated baseline produces the identical schema.
# - A constraint added later without an explicit name still gets a name Alembic
#   knows, so a future migration can drop it. Without a convention SQLAlchemy
#   leaves it unnamed, and an unnamed constraint cannot be dropped by name.
NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "fk": "%(table_name)s_%(column_0_N_name)s_fkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s_check",
    "ix": "ix_%(column_0_N_label)s",
}

DEFAULT_VERSION_TABLE = "alembic_version"


def schema_metadata(schema: str) -> MetaData:
    """A `MetaData` bound to one schema, carrying the shared naming convention."""
    from sqlalchemy import MetaData

    return MetaData(schema=schema, naming_convention=NAMING_CONVENTION)


def own_tables_only(metadata: MetaData) -> Callable[[str | None, str, dict], bool]:
    """An Alembic `include_name` hook restricting autogenerate to these tables.

    **A safety control, not a tidiness one.** `include_schemas=True` makes Alembic
    reflect the whole database, and anything it reflects but cannot find in
    `target_metadata` it proposes to **DROP**.

    One schema in this platform has two owners: `ai_summary_events` and
    `ai_summaries` belong to mib-ai but live in `identity` (FR-BE-15), each
    service keeping its own version table there. Without this filter,
    autogenerate in mib-identity emits `drop_table` for mib-ai's tables, and
    autogenerate in mib-ai emits `drop_table` for `users`. Migrations run
    automatically on deploy, so that is data loss on the next release rather than
    something review would catch.

    Derived from the metadata rather than a hand-kept list, so declaring a table
    is all a service has to do and the filter cannot fall behind. Each service
    should keep a test asserting both that the filter suppresses the drops *and*
    that removing it brings them back — a safety test that passes vacuously is
    worse than none, because it reports a control that is not holding.
    """
    schema = metadata.schema

    def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
        if type_ == "schema":
            # `None` is the default schema. Reflecting it pulls in whatever
            # happens to be sitting in `public`.
            return name == schema
        if type_ == "table":
            return name in {table.name for table in metadata.sorted_tables}
        return True

    return include_name


def migration_options(
    metadata: MetaData, *, version_table: str = DEFAULT_VERSION_TABLE
) -> dict[str, Any]:
    """The options every service passes to `context.configure`.

    Separated from `run_migrations` because this is where a mistake is silent:
    drop `include_name` and autogenerate proposes cross-service drops; drop
    `compare_type` or `compare_server_default` and `alembic check` stops noticing
    a changed column type or default while still reporting success. Being a plain
    function, it can be asserted on directly.
    """
    return {
        "target_metadata": metadata,
        "include_schemas": True,
        "include_name": own_tables_only(metadata),
        "version_table": version_table,
        "version_table_schema": metadata.schema,
        # Both default to False in Alembic, and both are most of the point of
        # having metadata at all.
        "compare_type": True,
        "compare_server_default": True,
    }


def run_migrations(
    *,
    metadata: MetaData,
    version_table: str = DEFAULT_VERSION_TABLE,
    create_schema: bool = True,
    database_url_env: str = "DATABASE_URL",
) -> None:
    """Run Alembic for one schema. Call this from `migrations/env.py`.

    The schema comes from `metadata.schema`, so it is stated once. `version_table`
    is only needed where two services share a schema — mib-ai uses
    `alembic_version_ai` so its history does not collide with mib-identity's.

    `create_schema` is a convenience for a fresh development database.
    Autogenerate cannot emit `CREATE SCHEMA`, and in production the schema and
    the per-service role are created during provisioning (FR-BE-16).
    """
    from logging.config import fileConfig

    from alembic import context
    from sqlalchemy import engine_from_config, pool, text

    config = context.config
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)

    url = os.getenv(database_url_env, "")
    if url:
        config.set_main_option("sqlalchemy.url", url)

    options = migration_options(metadata, version_table=version_table)
    schema = metadata.schema

    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            **options,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        if create_schema and schema:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            connection.commit()
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()


__all__ = [
    "DEFAULT_VERSION_TABLE",
    "NAMING_CONVENTION",
    "migration_options",
    "own_tables_only",
    "run_migrations",
    "schema_metadata",
]
