"""
Integration tests for Apple SDK (HealthKit) data import.

Tests the full import flow for Apple HealthKit data via SDK.
"""

import logging
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, EventRecord, SleepDetails, WorkoutDetails
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.providers.mobile_sdk import SyncRequest as SDKSyncRequest
from app.services.apple.healthkit.import_service import ImportService
from tests.factories import UserFactory

SDK_ENVELOPE: dict[str, str] = {
    "provider": "apple",
    "sdkVersion": "1.0.0",
    "syncTimestamp": "2025-04-10T12:00:00Z",
}


@pytest.fixture(autouse=True)
def mock_sleep_redis() -> Any:
    """Mock Redis client and Celery task in sleep_service to prevent connection errors."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    mock_redis.set.return_value = True
    mock_redis.expire.return_value = True
    mock_redis.sadd.return_value = 1
    mock_redis.srem.return_value = 1

    with (
        patch("app.services.apple.healthkit.sleep_service.get_redis_client") as mock_get_redis,
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
    ):
        mock_get_redis.return_value = mock_redis
        yield mock_redis


class TestAppleSDKImport:
    """Tests for Apple SDK (HealthKit) import functionality."""

    @pytest.fixture
    def import_service(self) -> ImportService:
        """Create HealthKit import service instance."""
        return ImportService(log=logging.getLogger("test"))

    @pytest.fixture
    def sample_sdk_payload(self) -> dict[str, Any]:
        """Sample Apple SDK payload with records, sleep, and workouts."""
        return {
            **SDK_ENVELOPE,
            "data": {
                "records": [
                    {
                        "id": "ED008640-6873-4647-92B2-24F7680014A0",
                        "type": "HKQuantityTypeIdentifierStepCount",
                        "unit": "count",
                        "value": 66,
                        "startDate": "2022-05-28T23:56:11Z",
                        "endDate": "2022-05-29T00:02:58Z",
                        "source": {
                            "name": "iPhone",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "iPhone",
                            "productType": "iPhone10,5",
                            "deviceHardwareVersion": "iPhone10,5",
                            "deviceSoftwareVersion": "15.4.1",
                            "operatingSystemVersion": {
                                "majorVersion": 15,
                                "minorVersion": 4,
                                "patchVersion": 1,
                            },
                        },
                    }
                ],
                "sleep": [
                    {
                        "id": "E3D5647B-2B0E-43AA-BE3F-9FAD43D35581",
                        "stage": "inBed",
                        "startDate": "2025-04-02T21:50:46Z",
                        "endDate": "2025-04-02T21:50:50Z",
                        "source": {
                            "name": "Test iPhone",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "iPhone",
                            "productType": "iPhone15,2",
                            "deviceSoftwareVersion": "17.6.1",
                            "operatingSystemVersion": {
                                "majorVersion": 17,
                                "minorVersion": 6,
                                "patchVersion": 1,
                            },
                        },
                    }
                ],
                "workouts": [
                    {
                        "id": "801B68D7-F4AA-4A23-BD26-A3BA1BA6B08D",
                        "type": "walking",
                        "startDate": "2025-03-25T17:27:00Z",
                        "endDate": "2025-03-25T18:51:24Z",
                        "source": {
                            "name": "Test Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceHardwareVersion": "Watch7,5",
                            "deviceSoftwareVersion": "10.3.1",
                            "operatingSystemVersion": {
                                "majorVersion": 10,
                                "minorVersion": 3,
                                "patchVersion": 1,
                            },
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 1683.27},
                            {"type": "activeEnergyBurned", "unit": "kcal", "value": 131.41},
                            {"type": "basalEnergyBurned", "unit": "kcal", "value": 48.59},
                            {"type": "distance", "unit": "m", "value": 2165.35},
                            {"type": "minHeartRate", "unit": "bpm", "value": 77},
                            {"type": "averageHeartRate", "unit": "bpm", "value": 121.49},
                            {"type": "maxHeartRate", "unit": "bpm", "value": 141},
                            {"type": "elevationAscended", "unit": "m", "value": 15.57},
                            {"type": "averageMETs", "unit": "kcal/kg/hr", "value": 1.6},
                            {"type": "indoorWorkout", "unit": "bool", "value": False},
                            {"type": "weatherTemperature", "unit": "degC", "value": 11.19},
                            {"type": "weatherHumidity", "unit": "%", "value": 66},
                        ],
                    }
                ],
            },
        }

    def test_import_workout_with_statistics(
        self,
        db: Session,
        import_service: ImportService,
        sample_sdk_payload: dict[str, Any],
    ) -> None:
        """Test importing workout with full statistics (HR, distance, energy)."""
        user = UserFactory()
        user_id = str(user.id)

        result = import_service.load_data(db, sample_sdk_payload, user_id)

        assert result["workouts_saved"] == 1

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").first()
        assert workout is not None
        assert workout.type == "walking"
        assert workout.duration_seconds == 1683

        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).first()
        assert details is not None
        assert details.heart_rate_min == 77
        assert details.heart_rate_max == 141
        assert details.heart_rate_avg == Decimal("121.49")
        assert details.distance == Decimal("2165.35")
        assert details.energy_burned == Decimal("180.00")  # 131.41 + 48.59
        assert details.total_elevation_gain == Decimal("15.57")

    def test_import_workout_without_heart_rate(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test importing workout without heart rate data (older devices)."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "AAAA0000-1111-2222-3333-444455556666",
                        "type": "cycling",
                        "startDate": "2019-09-30T17:00:49Z",
                        "endDate": "2019-09-30T17:14:29Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch3,3",
                            "deviceSoftwareVersion": "5.3",
                            "operatingSystemVersion": {"majorVersion": 5, "minorVersion": 3, "patchVersion": 0},
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 819.51},
                            {"type": "activeEnergyBurned", "unit": "kcal", "value": 77.16},
                            {"type": "basalEnergyBurned", "unit": "kcal", "value": 19.01},
                            {"type": "indoorWorkout", "unit": "bool", "value": True},
                        ],
                    }
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 1

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").first()
        assert workout is not None

        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).first()
        assert details is not None
        assert details.heart_rate_min is None
        assert details.heart_rate_max is None
        assert details.heart_rate_avg is None
        assert details.energy_burned == Decimal("96.17")  # 77.16 + 19.01

    def test_import_multiple_workouts(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test importing multiple workouts in a single batch."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "BBBB0000-1111-2222-3333-444455556666",
                        "type": "running",
                        "startDate": "2025-01-28T08:00:00Z",
                        "endDate": "2025-01-28T08:30:00Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 1800},
                            {"type": "distance", "unit": "m", "value": 5000},
                            {"type": "averageHeartRate", "unit": "bpm", "value": 155},
                        ],
                    },
                    {
                        "id": "CCCC0000-1111-2222-3333-444455556666",
                        "type": "swimming",
                        "startDate": "2025-01-28T18:00:00Z",
                        "endDate": "2025-01-28T18:45:00Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 2700},
                            {"type": "activeEnergyBurned", "unit": "kcal", "value": 450},
                        ],
                    },
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 2

        workouts = db.query(EventRecord).filter(EventRecord.category == "workout").all()
        assert len(workouts) == 2

        types = {w.type for w in workouts}
        assert types == {"running", "swimming"}

    def test_import_duplicate_workout_skipped(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test that duplicate workouts (same datetime) are skipped."""
        user = UserFactory()
        payload: dict[str, Any] = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "DDDD0000-1111-2222-3333-444455556666",
                        "type": "walking",
                        "startDate": "2025-01-29T10:00:00Z",
                        "endDate": "2025-01-29T10:30:00Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 1800},
                        ],
                    }
                ],
            },
        }

        # First import
        result1 = import_service.load_data(db, payload, str(user.id))
        assert result1["workouts_saved"] == 1

        # Second import (same workout, different ID)
        payload["data"]["workouts"][0]["id"] = "EEEE0000-1111-2222-3333-444455556666"
        result2 = import_service.load_data(db, payload, str(user.id))

        assert result2["workouts_saved"] == 0

        workouts = db.query(EventRecord).filter(EventRecord.category == "workout").all()
        assert len(workouts) == 1

    def test_reimport_without_energy_keeps_existing_energy(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """A partial retry must not turn a previously stored energy value into zero."""
        user = UserFactory()
        source = {"name": "Test Watch", "deviceModel": "Test Watch 1", "deviceManufacturer": "Test"}

        def payload(values: list[dict[str, Any]] | None, *, include_values: bool = True) -> dict[str, Any]:
            workout: dict[str, Any] = {
                "id": "ENERGY-RETRY",
                "type": "running",
                "startDate": "2025-01-29T10:00:00Z",
                "endDate": "2025-01-29T10:30:00Z",
                "source": source,
            }
            if include_values:
                workout["values"] = values
            return {**SDK_ENVELOPE, "data": {"workouts": [workout]}}

        initial = import_service.load_data(
            db,
            payload([{"type": "activeEnergyBurned", "unit": "kcal", "value": 321.5}]),
            str(user.id),
        )
        empty_retry = import_service.load_data(db, payload([]), str(user.id))
        metric_retry = import_service.load_data(
            db,
            payload([{"type": "distance", "unit": "m", "value": 2500}], include_values=True),
            str(user.id),
        )
        omitted_retry = import_service.load_data(db, payload(None, include_values=False), str(user.id))

        assert initial["workouts_saved"] == 1
        assert empty_retry["workouts_saved"] == 0
        assert metric_retry["workouts_saved"] == 0
        assert omitted_retry["workouts_saved"] == 0

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").one()
        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).one()
        assert details.energy_burned == Decimal("321.5")
        assert details.distance == Decimal("2500")

    def test_import_mean_cadence_as_workout_detail(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """The aggregate cadence statistic belongs on workout_details, not samples."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "CADENCE-DETAIL",
                        "type": "running",
                        "startDate": "2025-01-29T11:00:00Z",
                        "endDate": "2025-01-29T11:30:00Z",
                        "source": {"name": "Test Watch", "deviceModel": "Test Watch 1"},
                        "values": [{"type": "meanCadence", "unit": "spm", "value": 165}],
                    }
                ]
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 1
        assert result["records_saved"] == 0
        workout = db.query(EventRecord).filter(EventRecord.category == "workout").one()
        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).one()
        assert details.average_cadence == Decimal("165")

    def test_import_route_and_details_enrich_existing_workout(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Route/details arriving on a retry enrich one event without duplicate rows."""
        user = UserFactory()
        source = {
            "name": "Test Watch",
            "deviceModel": "Test Watch 1",
            "deviceManufacturer": "Test",
        }
        initial_payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "ROUTE-INITIAL",
                        "type": "running",
                        "startDate": "2025-01-29T10:00:00Z",
                        "endDate": "2025-01-29T10:30:00Z",
                        "source": source,
                        "values": [
                            {"type": "distance", "unit": "m", "value": 5000},
                            {"type": "averageHeartRate", "unit": "bpm", "value": 145},
                        ],
                    }
                ],
            },
        }
        enrichment_payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "ROUTE-ENRICHMENT",
                        "type": "running",
                        "startDate": "2025-01-29T10:00:00Z",
                        "endDate": "2025-01-29T10:30:00Z",
                        "source": source,
                        "segments": [{"lap": 1, "duration_seconds": 30.0}],
                        "hrZones": {
                            "zones": [{"zone": 1, "seconds": 120}],
                            "maxHr": 180,
                            "thresholdHr": 150,
                        },
                        "route": [
                            {
                                "timestamp": "2025-01-29T10:01:00Z",
                                "latitude": 52.229676,
                                "longitude": 21.012229,
                                "altitudeM": 142.0,
                            },
                            {
                                "timestamp": "2025-01-29T10:02:00Z",
                                "latitude": 52.229700,
                                "longitude": 21.012300,
                            },
                        ],
                    }
                ],
            },
        }

        with (
            patch("app.services.event_record_service.svix_service.is_enabled", return_value=True),
            patch("app.services.event_record_service.on_workout_created") as mock_workout_webhook,
        ):
            assert import_service.load_data(db, initial_payload, str(user.id))["workouts_saved"] == 1
            result = import_service.load_data(db, enrichment_payload, str(user.id))

            empty_enrichment_payload = {
                **SDK_ENVELOPE,
                "data": {
                    "workouts": [
                        {
                            "id": "ROUTE-EMPTY-ENRICHMENT",
                            "type": "running",
                            "startDate": "2025-01-29T10:00:00Z",
                            "endDate": "2025-01-29T10:30:00Z",
                            "source": source,
                            "segments": [],
                            "hrZones": {"zones": []},
                        }
                    ],
                },
            }
            empty_result = import_service.load_data(db, empty_enrichment_payload, str(user.id))

        assert mock_workout_webhook.call_count == 1

        assert result["workouts_saved"] == 0
        assert empty_result["workouts_saved"] == 0
        workouts = db.query(EventRecord).filter(EventRecord.category == "workout").all()
        assert len(workouts) == 1

        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workouts[0].id).one()
        assert details.distance == Decimal("5000")
        assert details.heart_rate_avg == Decimal("145")
        assert details.segments == [{"lap": 1, "duration_seconds": 30.0}]
        assert details.hr_zones is not None
        assert details.hr_zones["max_hr"] == 180
        assert details.hr_zones["threshold_hr"] == 150
        assert details.hr_zones["zones"][0]["seconds"] == 120

        route_series = (
            db.query(DataPointSeries)
            .filter(
                DataPointSeries.series_type_definition_id.in_(
                    [
                        get_series_type_id(SeriesType.latitude),
                        get_series_type_id(SeriesType.longitude),
                        get_series_type_id(SeriesType.elevation),
                    ]
                )
            )
            .all()
        )
        assert len(route_series) == 5
        assert sum(s.series_type_definition_id == get_series_type_id(SeriesType.latitude) for s in route_series) == 2
        assert sum(s.series_type_definition_id == get_series_type_id(SeriesType.longitude) for s in route_series) == 2
        assert sum(s.series_type_definition_id == get_series_type_id(SeriesType.elevation) for s in route_series) == 1

        values_by_type = {}
        for sample in route_series:
            values_by_type.setdefault(sample.series_type_definition_id, []).append(sample.value)
        assert sorted(values_by_type[get_series_type_id(SeriesType.latitude)]) == [
            Decimal("52.229676"),
            Decimal("52.229700"),
        ]
        assert sorted(values_by_type[get_series_type_id(SeriesType.longitude)]) == [
            Decimal("21.012229"),
            Decimal("21.012300"),
        ]
        assert values_by_type[get_series_type_id(SeriesType.elevation)] == [Decimal("142.000000")]

        # A second retry upserts the same timestamp/type keys instead of duplicating points.
        import_service.load_data(db, enrichment_payload, str(user.id))
        route_type_ids = [
            get_series_type_id(SeriesType.latitude),
            get_series_type_id(SeriesType.longitude),
            get_series_type_id(SeriesType.elevation),
        ]
        route_count = (
            db.query(DataPointSeries).filter(DataPointSeries.series_type_definition_id.in_(route_type_ids)).count()
        )
        assert route_count == 5

    def test_import_records_as_time_series(
        self,
        db: Session,
        import_service: ImportService,
        sample_sdk_payload: dict[str, Any],
    ) -> None:
        """Test importing HealthKit records as time series samples."""
        user = UserFactory()

        result = import_service.load_data(db, sample_sdk_payload, str(user.id))

        assert result["records_saved"] >= 0

    def test_import_sleeping_stages_and_top_level_heart_rate_is_idempotent(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Generic sleeping stages survive gaps, session bounds, and reimport/upsert."""
        user = UserFactory()
        source = {
            "name": "Xiaomi Watch S3",
            "deviceModel": "Xiaomi Watch S3",
            "deviceManufacturer": "Xiaomi",
        }
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "records": [
                    {
                        "id": "SLEEP-HR-1",
                        "type": "HKQuantityTypeIdentifierHeartRate",
                        "startDate": "2026-07-01T22:10:00Z",
                        "endDate": "2026-07-01T22:10:00Z",
                        "value": 55,
                        "unit": "count/min",
                        "source": source,
                    }
                ],
                "sleep": [
                    {
                        "id": "SLEEP-GENERIC-1",
                        "stage": "sleeping",
                        "startDate": "2026-07-01T22:00:00Z",
                        "endDate": "2026-07-01T23:00:00Z",
                        "source": source,
                    },
                    {
                        "id": "SLEEP-DEEP-1",
                        "stage": "deep",
                        "startDate": "2026-07-01T22:15:00Z",
                        "endDate": "2026-07-01T22:30:00Z",
                        "source": source,
                    },
                    {
                        "id": "SLEEP-GENERIC-2",
                        "stage": "sleeping",
                        "startDate": "2026-07-01T23:30:00Z",
                        "endDate": "2026-07-02T07:00:00Z",
                        "source": source,
                    },
                    {
                        "id": "SLEEP-LIGHT-1",
                        "stage": "light",
                        "startDate": "2026-07-01T23:30:00Z",
                        "endDate": "2026-07-02T00:30:00Z",
                        "source": source,
                    },
                    {
                        "id": "SLEEP-IN-BED-1",
                        "stage": "in_bed",
                        "startDate": "2026-07-01T21:50:00Z",
                        "endDate": "2026-07-02T07:05:00Z",
                        "source": source,
                    },
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["sleep_saved"] == 5
        assert result["records_saved"] == 1

        sleep_records = db.query(EventRecord).filter(EventRecord.category == "sleep").all()
        assert len(sleep_records) == 1
        record = sleep_records[0]
        assert record.end_datetime.isoformat() == "2026-07-02T07:05:00+00:00"

        detail = db.query(SleepDetails).filter(SleepDetails.record_id == record.id).one()
        assert detail.sleep_deep_minutes == 15
        assert detail.sleep_light_minutes == 60
        assert detail.sleep_rem_minutes == 0
        assert detail.sleep_awake_minutes == 0
        assert detail.sleep_total_duration_minutes == 510
        assert detail.sleep_stages is not None
        assert {stage["stage"] for stage in detail.sleep_stages} == {"sleeping", "deep", "light"}

        sleeping = [stage for stage in detail.sleep_stages if stage["stage"] == "sleeping"]
        assert len(sleeping) == 3
        assert all(left["end_time"] <= right["start_time"] for left, right in zip(sleeping, sleeping[1:]))

        heart_rate = (
            db.query(DataPointSeries)
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(
                DataSource.user_id == user.id,
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
            )
            .all()
        )
        assert len(heart_rate) == 1
        assert heart_rate[0].value == Decimal("55")

        # A retry merges/upserts the same session and top-level HR sample instead
        # of duplicating records, stages, or time-series points.
        import_service.load_data(db, payload, str(user.id))

        assert db.query(EventRecord).filter(EventRecord.category == "sleep").count() == 1
        assert db.query(SleepDetails).count() == 1
        assert (
            db.query(DataPointSeries)
            .filter(DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate))
            .count()
            == 1
        )

        reimported_record = db.query(EventRecord).filter(EventRecord.category == "sleep").one()
        reimported_detail = db.query(SleepDetails).filter(SleepDetails.record_id == reimported_record.id).one()
        assert reimported_record.end_datetime.isoformat() == "2026-07-02T07:05:00+00:00"
        assert reimported_detail.sleep_total_duration_minutes == 510
        assert len(reimported_detail.sleep_stages or []) == 5

    def test_import_empty_payload(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test importing empty payload returns zeros."""
        user = UserFactory()
        payload: dict[str, Any] = {**SDK_ENVELOPE, "data": {}}

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 0
        assert result["records_saved"] == 0
        assert result["sleep_saved"] == 0

    def test_import_workout_with_steps(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test workout with step count statistic."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "FFFF0000-1111-2222-3333-444455556666",
                        "type": "walking",
                        "startDate": "2025-01-29T12:00:00Z",
                        "endDate": "2025-01-29T12:45:00Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 2700},
                            {"type": "stepCount", "unit": "count", "value": 4500},
                            {"type": "distance", "unit": "m", "value": 3200},
                        ],
                    }
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 1

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").first()
        assert workout is not None

        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).first()
        assert details is not None
        assert details.steps_count == 4500
        assert details.distance == Decimal("3200")

    def test_import_workout_with_fractional_steps(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test workout with fractional step count from Apple SDK is truncated to int."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "FFFF1111-2222-3333-4444-555566667777",
                        "type": "walking",
                        "startDate": "2025-02-10T10:00:00Z",
                        "endDate": "2025-02-10T11:00:00Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": [
                            {"type": "duration", "unit": "s", "value": 3600},
                            {"type": "stepCount", "unit": "count", "value": 2981.57515735105},
                            {"type": "distance", "unit": "m", "value": 2165.35},
                        ],
                    }
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 1

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").first()
        assert workout is not None

        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).first()
        assert details is not None
        assert details.steps_count == 2981
        assert details.distance == Decimal("2165.35")


class TestAppleSDKImportEdgeCases:
    """Edge case tests for Apple SDK import."""

    @pytest.fixture
    def import_service(self) -> ImportService:
        return ImportService(log=logging.getLogger("test"))

    def test_import_workout_with_zero_duration(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test workout with zero/missing duration uses calculated duration."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "AAAA1111-2222-3333-4444-555566667777",
                        "type": "yoga",
                        "startDate": "2025-01-29T14:00:00Z",
                        "endDate": "2025-01-29T14:30:00Z",  # 30 min = 1800 seconds
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": [],
                    }
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 1

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").first()
        assert workout is not None
        assert workout.duration_seconds == 1800

    def test_import_workout_null_statistics(
        self,
        db: Session,
        import_service: ImportService,
    ) -> None:
        """Test workout with null values."""
        user = UserFactory()
        payload = {
            **SDK_ENVELOPE,
            "data": {
                "workouts": [
                    {
                        "id": "BBBB1111-2222-3333-4444-555566667777",
                        "type": "other",
                        "startDate": "2025-01-29T15:00:00Z",
                        "endDate": "2025-01-29T15:20:00Z",
                        "source": {
                            "name": "Apple Watch",
                            "bundleIdentifier": "com.apple.health",
                            "deviceManufacturer": "Apple Inc.",
                            "deviceModel": "Watch",
                            "productType": "Watch7,5",
                            "deviceSoftwareVersion": "10.0",
                            "operatingSystemVersion": {"majorVersion": 10, "minorVersion": 0, "patchVersion": 0},
                        },
                        "values": None,
                    }
                ],
            },
        }

        result = import_service.load_data(db, payload, str(user.id))

        assert result["workouts_saved"] == 1

        workout = db.query(EventRecord).filter(EventRecord.category == "workout").first()
        assert workout is not None

        details = db.query(WorkoutDetails).filter(WorkoutDetails.record_id == workout.id).first()
        assert details is not None
        assert details.heart_rate_avg is None
        assert details.distance is None
        assert details.energy_burned is None


class TestSDKImportUnitConversion:
    """Unit conversions applied in `_build_statistic_bundles`.

    Apple HealthKit reports `body_fat_percentage` via `HKUnit.percent()` as a 0..1 ratio,
    while Android Health Connect's `BodyFatRecord.percentage` is already in percent.
    Height in meters is consistent across both platforms and must always be converted to cm.
    """

    @pytest.fixture
    def import_service(self) -> ImportService:
        return ImportService(log=logging.getLogger("test"))

    @staticmethod
    def _record(metric_type: str, value: float, unit: str | None = "") -> dict[str, Any]:
        return {
            "id": f"test-{metric_type}",
            "type": metric_type,
            "unit": unit,
            "value": value,
            "startDate": "2025-04-10T12:00:00Z",
            "endDate": "2025-04-10T12:00:00Z",
            "source": {
                "name": "Test Device",
                "bundleIdentifier": "test",
            },
        }

    def _build_request(self, provider: str, records: list[dict[str, Any]]) -> SDKSyncRequest:
        return SDKSyncRequest(
            **{
                "provider": provider,
                "sdkVersion": "1.0.0",
                "syncTimestamp": "2025-04-10T12:00:00Z",
                "data": {"records": records},
            }
        )

    def test_apple_body_fat_percentage_scaled_by_100(
        self,
        import_service: ImportService,
    ) -> None:
        """Apple sends ratio 0.304 — must be stored as 30.4 (%)."""
        user_id = str(uuid4())
        request = self._build_request(
            "apple",
            [self._record("HKQuantityTypeIdentifierBodyFatPercentage", 0.304)],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.body_fat_percentage
        assert samples[0].value == Decimal("30.400")

    def test_google_body_fat_percentage_not_scaled(
        self,
        import_service: ImportService,
    ) -> None:
        """Google/Health Connect sends already-percent 30.4 — must be stored as 30.4 (no x100)."""
        user_id = str(uuid4())
        request = self._build_request(
            "google",
            [self._record("BODY_FAT", 30.4)],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.body_fat_percentage
        assert samples[0].value == Decimal("30.4")

    def test_samsung_body_fat_percentage_not_scaled(
        self,
        import_service: ImportService,
    ) -> None:
        """Samsung uses Health Connect semantics — already percent, must not be scaled."""
        user_id = str(uuid4())
        request = self._build_request(
            "samsung",
            [self._record("BODY_FAT", 18.5)],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.body_fat_percentage
        assert samples[0].value == Decimal("18.5")

    def test_apple_height_converted_meters_to_centimeters(
        self,
        import_service: ImportService,
    ) -> None:
        """Height in meters 1.7526 — must be stored as 175.26 cm regardless of provider."""
        user_id = str(uuid4())
        request = self._build_request(
            "apple",
            [self._record("HKQuantityTypeIdentifierHeight", 1.7526)],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.height
        assert samples[0].value == Decimal("175.2600")

    def test_google_height_converted_meters_to_centimeters(
        self,
        import_service: ImportService,
    ) -> None:
        """Health Connect also sends height in meters — the x100 conversion still applies."""
        user_id = str(uuid4())
        request = self._build_request(
            "google",
            [self._record("HEIGHT", 1.7526)],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.height
        assert samples[0].value == Decimal("175.2600")

    @pytest.mark.parametrize(
        ("unit", "expected"),
        [
            ("mmol/L", Decimal("110.1092202")),
            ("MMOL/L", Decimal("110.1092202")),
            (None, Decimal("6.111")),
            ("", Decimal("6.111")),
        ],
    )
    def test_health_connect_blood_glucose_unit_handling(
        self,
        import_service: ImportService,
        unit: str | None,
        expected: Decimal,
    ) -> None:
        """Health Connect glucose arrives in mmol/L and must be stored as mg/dL; only a mmol unit converts."""
        user_id = str(uuid4())
        request = self._build_request(
            "samsung",
            [self._record("BLOOD_GLUCOSE", 6.111, unit=unit)],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.blood_glucose
        assert samples[0].value == expected

    def test_healthkit_blood_glucose_not_scaled(
        self,
        import_service: ImportService,
    ) -> None:
        """HealthKit reports glucose in mg/dL - stored unchanged."""
        user_id = str(uuid4())
        request = self._build_request(
            "apple",
            [self._record("HKQuantityTypeIdentifierBloodGlucose", 105, unit="mg/dL")],
        )
        samples = import_service._build_statistic_bundles(request, user_id)

        assert len(samples) == 1
        assert samples[0].series_type == SeriesType.blood_glucose
        assert samples[0].value == Decimal("105")
