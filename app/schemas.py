import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UserRead(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class VehicleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    registration: str | None = Field(default=None, max_length=32)


class VehicleRead(VehicleCreate):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeviceRead(BaseModel):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    token: str


class DeviceCreate(BaseModel):
    vehicle_id: uuid.UUID
    imei: str = Field(min_length=8, max_length=32)
    name: str = Field(default="GPS device", min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    company_name: str | None = Field(default=None, max_length=160)
    sim_number: str | None = Field(default=None, max_length=32)
    person_id: uuid.UUID | None = None


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    company_name: str | None = Field(default=None, max_length=160)
    sim_number: str | None = Field(default=None, max_length=32)
    person_id: uuid.UUID | None = None
    is_active: bool | None = None


class DeviceSummary(BaseModel):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    person_id: uuid.UUID | None
    imei: str
    name: str
    model: str | None
    company_name: str | None
    sim_number: str | None
    is_active: bool
    last_communication_at: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DeviceRegistered(DeviceSummary):
    token: str


class PersonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    notes: str | None = Field(default=None, max_length=4000)


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    notes: str | None = Field(default=None, max_length=4000)


class PersonRead(PersonCreate):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LocationInput(BaseModel):
    recorded_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_kph: float | None = Field(default=None, ge=0, le=500)
    heading: float | None = Field(default=None, ge=0, lt=360)
    ignition_on: bool | None = None

    @field_validator("recorded_at")
    @classmethod
    def normalize_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        return value.astimezone(timezone.utc)


class LocationRead(BaseModel):
    id: int
    vehicle_id: uuid.UUID
    recorded_at: datetime
    latitude: float
    longitude: float
    speed_kph: float | None
    heading: float | None
    ignition_on: bool | None

    model_config = ConfigDict(from_attributes=True)


class GeofenceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0, le=100_000)


class GeofenceRead(GeofenceCreate):
    id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HealthInput(BaseModel):
    recorded_at: datetime
    heart_rate_bpm: int | None = Field(default=None, ge=20, le=300)
    blood_pressure_systolic: int | None = Field(default=None, ge=30, le=300)
    blood_pressure_diastolic: int | None = Field(default=None, ge=20, le=200)
    temperature_c: float | None = Field(default=None, ge=25, le=50)
    step_count: int | None = Field(default=None, ge=0, le=2_000_000)
    sleep_hours: float | None = Field(default=None, ge=0, le=24)
    blood_oxygen_percent: float | None = Field(default=None, ge=0, le=100)
    battery_percent: float | None = Field(default=None, ge=0, le=100)

    @field_validator("recorded_at")
    @classmethod
    def normalize_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_measurement(self) -> "HealthInput":
        if all(
            getattr(self, field_name) is None
            for field_name in (
                "heart_rate_bpm",
                "blood_pressure_systolic",
                "blood_pressure_diastolic",
                "temperature_c",
                "step_count",
                "sleep_hours",
                "blood_oxygen_percent",
                "battery_percent",
            )
        ):
            raise ValueError("At least one health measurement is required")
        return self


class HealthRead(BaseModel):
    id: int
    device_id: uuid.UUID
    recorded_at: datetime
    heart_rate_bpm: int | None
    blood_pressure_systolic: int | None
    blood_pressure_diastolic: int | None
    temperature_c: float | None
    step_count: int | None
    sleep_hours: float | None
    blood_oxygen_percent: float | None
    battery_percent: float | None

    @field_validator("recorded_at")
    @classmethod
    def normalize_output_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    model_config = ConfigDict(from_attributes=True)


class AlertRead(BaseModel):
    id: uuid.UUID
    vehicle_id: uuid.UUID
    device_id: uuid.UUID | None
    geofence_id: uuid.UUID | None
    event: str
    alarm_type: str
    details: str | None
    occurred_at: datetime
    latitude: float | None
    longitude: float | None
    read: bool

    model_config = ConfigDict(from_attributes=True)


class AlarmInput(BaseModel):
    alarm_type: str = Field(min_length=2, max_length=48)
    occurred_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    details: str | None = Field(default=None, max_length=2000)

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_coordinate_pair(self) -> "AlarmInput":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if not self.alarm_type.strip():
            raise ValueError("alarm_type cannot be blank")
        return self


class PushTokenCreate(BaseModel):
    token: str = Field(min_length=16, max_length=4096)


class DashboardStats(BaseModel):
    device_count: int
    active_device_count: int
    online_device_count: int
    unread_alarm_count: int


class PushTokenRead(BaseModel):
    id: int
    registered: bool


class TripRead(BaseModel):
    started_at: datetime
    ended_at: datetime
    start_latitude: float
    start_longitude: float
    end_latitude: float
    end_longitude: float
    point_count: int
