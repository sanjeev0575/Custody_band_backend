import csv
import io
import logging
import math
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated

import firebase_admin
from firebase_admin import auth
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.exceptions import GoogleAuthError
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.config import settings
from app.database import engine, get_db, initialize_database
from app.firebase_auth import verify_firebase_id_token
from app.models import (
    Alert,
    Device,
    Geofence,
    GeofenceState,
    HealthRecord,
    Location,
    LocationBatch,
    Person,
    PushToken,
    User,
    Vehicle,
)
from app.notifications import send_push_notifications
from app.realtime import tracking_connections
from app.schemas import (
    AlarmInput,
    AlertRead,
    DashboardStats,
    DeviceCreate,
    DeviceRegistered,
    DeviceRead,
    DeviceSummary,
    DeviceUpdate,
    GeofenceCreate,
    GeofenceRead,
    HealthInput,
    HealthRead,
    LocationInput,
    LocationRead,
    PersonCreate,
    PersonRead,
    PersonUpdate,
    PushTokenCreate,
    PushTokenRead,
    TripRead,
    UserRead,
    VehicleCreate,
    VehicleRead,
)
from app.security import create_device_token, hash_device_token

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    initialize_database(engine)
    yield


app = FastAPI(title="Trackin360 Fleet API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    try:
        claims = verify_firebase_id_token(credentials.credentials)
    except (auth.InvalidIdTokenError, auth.ExpiredIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked Firebase ID token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except (firebase_admin.exceptions.FirebaseError, GoogleAuthError, OSError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firebase Authentication is temporarily unavailable",
        ) from None

    firebase_uid = claims.get("uid")
    if not isinstance(firebase_uid, str) or not firebase_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase ID token has no user ID",
            headers={"WWW-Authenticate": "Bearer"},
        )

    email = claims.get("email")
    if not isinstance(email, str):
        email = None
    else:
        email = email.strip().lower() or None

    user = db.scalar(select(User).where(User.firebase_uid == firebase_uid))
    if user is None:
        user = User(firebase_uid=firebase_uid, email=email)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.scalar(select(User).where(User.firebase_uid == firebase_uid))
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This email address is already associated with another account",
                ) from None
    elif user.email != email:
        user.email = email
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This email address is already associated with another account",
            ) from None
    display_name = claims.get("name")
    if isinstance(display_name, str) and display_name.strip() and user.display_name != display_name.strip():
        user.display_name = display_name.strip()
        db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
Database = Annotated[Session, Depends(get_db)]


def owned_vehicle(db: Session, vehicle_id: uuid.UUID, owner_id: uuid.UUID) -> Vehicle:
    vehicle = db.scalar(
        select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.owner_id == owner_id)
    )
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return vehicle


def owned_device(db: Session, device_id: uuid.UUID, owner_id: uuid.UUID) -> Device:
    device = db.scalar(
        select(Device).where(Device.id == device_id, Device.owner_id == owner_id)
    )
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


def validate_time_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status_code=422, detail="start and end must include a timezone")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be earlier than end")
    return start, end


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    haversine = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(haversine))


@app.get("/auth/me", response_model=UserRead)
def read_current_user(user: CurrentUser) -> User:
    return user


@app.post("/vehicles", response_model=VehicleRead, status_code=status.HTTP_201_CREATED)
def create_vehicle(payload: VehicleCreate, user: CurrentUser, db: Database) -> Vehicle:
    vehicle = Vehicle(owner_id=user.id, name=payload.name.strip(), registration=payload.registration)
    if not vehicle.name:
        raise HTTPException(status_code=422, detail="Vehicle name cannot be blank")
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return vehicle


@app.get("/vehicles", response_model=list[VehicleRead])
def list_vehicles(user: CurrentUser, db: Database) -> list[Vehicle]:
    return list(db.scalars(select(Vehicle).where(Vehicle.owner_id == user.id).order_by(Vehicle.created_at)))


@app.delete("/vehicles/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vehicle(vehicle_id: uuid.UUID, user: CurrentUser, db: Database) -> None:
    vehicle = owned_vehicle(db, vehicle_id, user.id)
    db.delete(vehicle)
    db.commit()


@app.post(
    "/vehicles/{vehicle_id}/devices",
    response_model=DeviceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_device(vehicle_id: uuid.UUID, user: CurrentUser, db: Database) -> DeviceRead:
    owned_vehicle(db, vehicle_id, user.id)
    token = create_device_token()
    device = Device(vehicle_id=vehicle_id, owner_id=user.id, token_hash=hash_device_token(token))
    db.add(device)
    db.commit()
    db.refresh(device)
    return DeviceRead(id=device.id, vehicle_id=vehicle_id, token=token)


@app.post("/devices", response_model=DeviceRegistered, status_code=status.HTTP_201_CREATED)
def register_device(payload: DeviceCreate, user: CurrentUser, db: Database) -> DeviceRegistered:
    vehicle = owned_vehicle(db, payload.vehicle_id, user.id)
    if not payload.imei.strip() or not payload.name.strip():
        raise HTTPException(status_code=422, detail="IMEI and device name cannot be blank")
    if payload.person_id is not None:
        person = db.scalar(
            select(Person).where(Person.id == payload.person_id, Person.owner_id == user.id)
        )
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
    token = create_device_token()
    device = Device(
        vehicle_id=vehicle.id,
        owner_id=user.id,
        person_id=payload.person_id,
        imei=payload.imei.strip(),
        name=payload.name.strip(),
        model=payload.model,
        company_name=payload.company_name,
        sim_number=payload.sim_number,
        token_hash=hash_device_token(token),
    )
    db.add(device)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="A device with this IMEI already exists") from None
    db.refresh(device)
    return DeviceRegistered(
        id=device.id,
        vehicle_id=device.vehicle_id,
        person_id=device.person_id,
        imei=device.imei,
        name=device.name,
        model=device.model,
        company_name=device.company_name,
        sim_number=device.sim_number,
        is_active=device.is_active,
        last_communication_at=device.last_communication_at,
        created_at=device.created_at,
        token=token,
    )


@app.get("/devices", response_model=list[DeviceSummary])
def list_devices(user: CurrentUser, db: Database) -> list[Device]:
    return list(
        db.scalars(
            select(Device).where(Device.owner_id == user.id).order_by(Device.created_at.desc())
        )
    )


@app.patch("/devices/{device_id}", response_model=DeviceSummary)
def update_device(
    device_id: uuid.UUID,
    payload: DeviceUpdate,
    user: CurrentUser,
    db: Database,
) -> Device:
    device = owned_device(db, device_id, user.id)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Device name cannot be null")
    if "person_id" in changes and changes["person_id"] is not None:
        person = db.scalar(
            select(Person).where(Person.id == changes["person_id"], Person.owner_id == user.id)
        )
        if person is None:
            raise HTTPException(status_code=404, detail="Person not found")
    for key, value in changes.items():
        setattr(device, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(device)
    return device


@app.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: uuid.UUID, user: CurrentUser, db: Database) -> None:
    device = owned_device(db, device_id, user.id)
    db.delete(device)
    db.commit()


@app.post("/persons", response_model=PersonRead, status_code=status.HTTP_201_CREATED)
def create_person(payload: PersonCreate, user: CurrentUser, db: Database) -> Person:
    person = Person(owner_id=user.id, **payload.model_dump())
    person.name = person.name.strip()
    if not person.name:
        raise HTTPException(status_code=422, detail="Person name cannot be blank")
    db.add(person)
    db.commit()
    db.refresh(person)
    return person


@app.get("/persons", response_model=list[PersonRead])
def list_persons(user: CurrentUser, db: Database) -> list[Person]:
    return list(db.scalars(select(Person).where(Person.owner_id == user.id).order_by(Person.name)))


@app.patch("/persons/{person_id}", response_model=PersonRead)
def update_person(
    person_id: uuid.UUID,
    payload: PersonUpdate,
    user: CurrentUser,
    db: Database,
) -> Person:
    person = db.scalar(select(Person).where(Person.id == person_id, Person.owner_id == user.id))
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Person name cannot be null")
    for key, value in changes.items():
        setattr(person, key, value.strip() if isinstance(value, str) else value)
    db.commit()
    db.refresh(person)
    return person


@app.delete("/persons/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person(person_id: uuid.UUID, user: CurrentUser, db: Database) -> None:
    person = db.scalar(select(Person).where(Person.id == person_id, Person.owner_id == user.id))
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    db.delete(person)
    db.commit()


@app.post("/ingest/{device_id}", response_model=LocationRead, status_code=status.HTTP_201_CREATED)
async def ingest_location(
    device_id: uuid.UUID,
    payload: LocationInput,
    db: Database,
    background_tasks: BackgroundTasks,
    x_device_token: Annotated[str, Header()],
    idempotency_key: Annotated[str, Header(min_length=8, max_length=64)],
) -> Location:
    device = db.get(Device, device_id)
    if device is None or not device.is_active or device.token_hash != hash_device_token(x_device_token):
        raise HTTPException(status_code=401, detail="Invalid device credentials")
    if db.scalar(
        select(LocationBatch).where(
            LocationBatch.device_id == device_id,
            LocationBatch.batch_key == idempotency_key,
        )
    ) is not None:
        raise HTTPException(status_code=409, detail="This location batch was already submitted")

    vehicle = db.get(Vehicle, device.vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Device vehicle no longer exists")
    point = Location(vehicle_id=vehicle.id, **payload.model_dump())
    device.last_communication_at = payload.recorded_at
    db.add(point)
    db.flush()
    db.add(LocationBatch(device_id=device.id, batch_key=idempotency_key))

    if db.bind is not None and db.bind.dialect.name == "postgresql":
        active_fence_ids = select(GeofenceState.geofence_id).where(
            GeofenceState.vehicle_id == vehicle.id,
            GeofenceState.is_inside.is_(True),
        )
        nearby_fences = text(
            "ST_DWithin("
            "ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography, "
            "ST_SetSRID(ST_MakePoint(geofences.longitude, geofences.latitude), 4326)::geography, "
            "geofences.radius_m)"
        )
        fence_query = (
            select(Geofence)
            .where(
                Geofence.owner_id == vehicle.owner_id,
                or_(nearby_fences, Geofence.id.in_(active_fence_ids)),
            )
            .params(longitude=payload.longitude, latitude=payload.latitude)
        )
    else:
        fence_query = select(Geofence).where(Geofence.owner_id == vehicle.owner_id)
    geofences = db.scalars(fence_query).all()
    geofence_transition = False
    for geofence in geofences:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            inside = bool(
                db.execute(
                    text(
                        "SELECT ST_DWithin("
                        "ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography, "
                        "ST_SetSRID(ST_MakePoint(:fence_longitude, :fence_latitude), 4326)::geography, "
                        ":radius_m)"
                    ),
                    {
                        "longitude": payload.longitude,
                        "latitude": payload.latitude,
                        "fence_longitude": geofence.longitude,
                        "fence_latitude": geofence.latitude,
                        "radius_m": geofence.radius_m,
                    },
                ).scalar_one()
            )
        else:
            inside = (
                distance_meters(payload.latitude, payload.longitude, geofence.latitude, geofence.longitude)
                <= geofence.radius_m
            )
        state = db.scalar(
            select(GeofenceState).where(
                GeofenceState.geofence_id == geofence.id,
                GeofenceState.vehicle_id == vehicle.id,
            )
        )
        if state is None:
            db.add(GeofenceState(geofence_id=geofence.id, vehicle_id=vehicle.id, is_inside=inside))
            if inside:
                geofence_transition = True
                db.add(
                    Alert(
                        owner_id=vehicle.owner_id,
                        vehicle_id=vehicle.id,
                        geofence_id=geofence.id,
                        event="entered",
                        alarm_type="geofence",
                        occurred_at=payload.recorded_at,
                        latitude=payload.latitude,
                        longitude=payload.longitude,
                    )
                )
        elif state.is_inside != inside:
            state.is_inside = inside
            geofence_transition = True
            db.add(
                Alert(
                    owner_id=vehicle.owner_id,
                    vehicle_id=vehicle.id,
                    geofence_id=geofence.id,
                    event="entered" if inside else "exited",
                    alarm_type="geofence",
                    occurred_at=payload.recorded_at,
                    latitude=payload.latitude,
                    longitude=payload.longitude,
                )
            )
    db.commit()
    db.refresh(point)
    await tracking_connections.broadcast(
        vehicle.owner_id,
        {
            "type": "location.update",
            "device_id": str(device.id),
            "vehicle_id": str(vehicle.id),
            "imei": device.imei,
            "recorded_at": payload.recorded_at.isoformat(),
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "speed_kph": payload.speed_kph,
            "heading": payload.heading,
            "ignition_on": payload.ignition_on,
        },
    )
    if geofence_transition:
        push_tokens = list(
            db.scalars(select(PushToken.token).where(PushToken.owner_id == vehicle.owner_id))
        )
        background_tasks.add_task(
            send_push_notifications,
            push_tokens,
            title="Geofence event",
            body=f"{device.name} reported a geofence transition",
            data={"type": "geofence", "device_id": str(device.id), "vehicle_id": str(vehicle.id)},
        )
    return point


@app.post(
    "/ingest/{device_id}/health",
    response_model=HealthRead,
    status_code=status.HTTP_201_CREATED,
)
def ingest_health(
    device_id: uuid.UUID,
    payload: HealthInput,
    db: Database,
    x_device_token: Annotated[str, Header()],
) -> HealthRecord:
    device = db.get(Device, device_id)
    if device is None or not device.is_active or device.token_hash != hash_device_token(x_device_token):
        raise HTTPException(status_code=401, detail="Invalid device credentials")
    record = HealthRecord(device_id=device.id, **payload.model_dump())
    device.last_communication_at = payload.recorded_at
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.get("/devices/{device_id}/health", response_model=list[HealthRead])
def health_history(
    device_id: uuid.UUID,
    user: CurrentUser,
    db: Database,
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1_000,
) -> list[HealthRecord]:
    device = owned_device(db, device_id, user.id)
    start, end = validate_time_range(start, end)
    return list(
        db.scalars(
            select(HealthRecord)
            .where(
                HealthRecord.device_id == device.id,
                HealthRecord.recorded_at >= start,
                HealthRecord.recorded_at <= end,
            )
            .order_by(HealthRecord.recorded_at)
            .limit(limit)
        )
    )


@app.get("/devices/{device_id}/health.csv")
def export_health_history(
    device_id: uuid.UUID,
    user: CurrentUser,
    db: Database,
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
) -> StreamingResponse:
    device = owned_device(db, device_id, user.id)
    records = health_history(device_id, user, db, start, end, 10_000)
    content = io.StringIO()
    writer = csv.writer(content)
    writer.writerow(
        [
            "recorded_at",
            "heart_rate_bpm",
            "blood_pressure_systolic",
            "blood_pressure_diastolic",
            "temperature_c",
            "step_count",
            "sleep_hours",
            "blood_oxygen_percent",
            "battery_percent",
        ]
    )
    for record in records:
        writer.writerow(
            [
                record.recorded_at.isoformat(),
                record.heart_rate_bpm,
                record.blood_pressure_systolic,
                record.blood_pressure_diastolic,
                record.temperature_c,
                record.step_count,
                record.sleep_hours,
                record.blood_oxygen_percent,
                record.battery_percent,
            ]
        )
    return StreamingResponse(
        iter([content.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="health-{device.imei}.csv"'},
    )


@app.post(
    "/ingest/{device_id}/alarms",
    response_model=AlertRead,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_alarm(
    device_id: uuid.UUID,
    payload: AlarmInput,
    db: Database,
    background_tasks: BackgroundTasks,
    x_device_token: Annotated[str, Header()],
) -> Alert:
    device = db.get(Device, device_id)
    if device is None or not device.is_active or device.token_hash != hash_device_token(x_device_token):
        raise HTTPException(status_code=401, detail="Invalid device credentials")
    vehicle = db.get(Vehicle, device.vehicle_id)
    if vehicle is None:
        raise HTTPException(status_code=404, detail="Device vehicle no longer exists")
    alert = Alert(
        owner_id=device.owner_id,
        vehicle_id=vehicle.id,
        device_id=device.id,
        event="alarm",
        alarm_type=payload.alarm_type.strip().lower().replace(" ", "_"),
        details=payload.details,
        occurred_at=payload.occurred_at,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )
    device.last_communication_at = payload.occurred_at
    db.add(alert)
    db.commit()
    db.refresh(alert)
    tokens = list(db.scalars(select(PushToken.token).where(PushToken.owner_id == device.owner_id)))
    background_tasks.add_task(
        send_push_notifications,
        tokens,
        title=f"Device alarm: {alert.alarm_type}",
        body=alert.details or f"{device.name} reported {alert.alarm_type}",
        data={
            "type": "alarm",
            "alarm_id": str(alert.id),
            "device_id": str(device.id),
            "vehicle_id": str(vehicle.id),
        },
    )
    await tracking_connections.broadcast(
        device.owner_id,
        {
            "type": "alarm.created",
            "alarm_id": str(alert.id),
            "device_id": str(device.id),
            "vehicle_id": str(vehicle.id),
            "alarm_type": alert.alarm_type,
            "occurred_at": alert.occurred_at.isoformat(),
        },
    )
    return alert


@app.get("/vehicles/{vehicle_id}/location", response_model=LocationRead)
def latest_location(vehicle_id: uuid.UUID, user: CurrentUser, db: Database) -> Location:
    owned_vehicle(db, vehicle_id, user.id)
    point = db.scalar(
        select(Location)
        .where(Location.vehicle_id == vehicle_id)
        .order_by(Location.recorded_at.desc(), Location.id.desc())
        .limit(1)
    )
    if point is None:
        raise HTTPException(status_code=404, detail="No location has been received for this vehicle")
    return point


@app.get("/vehicles/{vehicle_id}/history", response_model=list[LocationRead])
def location_history(
    vehicle_id: uuid.UUID,
    user: CurrentUser,
    db: Database,
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1_000,
) -> list[Location]:
    owned_vehicle(db, vehicle_id, user.id)
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(status_code=422, detail="start and end must include a timezone")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if start >= end:
        raise HTTPException(status_code=422, detail="start must be earlier than end")
    return list(
        db.scalars(
            select(Location)
            .where(
                Location.vehicle_id == vehicle_id,
                Location.recorded_at >= start,
                Location.recorded_at <= end,
            )
            .order_by(Location.recorded_at)
            .limit(limit)
        )
    )


@app.get("/vehicles/{vehicle_id}/trips", response_model=list[TripRead])
def trip_history(
    vehicle_id: uuid.UUID,
    user: CurrentUser,
    db: Database,
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
) -> list[TripRead]:
    points = location_history(vehicle_id, user, db, start, end, 10_000)
    trips: list[list[Location]] = []
    for point in points:
        if point.ignition_on is False:
            continue
        if not trips or (point.recorded_at - trips[-1][-1].recorded_at).total_seconds() > 30 * 60:
            trips.append([point])
        else:
            trips[-1].append(point)
    return [
        TripRead(
            started_at=trip[0].recorded_at,
            ended_at=trip[-1].recorded_at,
            start_latitude=trip[0].latitude,
            start_longitude=trip[0].longitude,
            end_latitude=trip[-1].latitude,
            end_longitude=trip[-1].longitude,
            point_count=len(trip),
        )
        for trip in trips
    ]


@app.post("/geofences", response_model=GeofenceRead, status_code=status.HTTP_201_CREATED)
def create_geofence(payload: GeofenceCreate, user: CurrentUser, db: Database) -> Geofence:
    values = payload.model_dump()
    values["name"] = values["name"].strip()
    if not values["name"]:
        raise HTTPException(status_code=422, detail="Geofence name cannot be blank")
    geofence = Geofence(owner_id=user.id, **values)
    db.add(geofence)
    db.commit()
    db.refresh(geofence)
    return geofence


@app.get("/geofences", response_model=list[GeofenceRead])
def list_geofences(user: CurrentUser, db: Database) -> list[Geofence]:
    return list(db.scalars(select(Geofence).where(Geofence.owner_id == user.id)))


@app.delete("/geofences/{geofence_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_geofence(geofence_id: uuid.UUID, user: CurrentUser, db: Database) -> None:
    geofence = db.scalar(
        select(Geofence).where(Geofence.id == geofence_id, Geofence.owner_id == user.id)
    )
    if geofence is None:
        raise HTTPException(status_code=404, detail="Geofence not found")
    db.delete(geofence)
    db.commit()


@app.get("/dashboard/stats", response_model=DashboardStats)
def dashboard_stats(user: CurrentUser, db: Database) -> DashboardStats:
    since = datetime.now(timezone.utc) - timedelta(minutes=settings.online_window_minutes)
    return DashboardStats(
        device_count=db.scalar(
            select(func.count()).select_from(Device).where(Device.owner_id == user.id)
        )
        or 0,
        active_device_count=db.scalar(
            select(func.count())
            .select_from(Device)
            .where(Device.owner_id == user.id, Device.is_active.is_(True))
        )
        or 0,
        online_device_count=db.scalar(
            select(func.count())
            .select_from(Device)
            .where(
                Device.owner_id == user.id,
                Device.is_active.is_(True),
                Device.last_communication_at >= since,
            )
        )
        or 0,
        unread_alarm_count=db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(Alert.owner_id == user.id, Alert.read.is_(False))
        )
        or 0,
    )


@app.get("/reports/summary")
def report_summary(
    user: CurrentUser,
    db: Database,
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
) -> dict[str, int | str]:
    start, end = validate_time_range(start, end)
    owned_device_ids = select(Device.id).where(Device.owner_id == user.id)
    owned_vehicle_ids = select(Vehicle.id).where(Vehicle.owner_id == user.id)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "device_count": db.scalar(
            select(func.count()).select_from(Device).where(Device.owner_id == user.id)
        )
        or 0,
        "location_point_count": db.scalar(
            select(func.count())
            .select_from(Location)
            .where(
                Location.vehicle_id.in_(owned_vehicle_ids),
                Location.recorded_at >= start,
                Location.recorded_at <= end,
            )
        )
        or 0,
        "health_record_count": db.scalar(
            select(func.count())
            .select_from(HealthRecord)
            .where(
                HealthRecord.device_id.in_(owned_device_ids),
                HealthRecord.recorded_at >= start,
                HealthRecord.recorded_at <= end,
            )
        )
        or 0,
        "alarm_count": db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.owner_id == user.id,
                Alert.occurred_at >= start,
                Alert.occurred_at <= end,
            )
        )
        or 0,
    }

@app.get("/alerts", response_model=list[AlertRead])
def list_alerts(
    user: CurrentUser,
    db: Database,
    unread_only: bool = False,
    alarm_type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> list[Alert]:
    query = select(Alert).where(Alert.owner_id == user.id)
    if unread_only:
        query = query.where(Alert.read.is_(False))
    if alarm_type:
        query = query.where(Alert.alarm_type == alarm_type.strip().lower())
    if start is not None or end is not None:
        if start is None or end is None:
            raise HTTPException(status_code=422, detail="Both start and end are required")
        start, end = validate_time_range(start, end)
        query = query.where(Alert.occurred_at >= start, Alert.occurred_at <= end)
    return list(db.scalars(query.order_by(Alert.occurred_at.desc()).limit(limit)))


@app.patch("/alerts/{alert_id}/read", response_model=AlertRead)
def mark_alert_read(alert_id: uuid.UUID, user: CurrentUser, db: Database) -> Alert:
    alert = db.scalar(select(Alert).where(Alert.id == alert_id, Alert.owner_id == user.id))
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.read = True
    db.commit()
    db.refresh(alert)
    return alert


@app.post(
    "/notifications/tokens",
    response_model=PushTokenRead,
    status_code=status.HTTP_201_CREATED,
)
def register_push_token(payload: PushTokenCreate, user: CurrentUser, db: Database) -> PushTokenRead:
    existing = db.scalar(select(PushToken).where(PushToken.token == payload.token))
    if existing is not None:
        if existing.owner_id != user.id:
            raise HTTPException(status_code=409, detail="This push token is already registered")
        return PushTokenRead(id=existing.id, registered=True)
    token = PushToken(owner_id=user.id, token=payload.token)
    db.add(token)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This push token is already registered") from None
    db.refresh(token)
    return PushTokenRead(id=token.id, registered=True)


@app.delete("/notifications/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_token(token_id: int, user: CurrentUser, db: Database) -> None:
    token = db.scalar(select(PushToken).where(PushToken.id == token_id, PushToken.owner_id == user.id))
    if token is None:
        raise HTTPException(status_code=404, detail="Push token not found")
    db.delete(token)
    db.commit()


@app.websocket("/ws/tracking")
async def live_tracking(websocket: WebSocket, db: Database) -> None:
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Firebase ID token is required")
        return
    try:
        claims = verify_firebase_id_token(token)
    except (auth.InvalidIdTokenError, auth.ExpiredIdTokenError, auth.RevokedIdTokenError, auth.UserDisabledError):
        await websocket.close(code=1008, reason="Invalid Firebase ID token")
        return
    except (firebase_admin.exceptions.FirebaseError, GoogleAuthError, OSError):
        logger.exception("Firebase Authentication failed for live-tracking connection")
        await websocket.close(code=1011, reason="Authentication service unavailable")
        return

    firebase_uid = claims.get("uid")
    if not isinstance(firebase_uid, str) or not firebase_uid:
        await websocket.close(code=1008, reason="Firebase token has no user ID")
        return
    user = db.scalar(select(User).where(User.firebase_uid == firebase_uid))
    if user is None:
        await websocket.close(code=1008, reason="User profile not found; call GET /auth/me first")
        return
    await tracking_connections.connect(user.id, websocket)
    try:
        await websocket.send_json({"type": "connected"})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await tracking_connections.disconnect(user.id, websocket)
