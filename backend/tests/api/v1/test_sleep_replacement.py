from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Never
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.fill_missing_sleep_scores_task import _MISSING_SCORES_QUERY
from app.models import EventRecord, HealthScore, SleepDetails
from app.services.event_record_service import event_record_service
from app.services.scores.sleep_service import sleep_score_service
from tests.factories import ApiKeyFactory, DataSourceFactory, EventRecordFactory, SleepDetailsFactory, UserFactory
from tests.utils import api_key_headers


def _payload(start: datetime, external_id: str = "session-demo-a") -> dict:
    end = start + timedelta(minutes=4)
    return {
        "external_id": external_id,
        "provider": "samsung",
        "source": "sdk",
        "device_model": "synthetic-watch",
        "source_name": "Synthetic SDK",
        "start_time": start.isoformat().replace("+00:00", "Z"),
        "end_time": end.isoformat().replace("+00:00", "Z"),
        "is_nap": False,
        "aggregates": {
            "sleep_duration_minutes": 3,
            "time_in_bed_minutes": 4,
            "deep_minutes": 1,
            "light_minutes": 2,
            "rem_minutes": 0,
            "awake_minutes": 1,
        },
        "intervals": [
            {
                "stage": "light",
                "start_time": start.isoformat().replace("+00:00", "Z"),
                "end_time": (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            },
            {
                "stage": "awake",
                "start_time": (start + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                "end_time": (start + timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
            },
            {
                "stage": "deep",
                "start_time": (start + timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
                "end_time": end.isoformat().replace("+00:00", "Z"),
            },
        ],
    }


def test_exact_sleep_score_uses_session_identity_and_local_wake_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    record_id = uuid4()
    data_source_id = uuid4()
    record = SimpleNamespace(
        id=record_id,
        data_source_id=data_source_id,
        end_datetime=datetime(2026, 1, 9, 23, 58, tzinfo=timezone.utc),
        zone_offset="+02:00",
    )

    def pillar(value: int) -> SimpleNamespace:
        return SimpleNamespace(score=value)

    result = SimpleNamespace(
        overall_score=87,
        breakdown=SimpleNamespace(
            duration=pillar(80),
            stages=pillar(90),
            consistency=pillar(85),
            interruptions=pillar(92),
        ),
    )
    db = MagicMock()
    upsert = MagicMock()
    observed_record_wakes: list[tuple[object, object]] = []

    def get_scores(
        _db: object,
        _user_id: object,
        record_wakes: list[tuple[object, object]],
    ) -> dict[tuple[object, object], SimpleNamespace]:
        observed_record_wakes.extend(record_wakes)
        return {(record_id, record_wakes[0][1]): result}

    monkeypatch.setattr(
        sleep_score_service,
        "get_sleep_scores_for_records",
        get_scores,
    )
    monkeypatch.setattr(
        event_record_service.health_score_repo,
        "upsert_sleep_record_score",
        upsert,
    )

    event_record_service._recompute_sleep_score_for_record(db, user_id, record)

    score = upsert.call_args.args[1]
    assert observed_record_wakes == [(record_id, datetime(2026, 1, 10).date())]
    assert score.sleep_record_id == record_id
    assert score.data_source_id == data_source_id
    assert score.recorded_at == datetime(2026, 1, 10, 1, 58, tzinfo=timezone.utc)
    assert score.components["duration"]["value"] == 80


def test_exact_sleep_replacement_creates_replaces_and_retries_without_duplicates(
    client: TestClient, db: Session
) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    start = datetime(2026, 1, 2, 22, tzinfo=timezone.utc)
    url = f"/api/v1/users/{user.id}/events/sleep/exact-replacement"
    created = client.put(url, headers=api_key_headers(key.id), json=_payload(start))
    assert created.status_code == 200
    first = created.json()
    assert first["created"] is True
    assert first["sleep_duration_seconds"] == 180
    assert first["aggregates"] == _payload(start)["aggregates"]

    replacement = _payload(start)
    replacement["intervals"][0]["stage"] = "rem"
    replaced = client.put(url, headers=api_key_headers(key.id), json=replacement)
    assert replaced.status_code == 200
    assert replaced.json()["created"] is False
    assert [item["stage"] for item in replaced.json()["sleep_stage_intervals"]] == ["rem", "awake", "deep"]

    retried = client.put(url, headers=api_key_headers(key.id), json=replacement)
    assert retried.status_code == 200
    assert retried.json() == replaced.json()
    assert db.query(EventRecord).filter(EventRecord.external_id == "session-demo-a").count() == 1
    assert db.query(SleepDetails).count() == 1
    detail = db.query(SleepDetails).one()
    assert (
        detail.sleep_deep_minutes,
        detail.sleep_light_minutes,
        detail.sleep_rem_minutes,
        detail.sleep_awake_minutes,
    ) == (1, 2, 0, 1)
    listed = client.get(
        f"/api/v1/users/{user.id}/events/sleep",
        headers=api_key_headers(key.id),
        params={
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(minutes=5)).isoformat(),
        },
    )
    assert listed.status_code == 200
    event = listed.json()["data"][0]
    assert event["sleep_duration_seconds"] == 180
    assert event["stages"] == {
        "awake_minutes": 1,
        "light_minutes": 2,
        "deep_minutes": 1,
        "rem_minutes": 0,
    }
    assert [item["stage"] for item in event["sleep_stage_intervals"]] == ["rem", "awake", "deep"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deep_minutes", True),
        ("deep_minutes", -1),
        ("deep_minutes", 1.5),
        ("deep_minutes", "1"),
        ("sleep_duration_minutes", 4),
        ("time_in_bed_minutes", 5),
    ],
)
def test_exact_sleep_replacement_rejects_invalid_aggregates(
    client: TestClient,
    field: str,
    value: object,
) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    payload = _payload(datetime(2026, 1, 6, 22, tzinfo=timezone.utc))
    payload["aggregates"][field] = value

    response = client.put(
        f"/api/v1/users/{user.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=payload,
    )

    assert response.status_code == 400


def test_exact_sleep_replacement_rejects_coerced_boolean(client: TestClient) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    payload = _payload(datetime(2026, 1, 6, 22, tzinfo=timezone.utc))
    payload["is_nap"] = "false"

    response = client.put(
        f"/api/v1/users/{user.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=payload,
    )

    assert response.status_code == 400


@pytest.mark.parametrize("location", ["payload", "aggregates", "interval"])
def test_exact_sleep_replacement_rejects_unknown_fields(client: TestClient, location: str) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    payload = _payload(datetime(2026, 1, 7, 22, tzinfo=timezone.utc))
    target = payload if location == "payload" else payload["aggregates"]
    if location == "interval":
        target = payload["intervals"][0]
    target["unexpected"] = 1

    response = client.put(
        f"/api/v1/users/{user.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=payload,
    )

    assert response.status_code == 400


def test_exact_sleep_replacement_rejects_gap_without_mutating_existing(client: TestClient, db: Session) -> None:
    user = UserFactory()
    source = DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    start = datetime(2026, 1, 3, 22, tzinfo=timezone.utc)
    record = EventRecordFactory(
        mapping=source,
        category="sleep",
        external_id="session-demo-b",
        start_datetime=start,
        end_datetime=start + timedelta(minutes=4),
        duration_seconds=240,
    )
    SleepDetailsFactory(event_record=record, sleep_total_duration_minutes=4, sleep_stages=None)
    key = ApiKeyFactory()
    payload = _payload(start, "session-demo-b")
    payload["intervals"][1]["start_time"] = (
        (start + timedelta(minutes=2, seconds=30)).isoformat().replace("+00:00", "Z")
    )

    response = client.put(
        f"/api/v1/users/{user.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=payload,
    )

    assert response.status_code == 400
    db.refresh(record)
    assert record.start_datetime == start
    assert record.sleep_detail.sleep_stages is None


def test_exact_sleep_replacement_wrong_user_does_not_disclose_existing(client: TestClient, db: Session) -> None:
    owner = UserFactory()
    other = UserFactory()
    source = DataSourceFactory(user=owner, provider="samsung", source="sdk", device_model="synthetic-watch")
    start = datetime(2026, 1, 4, 22, tzinfo=timezone.utc)
    record = EventRecordFactory(
        mapping=source,
        category="sleep",
        external_id="session-demo-c",
        start_datetime=start,
        end_datetime=start + timedelta(minutes=4),
        duration_seconds=240,
    )
    SleepDetailsFactory(event_record=record)
    key = ApiKeyFactory()

    response = client.put(
        f"/api/v1/users/{other.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=_payload(start, "session-demo-c"),
    )

    assert response.status_code == 404
    assert "exist" not in response.text.lower()


def test_exact_sleep_replacement_rejects_external_identity_reuse_in_another_source(
    client: TestClient,
    db: Session,
) -> None:
    user = UserFactory()
    source = DataSourceFactory(user=user, provider="samsung", source="other", device_model="synthetic-watch")
    start = datetime(2026, 1, 5, 22, tzinfo=timezone.utc)
    record = EventRecordFactory(
        mapping=source,
        category="sleep",
        external_id="session-demo-d",
        start_datetime=start,
        end_datetime=start + timedelta(minutes=4),
        duration_seconds=240,
    )
    SleepDetailsFactory(event_record=record)
    key = ApiKeyFactory()

    response = client.put(
        f"/api/v1/users/{user.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=_payload(start, "session-demo-d"),
    )

    assert response.status_code == 404
    assert db.query(EventRecord).filter(EventRecord.external_id == "session-demo-d").count() == 1


def test_exact_sleep_replacement_accepts_undercoverage_without_chronology(
    client: TestClient,
    db: Session,
) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    start = datetime(2026, 1, 8, 22, tzinfo=timezone.utc)
    payload = _payload(start, "session-demo-undercoverage")
    payload["aggregates"] = {
        "sleep_duration_minutes": 2,
        "time_in_bed_minutes": 4,
        "deep_minutes": 1,
        "light_minutes": 1,
        "rem_minutes": 0,
        "awake_minutes": 1,
    }
    payload["intervals"] = []

    response = client.put(
        f"/api/v1/users/{user.id}/events/sleep/exact-replacement",
        headers=api_key_headers(key.id),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["sleep_stage_intervals"] == []
    detail = db.query(SleepDetails).one()
    assert detail.sleep_total_duration_minutes == 2
    assert detail.sleep_time_in_bed_minutes == 4
    assert detail.sleep_stages == []


def test_exact_sleep_replacement_score_is_session_linked_retry_safe_and_backfill_complete(
    client: TestClient,
    db: Session,
) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    start = datetime(2026, 1, 9, 23, 58, tzinfo=timezone.utc)
    payload = _payload(start, "session-demo-score")
    payload["zone_offset"] = "+02:00"
    url = f"/api/v1/users/{user.id}/events/sleep/exact-replacement"

    created = client.put(url, headers=api_key_headers(key.id), json=payload)
    assert created.status_code == 200
    record = db.query(EventRecord).filter_by(external_id="session-demo-score").one()
    first_score = db.query(HealthScore).filter_by(sleep_record_id=record.id).one()
    first_score_id = first_score.id
    assert first_score.data_source_id == record.data_source_id
    assert first_score.recorded_at == datetime(2026, 1, 10, 2, 2, tzinfo=timezone.utc)

    payload["aggregates"]["deep_minutes"] = 0
    payload["aggregates"]["light_minutes"] = 3
    retried = client.put(url, headers=api_key_headers(key.id), json=payload)
    assert retried.status_code == 200
    db.expire_all()
    scores = db.query(HealthScore).filter_by(sleep_record_id=record.id).all()
    assert len(scores) == 1
    assert scores[0].id == first_score_id

    missing = db.execute(_MISSING_SCORES_QUERY, {"cutoff": datetime(2020, 1, 1, tzinfo=timezone.utc)}).fetchall()
    assert all(row.record_id != record.id for row in missing)


def test_exact_sleep_replacement_rolls_back_detail_and_score_together(
    client: TestClient,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = UserFactory()
    DataSourceFactory(user=user, provider="samsung", source="sdk", device_model="synthetic-watch")
    key = ApiKeyFactory()
    start = datetime(2026, 1, 11, 22, tzinfo=timezone.utc)
    payload = _payload(start, "session-demo-rollback")
    url = f"/api/v1/users/{user.id}/events/sleep/exact-replacement"
    assert client.put(url, headers=api_key_headers(key.id), json=payload).status_code == 200
    record = db.query(EventRecord).filter_by(external_id="session-demo-rollback").one()
    original_score = db.query(HealthScore).filter_by(sleep_record_id=record.id).one()
    original_score_id = original_score.id
    original_stages = list(record.sleep_detail.sleep_stages)

    def fail_score(*_args, **_kwargs) -> Never:
        raise RuntimeError("synthetic score failure")

    monkeypatch.setattr(sleep_score_service, "get_sleep_scores_for_records", fail_score)
    replacement = _payload(start, "session-demo-rollback")
    replacement["intervals"][0]["stage"] = "rem"
    with pytest.raises(RuntimeError, match="synthetic score failure"):
        client.put(url, headers=api_key_headers(key.id), json=replacement)

    db.expire_all()
    persisted = db.query(EventRecord).filter_by(external_id="session-demo-rollback").one()
    assert persisted.sleep_detail.sleep_stages == original_stages
    scores = db.query(HealthScore).filter_by(sleep_record_id=persisted.id).all()
    assert len(scores) == 1
    assert scores[0].id == original_score_id
