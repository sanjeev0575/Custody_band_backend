from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/trackin360"
    firebase_project_id: str | None = None
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    online_window_minutes: int = 10

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
