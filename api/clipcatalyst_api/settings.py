"""Environment-driven configuration. Every knob has a sane single-box default."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _find_ffmpeg(name: str) -> str:
    """Resolve ffmpeg/ffprobe: explicit env → PATH → imageio-ffmpeg fallback."""
    env = os.environ.get(f"CC_{name.upper()}_BIN")
    if env:
        return env
    on_path = shutil.which(name)
    if on_path:
        return on_path
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return name  # let subprocess fail with a clear message


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    storage: str  # "local" | "s3"
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    redis_url: str
    queue: str  # "redis" | "eager"
    transcriber: str  # "faster-whisper" | "fake"
    whisper_model: str
    whisper_device: str  # "auto" | "cuda" | "cpu"
    whisper_compute: str  # "default" | "float16" | "int8" ...
    ffmpeg_bin: str
    ffprobe_bin: str
    cors_origins: list[str] = field(default_factory=list)
    max_upload_bytes: int = 2_000_000_000
    public_base_url: str = ""  # e.g. https://api.clipcatalyst.io; "" = relative
    fake_transcript_path: str = ""
    api_token: str = ""  # "" = open (dev); set = Bearer token required on writes
    job_ttl_hours: int = 48  # sources/clips/rows older than this are reaped
    render_timeout_s: int = 900  # hard ceiling on a single ffmpeg render
    face_tracking: str = "on"  # "on" | "off" — reframe on the speaker
    diarization: str = "on"  # "on" | "off" — color captions per speaker
    session_ttl_days: int = 30  # account sessions expire this many days out
    mailer: str = "none"  # "none" | "console" | "resend" — account email
    resend_api_key: str = ""  # only for CC_MAILER=resend
    billing: str = "off"  # "stripe" | "fake" | "off" — plan upgrades
    stripe_secret_key: str = ""  # sk_… (never logged)
    stripe_webhook_secret: str = ""  # whsec_… — webhook signature verification
    stripe_price_starter: str = ""  # price_… ids mapping Stripe prices → plans
    stripe_price_pro: str = ""
    stripe_price_enterprise: str = ""
    frontend_origin: str = "http://localhost:3000"  # checkout/portal return URLs

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.clips_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("CC_DATA_DIR", "/tmp/clipcatalyst-data"))
    return Settings(
        data_dir=data_dir,
        db_path=Path(os.environ.get("CC_DB_PATH", str(data_dir / "jobs.sqlite3"))),
        storage=os.environ.get("CC_STORAGE", "local"),
        s3_bucket=os.environ.get("CC_S3_BUCKET", ""),
        s3_prefix=os.environ.get("CC_S3_PREFIX", "clipcatalyst"),
        s3_region=os.environ.get("CC_S3_REGION", "us-east-1"),
        redis_url=os.environ.get("CC_REDIS_URL", "redis://localhost:6379/0"),
        queue=os.environ.get("CC_QUEUE", "redis"),
        transcriber=os.environ.get("CC_TRANSCRIBER", "faster-whisper"),
        whisper_model=os.environ.get("CC_WHISPER_MODEL", "distil-large-v3"),
        whisper_device=os.environ.get("CC_WHISPER_DEVICE", "auto"),
        whisper_compute=os.environ.get("CC_WHISPER_COMPUTE", "default"),
        ffmpeg_bin=_find_ffmpeg("ffmpeg"),
        ffprobe_bin=_find_ffmpeg("ffprobe"),
        cors_origins=[
            o.strip()
            for o in os.environ.get(
                "CC_CORS_ORIGINS",
                "https://allentackie-ops.github.io,http://localhost:3000,http://localhost:3100",
            ).split(",")
            if o.strip()
        ],
        max_upload_bytes=int(os.environ.get("CC_MAX_UPLOAD_BYTES", "2000000000")),
        public_base_url=os.environ.get("CC_PUBLIC_BASE_URL", "").rstrip("/"),
        fake_transcript_path=os.environ.get("CC_FAKE_TRANSCRIPT_PATH", ""),
        api_token=os.environ.get("CC_API_TOKEN", ""),
        job_ttl_hours=int(os.environ.get("CC_JOB_TTL_HOURS", "48")),
        render_timeout_s=int(os.environ.get("CC_RENDER_TIMEOUT_S", "900")),
        face_tracking=os.environ.get("CC_FACE_TRACKING", "on"),
        diarization=os.environ.get("CC_DIARIZATION", "on"),
        session_ttl_days=int(os.environ.get("CC_SESSION_TTL_DAYS", "30")),
        mailer=os.environ.get("CC_MAILER", "none"),
        resend_api_key=os.environ.get("CC_RESEND_API_KEY", ""),
        billing=os.environ.get("CC_BILLING", "off"),
        stripe_secret_key=os.environ.get("CC_STRIPE_SECRET_KEY", ""),
        stripe_webhook_secret=os.environ.get("CC_STRIPE_WEBHOOK_SECRET", ""),
        stripe_price_starter=os.environ.get("CC_STRIPE_PRICE_STARTER", ""),
        stripe_price_pro=os.environ.get("CC_STRIPE_PRICE_PRO", ""),
        stripe_price_enterprise=os.environ.get("CC_STRIPE_PRICE_ENTERPRISE", ""),
        frontend_origin=os.environ.get(
            "CC_FRONTEND_ORIGIN", "http://localhost:3000"
        ).rstrip("/"),
    )
