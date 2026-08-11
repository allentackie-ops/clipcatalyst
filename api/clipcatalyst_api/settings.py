"""Environment-driven configuration. Every knob has a sane single-box default."""

from __future__ import annotations

import ipaddress
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
    # Reverse proxies whose X-Forwarded-For we believe (ips or CIDR blocks).
    # Empty (the default) = trust nobody: the peer address IS the client.
    trusted_proxies: list[str] = field(default_factory=list)
    max_upload_bytes: int = 2_000_000_000
    public_base_url: str = ""  # e.g. https://api.clipcatalyst.io; "" = relative
    fake_transcript_path: str = ""
    api_token: str = ""  # "" = open (dev); set = Bearer token required on writes
    job_ttl_hours: int = 48  # sources/clips/rows older than this are reaped
    render_timeout_s: int = 900  # hard ceiling on a single ffmpeg render
    face_tracking: str = "on"  # "on" | "off" — reframe on the speaker
    diarization: str = "on"  # "on" | "off" — color captions per speaker
    session_ttl_days: int = 30  # account sessions expire this many days out
    # What the credential rate limiter does when it cannot reach Redis:
    # "off" (the default) refuses the attempt, "on" lets it through. See
    # auth.rate_limit_fails_open for why closed is the shipped choice.
    rate_limit_fail_open: str = "off"  # "on" | "off"
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

    @property
    def brand_dir(self) -> Path:
        """Uploaded brand logos, one file per account (see brandkit.py)."""
        return self.data_dir / "brand"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.uploads_dir,
            self.clips_dir,
            self.tmp_dir,
            self.brand_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def duplicate_price_ids(self) -> dict[str, list[str]]:
        """Stripe price ids configured for more than one plan.

        Maps the repeated price id to the env var names that carry it. The
        price → plan map (billing.price_to_plan) is a dict build, so a shared
        price would silently resolve to whichever plan happens to be last —
        never a decision worth guessing at.
        """
        by_price: dict[str, list[str]] = {}
        for name, price_id in (
            ("CC_STRIPE_PRICE_STARTER", self.stripe_price_starter),
            ("CC_STRIPE_PRICE_PRO", self.stripe_price_pro),
            ("CC_STRIPE_PRICE_ENTERPRISE", self.stripe_price_enterprise),
        ):
            if price_id:
                by_price.setdefault(price_id, []).append(name)
        return {price: names for price, names in by_price.items() if len(names) > 1}

    def wide_open_proxies(self) -> list[str]:
        """CC_TRUSTED_PROXIES entries that trust the ENTIRE address space.

        A zero-length prefix — ``0.0.0.0/0``, ``::/0``, or any spelling of
        them — matches every possible peer, which is the one value that turns
        the trusted-proxy rule inside out (see validate). Entries that are not
        addresses at all are ignored here exactly as ``auth._trusted_networks``
        ignores them: they trust nobody, so they are nobody's problem.
        """
        wide: list[str] = []
        for entry in self.trusted_proxies:
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if network.prefixlen == 0:
                wide.append(entry)
        return wide

    def validate(self) -> None:
        """Refuse to boot on a configuration that cannot enforce what it sells.

        Called from ``create_app`` so a misconfigured box fails loudly at
        startup instead of quietly serving unmetered renders (or granting the
        wrong plan) in production. Everything checked here is a deployment
        state, never client input.
        """
        for entry in self.wide_open_proxies():
            raise RuntimeError(
                f"CC_TRUSTED_PROXIES contains {entry!r}, which trusts EVERY "
                "address. The whole point of that list is telling a real "
                "reverse proxy apart from a stranger: with everyone trusted, "
                "any direct client speaks for itself and picks its own "
                "rate-limit bucket by rotating X-Forwarded-For per attempt, so "
                "the login limiter is not weakened — it is gone, and silently. "
                "Name the proxy's actual address or CIDR block (e.g. "
                "172.18.0.2 or 10.0.0.0/24), or leave CC_TRUSTED_PROXIES empty "
                "to trust nobody. See DEPLOY.md §7.2."
            )
        if self.billing == "off":
            return  # the dev default: no revenue to gate, no price map to build
        if not self.api_token:
            raise RuntimeError(
                f"CC_BILLING={self.billing!r} but CC_API_TOKEN is empty. With no "
                "founder token the job routes stay open to anonymous callers, "
                "who are not attached to any account — they skip plan "
                "entitlements and the monthly quota entirely (4K, unmetered), so "
                "every plan limit becomes optional. Set CC_API_TOKEN "
                "(openssl rand -hex 32) or set CC_BILLING=off. See DEPLOY.md §7.1."
            )
        for price_id, names in self.duplicate_price_ids().items():
            raise RuntimeError(
                f"{' and '.join(names)} are all set to {price_id!r}. One Stripe "
                "price cannot map to two plans — a webhook carrying it would "
                "grant whichever plan won the map build. Give each plan its own "
                "price id."
            )


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
        trusted_proxies=[
            p.strip()
            for p in os.environ.get("CC_TRUSTED_PROXIES", "").split(",")
            if p.strip()
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
        rate_limit_fail_open=os.environ.get("CC_RATE_LIMIT_FAIL_OPEN", "off"),
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
