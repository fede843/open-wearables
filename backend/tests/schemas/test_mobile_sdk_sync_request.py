from app.schemas.providers.mobile_sdk.sync_request import MetricRecord


def _metric_record_payload() -> dict[str, object]:
    return {
        "type": "HKQuantityTypeIdentifierStepCount",
        "startDate": "2026-08-07T00:00:00Z",
        "endDate": "2026-08-07T00:15:00Z",
        "value": 120,
        "unit": "count",
    }


def test_metric_record_accepts_is_daily_total_wire_alias() -> None:
    record = MetricRecord.model_validate({**_metric_record_payload(), "isDailyTotal": True})

    assert record.is_daily_total is True
    assert record.model_dump(by_alias=True)["isDailyTotal"] is True


def test_metric_record_keeps_missing_or_null_is_daily_total_optional() -> None:
    missing = MetricRecord.model_validate(_metric_record_payload())
    null = MetricRecord.model_validate({**_metric_record_payload(), "isDailyTotal": None})

    assert missing.is_daily_total is None
    assert null.is_daily_total is None
