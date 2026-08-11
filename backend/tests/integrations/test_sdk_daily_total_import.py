import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource
from app.schemas.enums import SeriesType, get_series_type_id
from app.services.apple.healthkit.import_service import ImportService
from tests.factories import UserFactory


def _metric_record(record_id: str, start_date: str, value: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": record_id,
        "type": "HKQuantityTypeIdentifierStepCount",
        "unit": "count",
        "value": value,
        "startDate": start_date,
        "endDate": start_date,
        "source": {"name": "Gadgetbridge", "deviceModel": "Xiaomi Watch S3"},
        **extra,
    }


def _payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider": "apple",
        "sdkVersion": "1.0.0",
        "syncTimestamp": "2026-08-07T12:00:00Z",
        "data": {"records": records},
    }


def test_sdk_daily_total_flag_is_persisted_and_missing_flag_is_retry_safe(db: Session) -> None:
    user = UserFactory()
    service = ImportService(log=logging.getLogger("test"))
    daily_timestamp = "2026-08-07T00:00:00Z"

    result = service.load_data(
        db,
        _payload(
            [
                _metric_record("daily", daily_timestamp, 10000, isDailyTotal=True),
                _metric_record("intraday", "2026-08-07T00:15:00Z", 100, isDailyTotal=False),
                _metric_record("legacy", "2026-08-07T00:30:00Z", 200),
            ]
        ),
        str(user.id),
    )

    assert result["records_saved"] == 3
    rows = (
        db.query(DataPointSeries)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user.id,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.steps),
        )
        .order_by(DataPointSeries.recorded_at)
        .all()
    )
    assert [row.is_daily_total for row in rows] == [True, False, False]

    retry_result = service.load_data(
        db,
        _payload([_metric_record("daily", daily_timestamp, 12000)]),
        str(user.id),
    )

    assert retry_result["records_saved"] == 1
    assert len(rows) == 3
    daily_row = rows[0]
    db.refresh(daily_row)
    assert daily_row.value == 12000
    assert daily_row.is_daily_total is True
