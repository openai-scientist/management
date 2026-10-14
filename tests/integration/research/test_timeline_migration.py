import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_migration_preserves_pipeline_stage_and_roundtrips_legacy_step():
    path = Path("alembic/versions/20261014_0027_run_event_fields.py")
    spec = importlib.util.spec_from_file_location("timeline_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE stage_runs (id TEXT PRIMARY KEY, iteration INTEGER NOT NULL)")
        )
        connection.execute(sa.text("INSERT INTO stage_runs VALUES ('s1', 2)"))
        connection.execute(
            sa.text(
                "CREATE TABLE run_events (seq INTEGER PRIMARY KEY, stage_key TEXT, "
                "stage_run_id TEXT, source_seq INTEGER)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO run_events VALUES (1, 'screen', NULL, 1), "
                "(2, 'hypothesis', 's1', 1), (3, 'hypothesis', 's1', NULL)"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        rows = connection.execute(sa.text("SELECT * FROM run_events ORDER BY seq")).mappings().all()
        assert (rows[0]["stage_key"], rows[0]["step_key"]) == (None, "screen")
        assert rows[1]["stage_key"] == "hypothesis" and rows[1]["iteration"] == 2
        assert rows[2]["class"] == "platform"
        # The retained Popper callback still writes legacy stage_key after this upgrade.
        connection.execute(
            sa.text(
                "INSERT INTO run_events (seq, stage_key, stage_run_id, source_seq) "
                "VALUES (4, 'scope', NULL, 2)"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        rows = connection.execute(sa.text("SELECT * FROM run_events ORDER BY seq")).mappings().all()
        assert rows[0]["stage_key"] == "screen" and rows[1]["stage_key"] == "hypothesis"
        assert rows[3]["stage_key"] == "scope"
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(
            sa.text("SELECT step_key FROM run_events WHERE seq=1")
        ).scalar() == ("screen")
    engine.dispose()
