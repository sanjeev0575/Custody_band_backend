# Trackin360 backend

FastAPI API for a React fleet-tracking dashboard. PostgreSQL is the primary database and PostGIS provides distance checks for circular geofences. Firebase Authentication verifies user ID tokens; Firebase Cloud Messaging delivers alarm and geofence notifications. GPS and wearable devices submit normalized JSON to the device-ingest API.

## Local setup (Windows PowerShell)

1. Install PostgreSQL with the PostGIS extension and create a database named `trackin360`.
2. Create and activate a virtual environment, install this project, and copy the environment template:

   ```powershell
   cd e:\backend
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[test]"
   Copy-Item .env.example .env
   ```

3. Edit `.env` with the PostgreSQL connection string and Firebase project ID. For local Firebase Admin access, set `GOOGLE_APPLICATION_CREDENTIALS` in the PowerShell session to the full path of a Firebase service-account JSON file. Do not commit that file. On production, prefer the host's Application Default Credentials or workload identity.
4. Run the API:

   ```powershell
   uvicorn app.main:app --reload
   ```

The API initializes missing tables and enables PostGIS at startup; the database role must be allowed to create the PostGIS extension. Existing databases are not automatically migrated by SQLAlchemy `create_all`, so back up and migrate an existing database before upgrading. Open `http://127.0.0.1:8000/docs` for the interactive API.

## Authentication and authorization

The React app signs in with the Firebase client SDK and sends its Firebase ID token using `Authorization: Bearer <id-token>`. FastAPI validates token signature, expiry, and revocation via Firebase Admin. The first authenticated `GET /auth/me` call creates the local user profile. API records are scoped to that user's account.

GPS devices use a separate randomly generated device token. The raw token is shown once when a device is registered; store it securely on the device and send it as `X-Device-Token`. Never use a Firebase service-account key in the React app.

## Main API

All listed management endpoints require Firebase bearer authentication unless marked device-ingest.

| Capability | Routes |
| --- | --- |
| User profile and dashboard | `GET /auth/me`, `GET /dashboard/stats` |
| Fleet/vehicle records | `POST /vehicles`, `GET /vehicles`, `DELETE /vehicles/{vehicle_id}` |
| Device registry and Monitoring Center feed | `POST /devices`, `GET /devices`, `GET /monitoring/devices`, `PATCH /devices/{device_id}`, `DELETE /devices/{device_id}` |
| Persons | `POST /persons`, `GET /persons`, `PATCH /persons/{person_id}`, `DELETE /persons/{person_id}` |
| GPS location ingest (device token) | `POST /ingest/{device_id}` with `X-Device-Token` and `Idempotency-Key` |
| Latest location, history, playback and trips | `GET /vehicles/{vehicle_id}/location`, `/history`, `/trips` |
| Health / wearable measurements | `POST /ingest/{device_id}/health` (device token), `GET /devices/{device_id}/health`, `GET /devices/{device_id}/health.csv` |
| Alarms | `POST /ingest/{device_id}/alarms` (device token), `GET /alerts`, `PATCH /alerts/{alert_id}/read` |
| Geofences | `POST /geofences`, `GET /geofences`, `DELETE /geofences/{geofence_id}` |
| Reporting | `GET /reports/summary?start=...&end=...` |
| Push notifications | `POST /notifications/tokens`, `DELETE /notifications/tokens/{token_id}` |

Ingest timestamps must include a timezone. Geofence radius is measured in meters. Device online status defaults to having communicated in the last 10 minutes and can be adjusted with `ONLINE_WINDOW_MINUTES`.

`GET /monitoring/devices` returns a paginated list of the authenticated user's devices, vehicle names, connection status, and latest location. Optional query parameters are `imei` and `company` (case-insensitive partial matches), `status` (`all`, `online`, `offline`, or `not_active`), `limit` (1-500), and `offset`. Each new GPS ingest is tagged with its device ID so multiple trackers assigned to the same vehicle retain their individual latest positions. Older location rows without a device ID are still returned as a vehicle-level fallback.

The Monitoring Center API supplies positions and status; it does not provide map tiles or a satellite layer. The frontend must render the `latest_location` coordinates using its configured map provider. GPS hardware must send location updates to `POST /ingest/{device_id}`; the API cannot determine a device's position without those reports.

## Live tracking

Connect the browser to `ws://127.0.0.1:8000/ws/tracking?token=<firebase-id-token>` (use `wss://` in production). A connection only receives location/alarm events belonging to its authenticated user. Devices send GPS points to the normal ingest endpoint; successful points are persisted to PostgreSQL before a `location.update` event is broadcast. The built-in connection manager is process-local: use a shared pub/sub such as Redis when running multiple API workers or instances.

## PostGIS and device protocols

The app requires the PostGIS extension in PostgreSQL. At startup it enables the extension and creates a GIST index used by PostGIS distance queries for geofences. SQLite is not the production datastore; it is only used by the automated tests with a small Haversine fallback.

This API accepts normalized JSON GPS and health payloads. Tracker models often use proprietary TCP/UDP protocols; a device-specific protocol adapter or gateway is required to translate those packets into the documented ingest payloads. This project does not claim compatibility with an undocumented vendor protocol.

## Tests

```powershell
python -m pytest
```
