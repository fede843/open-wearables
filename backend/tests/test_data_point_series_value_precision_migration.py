"""Regression tests for the data point numeric precision migration."""

import runpy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Numeric, inspect, text
from sqlalchemy.orm import Session

from app.models import DataPointSeries
from tests.factories import DataPointSeriesFactory, DataSourceFactory, UserFactory

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "2026_08_07_1200-f6a7b8c9d0e1_data_point_series_value_precision.py"
)


def test_migration_widens_value_and_preserves_existing_precision(db: Session) -> None:
    """Upgrade keeps old values and accepts six-decimal GPS-like values."""
    db.execute(text("ALTER TABLE data_point_series ALTER COLUMN value TYPE NUMERIC(10, 3)"))

    user = UserFactory()
    data_source = DataSourceFactory(user=user)
    old_value = DataPointSeriesFactory(
        data_source=data_source,
        value=Decimal("52.229"),
        recorded_at=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
    )
    db.flush()

    migration = runpy.run_path(str(MIGRATION_PATH))
    upgrade = cast(Any, migration["upgrade"])
    with Operations.context(MigrationContext.configure(db.connection())):
        upgrade()

    db.expire(old_value)
    assert db.get(DataPointSeries, old_value.id).value == Decimal("52.229000")

    precise_value = DataPointSeriesFactory(
        data_source=data_source,
        value=Decimal("52.229676"),
        recorded_at=datetime(2026, 8, 7, 12, 1, tzinfo=timezone.utc),
    )
    db.flush()
    db.refresh(precise_value)
    assert precise_value.value == Decimal("52.229676")

    value_column = next(
        column for column in inspect(db.get_bind()).get_columns("data_point_series") if column["name"] == "value"
    )
    assert isinstance(value_column["type"], Numeric)
    assert value_column["type"].precision == 15
    assert value_column["type"].scale == 6
