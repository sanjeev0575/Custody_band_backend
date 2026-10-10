from collections.abc import Generator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from firebase_admin import auth
from firebase_admin.exceptions import FirebaseError

from app.database import Base, get_db
from app import main as main_module


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr(main_module, "engine", test_engine)

    def verify_test_token(token: str) -> dict[str, str]:
        if not token.startswith("test-"):
            raise auth.InvalidIdTokenError("Invalid test token")
        uid = token.removeprefix("test-")
        return {"uid": uid, "email": f"{uid}@example.com"}

    monkeypatch.setattr(main_module, "verify_firebase_id_token", verify_test_token)

    def override_get_db() -> Generator[Session, None, None]:
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app) as test_client:
        yield test_client
    main_module.app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def auth_headers(uid: str = "operator") -> dict[str, str]:
    return {"Authorization": f"Bearer test-{uid}"}


def test_firebase_user_sync_and_tenant_isolation(client: TestClient) -> None:
    response = client.get("/auth/me", headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["email"] == "operator@example.com"
    assert client.get("/auth/me", headers=auth_headers()).json()["id"] == response.json()["id"]
    headers = auth_headers()
    vehicle = client.post("/vehicles", json={"name": "Truck 1"}, headers=headers)
    assert vehicle.status_code == 201
    hidden_vehicle = client.get(
        f"/vehicles/{vehicle.json()['id']}/location",
        headers=auth_headers("another-user"),
    )
    assert hidden_vehicle.status_code == 404
    assert client.get("/vehicles", headers=headers).json()[0]["name"] == "Truck 1"


def test_invalid_firebase_token_is_rejected(client: TestClient) -> None:
    response = client.get("/auth/me", headers={"Authorization": "Bearer invalid-token"})
    assert response.status_code == 401


def test_firebase_auth_service_error_is_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def firebase_unavailable(_: str) -> dict[str, str]:
        raise FirebaseError("Firebase is unavailable")

    monkeypatch.setattr(main_module, "verify_firebase_id_token", firebase_unavailable)
    response = client.get("/auth/me", headers=auth_headers())
    assert response.status_code == 503


def test_device_ingestion_geofence_alert_and_trip_history(client: TestClient) -> None:
    headers = auth_headers()
    vehicle = client.post("/vehicles", json={"name": "Van 2"}, headers=headers).json()
    device = client.post(f"/vehicles/{vehicle['id']}/devices", headers=headers).json()
    client.post(
        "/geofences",
        json={"name": "Depot", "latitude": 40.0, "longitude": -75.0, "radius_m": 500},
        headers=headers,
    ).raise_for_status()

    def ingest(index: int, latitude: float, ignition: bool) -> None:
        timestamp = datetime.now(timezone.utc) + timedelta(minutes=index)
        response = client.post(
            f"/ingest/{device['id']}",
            headers={
                "X-Device-Token": device["token"],
                "Idempotency-Key": f"batch-key-{index:04}",
            },
            json={
                "recorded_at": timestamp.isoformat(),
                "latitude": latitude,
                "longitude": -75.0,
                "speed_kph": 30,
                "ignition_on": ignition,
            },
        )
        assert response.status_code == 201, response.text

    ingest(1, 40.0, True)
    duplicate = client.post(
        f"/ingest/{device['id']}",
        headers={
            "X-Device-Token": device["token"],
            "Idempotency-Key": "batch-key-0001",
        },
        json={
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "latitude": 40.0,
            "longitude": -75.0,
            "speed_kph": 30,
            "ignition_on": True,
        },
    )
    assert duplicate.status_code == 409
    ingest(2, 40.02, True)
    alerts = client.get("/alerts", headers=headers).json()
    assert [alert["event"] for alert in alerts] == ["exited", "entered"]
    latest = client.get(f"/vehicles/{vehicle['id']}/location", headers=headers)
    assert latest.status_code == 200
    assert latest.json()["latitude"] == 40.02
    trips = client.get(
        f"/vehicles/{vehicle['id']}/trips",
        params={
            "start": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "end": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        },
        headers=headers,
    )
    assert trips.status_code == 200
    assert trips.json()[0]["point_count"] == 2


def test_ingestion_requires_valid_device_token_and_timezone(client: TestClient) -> None:
    headers = auth_headers()
    vehicle = client.post("/vehicles", json={"name": "Bus 3"}, headers=headers).json()
    device = client.post(f"/vehicles/{vehicle['id']}/devices", headers=headers).json()
    response = client.post(
        f"/ingest/{device['id']}",
        headers={"X-Device-Token": "wrong", "Idempotency-Key": "batch-key-0001"},
        json={
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "latitude": 12,
            "longitude": 20,
        },
    )
    assert response.status_code == 401
    response = client.post(
        f"/ingest/{device['id']}",
        headers={"X-Device-Token": device["token"], "Idempotency-Key": "batch-key-0002"},
        json={"recorded_at": "2026-01-01T12:00:00", "latitude": 12, "longitude": 20},
    )
    assert response.status_code == 422


def test_person_device_health_alarm_and_reporting_endpoints(client: TestClient) -> None:
    headers = auth_headers()
    person_response = client.post("/persons", json={"name": "Driver One"}, headers=headers)
    assert person_response.status_code == 201
    person = person_response.json()
    vehicle_response = client.post(
        "/vehicles",
        json={"name": "Health monitor", "registration": "HLTH-1"},
        headers=headers,
    )
    assert vehicle_response.status_code == 201
    vehicle = vehicle_response.json()

    device_response = client.post(
        "/devices",
        json={
            "vehicle_id": vehicle["id"],
            "imei": "123456789012345",
            "name": "Wearable 1",
            "model": "HealthWatch",
            "person_id": person["id"],
        },
        headers=headers,
    )
    assert device_response.status_code == 201, device_response.text
    device = device_response.json()
    assert device["imei"] == "123456789012345"
    assert device["token"]
    assert client.get("/devices", headers=headers).json()[0]["person_id"] == person["id"]
    assert client.get("/devices", headers=auth_headers("other")).json() == []

    recorded_at = datetime.now(timezone.utc).isoformat()
    device_headers = {"X-Device-Token": device["token"]}
    health = client.post(
        f"/ingest/{device['id']}/health",
        headers=device_headers,
        json={
            "recorded_at": recorded_at,
            "heart_rate_bpm": 99,
            "temperature_c": 35.9,
            "step_count": 2345,
            "battery_percent": 16,
        },
    )
    assert health.status_code == 201, health.text
    assert health.json()["heart_rate_bpm"] == 99
    start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    health_history = client.get(
        f"/devices/{device['id']}/health",
        params={"start": start, "end": end},
        headers=headers,
    )
    assert health_history.status_code == 200
    assert len(health_history.json()) == 1
    exported = client.get(
        f"/devices/{device['id']}/health.csv",
        params={"start": start, "end": end},
        headers=headers,
    )
    assert exported.status_code == 200
    assert "heart_rate_bpm" in exported.text
    assert "99" in exported.text

    alarm = client.post(
        f"/ingest/{device['id']}/alarms",
        headers=device_headers,
        json={
            "alarm_type": "SOS Alarm",
            "occurred_at": recorded_at,
            "latitude": 17.4,
            "longitude": 78.4,
        },
    )
    assert alarm.status_code == 201, alarm.text
    assert alarm.json()["alarm_type"] == "sos_alarm"
    assert len(client.get("/alerts", headers=headers).json()) == 1
    dashboard = client.get("/dashboard/stats", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["device_count"] == 1
    summary = client.get(
        "/reports/summary",
        params={"start": start, "end": end},
        headers=headers,
    )
    assert summary.status_code == 200
    assert summary.json()["health_record_count"] == 1
    assert summary.json()["alarm_count"] == 1

    push_registration = client.post(
        "/notifications/tokens",
        json={"token": "fcm-device-registration-token-123456"},
        headers=headers,
    )
    assert push_registration.status_code == 201
    push_token_id = push_registration.json()["id"]
    assert client.post(
        "/notifications/tokens",
        json={"token": "fcm-device-registration-token-123456"},
        headers=headers,
    ).json()["id"] == push_token_id
    assert client.delete(
        f"/notifications/tokens/{push_token_id}",
        headers=headers,
    ).status_code == 204


def test_live_tracking_websocket_receives_ingested_location(client: TestClient) -> None:
    headers = auth_headers()
    client.get("/auth/me", headers=headers).raise_for_status()
    vehicle = client.post("/vehicles", json={"name": "Live truck"}, headers=headers).json()
    device = client.post(f"/vehicles/{vehicle['id']}/devices", headers=headers).json()
    with client.websocket_connect("/ws/tracking?token=test-operator") as websocket:
        assert websocket.receive_json() == {"type": "connected"}
        timestamp = datetime.now(timezone.utc).isoformat()
        response = client.post(
            f"/ingest/{device['id']}",
            headers={
                "X-Device-Token": device["token"],
                "Idempotency-Key": "live-tracking-0001",
            },
            json={"recorded_at": timestamp, "latitude": 17.4, "longitude": 78.4},
        )
        assert response.status_code == 201, response.text
        event = websocket.receive_json()
        assert event["type"] == "location.update"
        assert event["imei"] == device.get("imei", event["imei"])
        assert event["latitude"] == 17.4
