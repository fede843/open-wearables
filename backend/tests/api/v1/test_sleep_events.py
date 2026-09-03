"""Focused readback tests for public sleep sessions."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.schemas.model_crud.activities.sleep import SleepStage
from app.schemas.providers.mobile_sdk import SleepStateStage
from app.services.apple.healthkit.sleep_service import _calculate_final_metrics
from tests.factories import (
    ApiKeyFactory,
    DataSourceFactory,
    EventRecordFactory,
    SleepDetailsFactory,
    UserFactory,
)
from tests.utils import api_key_headers


def test_get_sleep_event_reads_specific_and_generic_stage_intervals(client: TestClient, db: Session) -> None:
    user = UserFactory()
    mapping = DataSourceFactory(user=user)
    start = datetime(2025, 12, 25, 22, 0, tzinfo=timezone.utc)
    record = EventRecordFactory(
        mapping=mapping,
        category="sleep",
        start_datetime=start,
        end_datetime=datetime(2025, 12, 25, 22, 5, tzinfo=timezone.utc),
        duration_seconds=300,
    )
    stages = [
        SleepStage(
            stage=stage,
            start_time=start.replace(minute=index),
            end_time=start.replace(minute=index + 1),
        ).model_dump(mode="json")
        for index, stage in enumerate(("light", "deep", "rem", "awake", "sleeping"))
    ]
    SleepDetailsFactory(
        event_record=record,
        sleep_total_duration_minutes=4,
        sleep_light_minutes=1,
        sleep_deep_minutes=1,
        sleep_rem_minutes=1,
        sleep_awake_minutes=1,
        sleep_stages=stages,
        is_nap=False,
    )
    api_key = ApiKeyFactory()

    response = client.get(
        f"/api/v1/users/{user.id}/events/sleep",
        headers=api_key_headers(api_key.id),
        params={
            "start_date": "2025-12-25T00:00:00Z",
            "end_date": "2025-12-26T00:00:00Z",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["has_more"] is False
    assert body["data"][0]["stages"] == {
        "awake_minutes": 1,
        "light_minutes": 1,
        "deep_minutes": 1,
        "rem_minutes": 1,
    }
    assert [item["stage"] for item in body["data"][0]["sleep_stage_intervals"]] == [
        "light",
        "deep",
        "rem",
        "awake",
        "sleeping",
    ]


def test_get_sleep_events_preserves_nullable_zero_and_unknown_stages(client: TestClient, db: Session) -> None:
    user = UserFactory()
    mapping = DataSourceFactory(user=user)
    api_key = ApiKeyFactory()
    start = datetime(2025, 12, 26, 22, 0, tzinfo=timezone.utc)
    cases = (
        (None, None, "unknown"),
        (0, 0, "sleeping"),
    )
    for index, (light, deep, stage) in enumerate(cases):
        record_start = start.replace(minute=index * 2)
        record = EventRecordFactory(
            mapping=mapping,
            category="sleep",
            start_datetime=record_start,
            end_datetime=record_start.replace(minute=index * 2 + 1),
            duration_seconds=60,
        )
        SleepDetailsFactory(
            event_record=record,
            sleep_light_minutes=light,
            sleep_deep_minutes=deep,
            sleep_rem_minutes=None,
            sleep_awake_minutes=0 if light == 0 else None,
            sleep_stages=[
                SleepStage(
                    stage=stage,
                    start_time=record_start,
                    end_time=record_start.replace(minute=index * 2 + 1),
                ).model_dump(mode="json")
            ],
        )

    response = client.get(
        f"/api/v1/users/{user.id}/events/sleep",
        headers=api_key_headers(api_key.id),
        params={
            "start_date": "2025-12-26T00:00:00Z",
            "end_date": "2025-12-27T00:00:00Z",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    by_stage = {item["sleep_stage_intervals"][0]["stage"]: item for item in response.json()["data"]}
    assert by_stage["unknown"]["stages"] == {
        "awake_minutes": None,
        "light_minutes": None,
        "deep_minutes": None,
        "rem_minutes": None,
    }
    assert by_stage["sleeping"]["stages"] == {
        "awake_minutes": 0,
        "light_minutes": 0,
        "deep_minutes": 0,
        "rem_minutes": None,
    }


def test_get_sleep_event_reads_unknown_only_for_uncovered_gaps(client: TestClient, db: Session) -> None:
    user = UserFactory()
    mapping = DataSourceFactory(user=user)
    api_key = ApiKeyFactory()
    start = datetime(2025, 12, 27, 22, 0, tzinfo=timezone.utc)
    metrics, cleaned = _calculate_final_metrics(
        [
            SleepStateStage(
                stage="unknown",
                start_time=start,
                end_time=start.replace(minute=30),
            ),
            SleepStateStage(
                stage="unknown",
                start_time=start.replace(minute=12),
                end_time=start.replace(minute=13),
            ),
            SleepStateStage(
                stage="sleeping",
                start_time=start.replace(minute=5),
                end_time=start.replace(minute=10),
            ),
            SleepStateStage(
                stage="awake",
                start_time=start.replace(minute=10),
                end_time=start.replace(minute=15),
            ),
            SleepStateStage(
                stage="light",
                start_time=start.replace(minute=15),
                end_time=start.replace(minute=20),
            ),
            SleepStateStage(
                stage="deep",
                start_time=start.replace(minute=20),
                end_time=start.replace(minute=25),
            ),
            SleepStateStage(
                stage="rem",
                start_time=start.replace(minute=25),
                end_time=start.replace(minute=30),
            ),
        ]
    )
    record = EventRecordFactory(
        mapping=mapping,
        category="sleep",
        start_datetime=start,
        end_datetime=start.replace(minute=30),
        duration_seconds=1800,
    )
    SleepDetailsFactory(
        event_record=record,
        sleep_total_duration_minutes=int(
            (metrics["sleeping_seconds"] + metrics["light_seconds"] + metrics["deep_seconds"] + metrics["rem_seconds"])
            // 60
        ),
        sleep_light_minutes=int(metrics["light_seconds"] // 60),
        sleep_deep_minutes=int(metrics["deep_seconds"] // 60),
        sleep_rem_minutes=int(metrics["rem_seconds"] // 60),
        sleep_awake_minutes=int(metrics["awake_seconds"] // 60),
        sleep_stages=[stage.model_dump(mode="json") for stage in cleaned],
        is_nap=False,
    )

    response = client.get(
        f"/api/v1/users/{user.id}/events/sleep",
        headers=api_key_headers(api_key.id),
        params={
            "start_date": "2025-12-27T00:00:00Z",
            "end_date": "2025-12-28T00:00:00Z",
            "limit": 1,
        },
    )

    assert response.status_code == 200
    intervals = response.json()["data"][0]["sleep_stage_intervals"]
    assert [(item["stage"], item["start_time"], item["end_time"]) for item in intervals] == [
        ("unknown", "2025-12-27T22:00:00Z", "2025-12-27T22:05:00Z"),
        ("sleeping", "2025-12-27T22:05:00Z", "2025-12-27T22:10:00Z"),
        ("awake", "2025-12-27T22:10:00Z", "2025-12-27T22:15:00Z"),
        ("light", "2025-12-27T22:15:00Z", "2025-12-27T22:20:00Z"),
        ("deep", "2025-12-27T22:20:00Z", "2025-12-27T22:25:00Z"),
        ("rem", "2025-12-27T22:25:00Z", "2025-12-27T22:30:00Z"),
    ]
    assert response.json()["data"][0]["sleep_duration_seconds"] == 1200
