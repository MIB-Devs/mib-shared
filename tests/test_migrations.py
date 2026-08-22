"""The shared Alembic machinery (FR-BE-26).

`run_migrations` itself is glue over Alembic's own API and needs a real database
and an alembic directory to mean anything — the services exercise it, mib-identity
in `tests/test_schema.py`. What is tested here is everything a mistake in would be
*silent*: the naming convention, the safety filter, and the option set.
"""
from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.schema import CreateTable

from mib_shared.migrations import (
    NAMING_CONVENTION,
    migration_options,
    own_tables_only,
    schema_metadata,
)


@pytest.fixture
def meta():
    metadata = schema_metadata("identity")
    Table(
        "users",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(320), nullable=False),
        UniqueConstraint("email"),
    )
    Table(
        "sessions",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, ForeignKey("identity.users.id")),
    )
    return metadata


def test_the_metadata_is_bound_to_the_schema(meta):
    assert meta.schema == "identity"
    assert {t.name for t in meta.sorted_tables} == {"users", "sessions"}


def test_unnamed_constraints_get_the_names_postgresql_would_have_given(meta):
    """This is what lets a service adopt metadata without renaming anything.

    A regenerated baseline has to produce byte-identical DDL to the hand-written
    one it replaces, and constraint names are the part that silently differs. If
    these patterns ever change, every adopting service's schema-equivalence proof
    breaks at once.
    """
    users = str(CreateTable(meta.tables["identity.users"]).compile()).lower()
    sessions = str(CreateTable(meta.tables["identity.sessions"]).compile()).lower()

    assert "constraint users_pkey primary key" in users
    assert "constraint users_email_key unique" in users
    assert "constraint sessions_user_id_fkey foreign key" in sessions


def test_a_check_constraint_is_named_from_its_own_name():
    """`ck` interpolates `constraint_name`, so an unnamed CheckConstraint raises.

    Documented as a test rather than a surprise: name your check constraints.
    """
    metadata = schema_metadata("identity")
    table = Table(
        "prices",
        metadata,
        Column("amount", Integer),
        CheckConstraint("amount >= 0", name="non_negative"),
    )
    assert "constraint prices_non_negative_check" in str(CreateTable(table).compile()).lower()


def test_the_convention_covers_every_constraint_type():
    """A missing key means SQLAlchemy leaves that constraint type unnamed, and an
    unnamed constraint cannot be dropped by name later."""
    assert set(NAMING_CONVENTION) == {"pk", "fk", "uq", "ck", "ix"}


# --- the safety filter -----------------------------------------------------


def test_the_filter_admits_this_schema_and_refuses_every_other(meta):
    include = own_tables_only(meta)
    assert include("identity", "schema", {}) is True
    assert include("regulations", "schema", {}) is False
    # None is the default schema — reflecting it would pull in whatever is in
    # `public`.
    assert include(None, "schema", {}) is False


def test_the_filter_admits_declared_tables_and_refuses_the_rest(meta):
    """The whole point: a table this service does not own is never reflected, so
    autogenerate cannot propose dropping it (FR-BE-15)."""
    include = own_tables_only(meta)
    assert include("users", "table", {}) is True
    assert include("sessions", "table", {}) is True
    assert include("ai_summary_events", "table", {}) is False
    assert include("ai_summaries", "table", {}) is False
    assert include("alembic_version_ai", "table", {}) is False


def test_the_filter_does_not_suppress_columns_or_indexes(meta):
    """Filtering on anything other than schemas and tables would hide real
    changes to our own tables — the opposite of what this is for."""
    include = own_tables_only(meta)
    for type_ in ("column", "index", "unique_constraint", "foreign_key_constraint"):
        assert include("anything", type_, {}) is True


def test_the_filter_follows_the_metadata_rather_than_a_fixed_list(meta):
    """Declaring a table is all a service should have to do."""
    include = own_tables_only(meta)
    assert include("invoices", "table", {}) is False

    Table("invoices", meta, Column("id", Integer, primary_key=True))
    assert include("invoices", "table", {}) is True


# --- the option set --------------------------------------------------------


def test_the_options_carry_the_filter_and_both_comparisons(meta):
    """Each of these is silent when wrong.

    Without `include_name`, autogenerate proposes cross-service drops. Without
    the two comparison flags, `alembic check` reports success while a column's
    type or default has changed underneath it.
    """
    options = migration_options(meta)
    assert options["target_metadata"] is meta
    assert options["include_schemas"] is True
    assert callable(options["include_name"])
    assert options["compare_type"] is True
    assert options["compare_server_default"] is True


def test_the_version_table_defaults_to_this_schema(meta):
    options = migration_options(meta)
    assert options["version_table"] == "alembic_version"
    assert options["version_table_schema"] == "identity"


def test_a_second_owner_of_a_schema_can_name_its_own_version_table(meta):
    """mib-ai keeps `alembic_version_ai` in `identity` so its history does not
    collide with mib-identity's."""
    options = migration_options(meta, version_table="alembic_version_ai")
    assert options["version_table"] == "alembic_version_ai"
    assert options["version_table_schema"] == "identity"


def test_the_filter_in_the_options_is_the_one_for_this_metadata(meta):
    include = migration_options(meta)["include_name"]
    assert include("users", "table", {}) is True
    assert include("ai_summaries", "table", {}) is False
