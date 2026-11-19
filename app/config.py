import os
from dataclasses import dataclass


def _int(name, default):
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    recorder_url: str
    recorder_user: str
    recorder_device: str
    recorder_auth: str
    app_tz: str
    places_provider: str
    google_places_key: str
    stop_min_minutes: int
    stop_radius_m: int
    stop_max_gap_min: int
    max_acc_m: int
    app_password: str
    data_dir: str

    @classmethod
    def from_env(cls):
        e = os.environ.get
        return cls(
            recorder_url=e("RECORDER_URL", "").rstrip("/"),
            recorder_user=e("RECORDER_USER", ""),
            recorder_device=e("RECORDER_DEVICE", ""),
            recorder_auth=e("RECORDER_AUTH", ""),
            app_tz=e("APP_TZ", "America/Los_Angeles"),
            places_provider=e("PLACES_PROVIDER", "osm"),
            google_places_key=e("GOOGLE_PLACES_KEY", ""),
            stop_min_minutes=_int("STOP_MIN_MINUTES", 10),
            stop_radius_m=_int("STOP_RADIUS_M", 100),
            stop_max_gap_min=_int("STOP_MAX_GAP_MIN", 15),
            max_acc_m=_int("MAX_ACC_M", 100),
            app_password=e("APP_PASSWORD", ""),
            data_dir=e("DATA_DIR", "/data"),
        )
