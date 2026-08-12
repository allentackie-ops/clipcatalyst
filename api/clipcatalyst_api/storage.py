"""Storage backends: local disk for a single box, S3 for a fleet.

One interface, two implementations:

- ``upload_target(job_id)`` — where the client PUTs the source video.
- ``source_path(job_id)`` — a local path the pipeline can read (S3 downloads
  to the tmp dir first).
- ``source_exists(job_id)`` — has the source been uploaded yet?
- ``put_clip(job_id, path, name)`` — persist a rendered clip, returns the key.
- ``clip_key(job_id, name)`` — the durable storage key for a clip. THIS is what
  belongs in ``clips_json`` (never a signed URL — those expire).
- ``clip_url(job_id, name_or_key)`` — a URL a browser can GET the clip from,
  minted on demand at read time. Accepts either a bare clip name
  (``clip-01.mp4``) or a stored key, so it stays correct whichever the caller
  persisted (see the S3 read-time-signing contract note below).
- ``delete_source(job_id)`` — drop the uploaded source once a job is terminal
  (local: the uploads file; S3: the bucket object AND the tmp staging copy).

The clip library (LIBRARY.md Part 2) has its own four calls and its own root,
which is the point — a job's clips directory is swept by the reaper and by the
lost-claim cleanup, and a saved clip must be somewhere neither of them can
reach:

- ``put_library_clip(user_id, name, path)`` — persist a saved clip; returns the
  ``file_path`` to store on the row (local: relative to ``library_dir``; S3:
  the object key).
- ``library_clip_file(file_path)`` — a local file the API can serve, or None.
- ``library_clip_url(file_path)`` — a URL to send the browser to instead
  ('' when the file is local and the API serves it itself).
- ``fetch_library_clip(file_path, dest)`` — a local file to READ, downloading to
  ``dest`` when the backend has no local copy. What publishing uses: an adapter
  streams bytes off a path and must not have to know where this product keeps
  video (PUBLISH.md Part 3).
- ``delete_library_clip(file_path)`` — remove one stored clip.

boto3 (and its bundled botocore) are imported lazily so the API/worker
containers only need them when ``CC_STORAGE=s3``.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Protocol

from .settings import Settings

_PRESIGNED_PUT_EXPIRES = 3600  # 1 h to upload
_PRESIGNED_GET_EXPIRES = 7 * 24 * 3600  # 7 days, the S3 maximum

#: The containers a clip can be stored in — mp4 from the cloud renderer, and
#: mp4 OR webm from the browser, whose MediaRecorder picks whichever the
#: device supports. Anything else never becomes a stored file (main.py sniffs
#: the bytes before writing them).
CLIP_MEDIA_TYPES = {"mp4": "video/mp4", "webm": "video/webm"}


def clip_media_type(name: str) -> str | None:
    """The media type for a stored clip name, from its extension."""
    return CLIP_MEDIA_TYPES.get(name.rsplit(".", 1)[-1].lower())


class Storage(Protocol):
    def upload_target(self, job_id: str) -> dict: ...

    def source_path(self, job_id: str) -> Path: ...

    def source_exists(self, job_id: str) -> bool: ...

    def put_clip(self, job_id: str, path: str | Path, name: str) -> str: ...

    def clip_key(self, job_id: str, name: str) -> str: ...

    def clip_url(self, job_id: str, name_or_key: str) -> str: ...

    def delete_source(self, job_id: str) -> None: ...

    def put_library_clip(self, user_id: str, name: str, path: str | Path) -> str: ...

    def library_clip_file(self, file_path: str) -> Path | None: ...

    def library_clip_url(self, file_path: str) -> str: ...

    def fetch_library_clip(self, file_path: str, dest: Path) -> Path | None: ...

    def delete_library_clip(self, file_path: str) -> None: ...


def get_storage(settings: Settings) -> Storage:
    if settings.storage == "local":
        return LocalStorage(settings)
    if settings.storage == "s3":
        return S3Storage(settings)
    raise ValueError(f"Unknown CC_STORAGE {settings.storage!r} — use 'local' or 's3'.")


class LocalStorage:
    """Everything under settings.data_dir; files served by the API itself."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def upload_target(self, job_id: str) -> dict:
        return {"mode": "put", "url": f"/v1/uploads/{job_id}"}

    def source_path(self, job_id: str) -> Path:
        return self._settings.uploads_dir / f"{job_id}.src"

    def source_exists(self, job_id: str) -> bool:
        return self.source_path(job_id).is_file()

    def put_clip(self, job_id: str, path: str | Path, name: str) -> str:
        dest = self._settings.clips_dir / job_id / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        return self.clip_key(job_id, name)

    def clip_key(self, job_id: str, name: str) -> str:
        return f"{job_id}/{name}"

    def clip_url(self, job_id: str, name_or_key: str) -> str:
        # Relative when public_base_url is empty — the SPA resolves it against
        # the API origin it is already talking to.
        # Robust to either a bare clip name ("clip-01.mp4") or the stored key
        # ("<job_id>/clip-01.mp4"): the route already carries job_id, so we only
        # ever want the leaf filename here.
        leaf = name_or_key.rsplit("/", 1)[-1]
        return f"{self._settings.public_base_url}/v1/files/{job_id}/{leaf}"

    def delete_source(self, job_id: str) -> None:
        """Remove the uploaded source once the job is terminal. Never raises."""
        try:
            self.source_path(job_id).unlink(missing_ok=True)
        except OSError:
            pass

    # --- the clip library ------------------------------------------------ #

    def put_library_clip(self, user_id: str, name: str, path: str | Path) -> str:
        """Copy a rendered clip into the library and return its stored path.

        Under ``library_dir``, never under ``clips_dir``: the job reaper and
        the lost-claim cleanup both ``rmtree(clips_dir / job_id)``, and a
        library file in there would be deleted by a sweep that has no idea the
        library exists. The returned value is RELATIVE to the library root, so
        moving the data directory does not invalidate every stored row.
        """
        dest = self._library_root() / user_id / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        return f"{user_id}/{name}"

    def library_clip_file(self, file_path: str) -> Path | None:
        """The stored clip as a real file, or None if it isn't one.

        Resolved and then checked against the library root, the same
        belt-and-braces the files route uses: a stored value is data, and data
        that has been in a database is not automatically a safe path.
        """
        if not file_path:
            return None
        root = self._library_root()
        resolved = (root / file_path).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None
        return resolved

    def library_clip_url(self, file_path: str) -> str:
        """Local files are served by the API itself — there is nowhere to send
        the browser instead."""
        return ""

    def fetch_library_clip(self, file_path: str, dest: Path) -> Path | None:
        """The stored clip, already on this disk — `dest` is never written.

        Returning the real path rather than a copy is the point: a publish
        streams the file straight out of the library, so a local box spends no
        extra disk (and no extra time) to post a clip it already has.
        """
        return self.library_clip_file(file_path)

    def delete_library_clip(self, file_path: str) -> None:
        """Delete one stored clip, and prune the directory it leaves empty.

        Raises on a real filesystem failure, unlike ``delete_source``: this is
        a deliberate deletion whose caller (the retention reaper) only blanks
        the row once the file is genuinely gone, so a swallowed error would
        leak the file forever by convincing the reaper it had succeeded.
        """
        if not file_path:
            return
        root = self._library_root().resolve()
        resolved = (root / file_path).resolve()
        if not resolved.is_relative_to(root):
            return  # not ours to delete
        resolved.unlink(missing_ok=True)
        # A job's directory is empty once its last clip goes; the account's own
        # directory stays. rmdir refuses a non-empty directory, which is
        # exactly the check we want.
        parent = resolved.parent
        if parent != root and parent.is_relative_to(root):
            with contextlib.suppress(OSError):
                parent.rmdir()

    def _library_root(self) -> Path:
        return self._settings.library_dir


class S3Storage:
    """Presigned PUT for uploads, presigned GET for clips; tmp-dir staging.

    Contract note — clip persistence:
        Presigned GET URLs are short-lived (7-day S3 max). Persisting one in
        ``clips_json`` at render time means the link rots while the job row
        lives on. The durable value to store is the KEY (``clip_key``); the
        signed URL should be minted on demand at *read time* (in the status
        route), so every ``GET /v1/jobs/{id}`` hands back a fresh link.

        Until the worker/status route adopt that split, ``clip_url`` is written
        to be safe either way: it accepts a bare clip name OR a full stored key
        and always signs against the right object. So a caller may persist the
        key (preferred) or keep passing the name (legacy) without breaking.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

    def _s3(self):  # noqa: ANN202 - boto3 client, imported lazily on purpose
        if self._client is None:
            import boto3  # heavy + optional: only needed when CC_STORAGE=s3

            self._client = boto3.client("s3", region_name=self._settings.s3_region)
        return self._client

    def _key(self, *parts: str) -> str:
        prefix = self._settings.s3_prefix.strip("/")
        return "/".join(p for p in (prefix, *parts) if p)

    def _source_key(self, job_id: str) -> str:
        return self._key("uploads", f"{job_id}.src")

    def _resolve_clip_key(self, job_id: str, name_or_key: str) -> str:
        """Accept a bare clip name or an already-built key, return the key.

        A value carrying a path separator is treated as a full key (what
        ``clip_key``/``put_clip`` return); a bare filename gets the
        ``clips/<job_id>/`` location applied.
        """
        if "/" in name_or_key:
            return name_or_key
        return self.clip_key(job_id, name_or_key)

    def upload_target(self, job_id: str) -> dict:
        url = self._s3().generate_presigned_url(
            "put_object",
            Params={"Bucket": self._settings.s3_bucket, "Key": self._source_key(job_id)},
            ExpiresIn=_PRESIGNED_PUT_EXPIRES,
        )
        return {"mode": "put", "url": url}

    def source_path(self, job_id: str) -> Path:
        """Download the source to the tmp dir (once) so ffmpeg can seek it."""
        local = self._settings.tmp_dir / f"{job_id}.src"
        if not local.is_file():
            local.parent.mkdir(parents=True, exist_ok=True)
            self._s3().download_file(
                self._settings.s3_bucket, self._source_key(job_id), str(local)
            )
        return local

    def source_exists(self, job_id: str) -> bool:
        # Narrowed on purpose: only a genuine "object not found" is False. Any
        # other failure — boto3 not installed (ImportError from _s3()), bad
        # creds, a 403, a network error — must propagate so start_job surfaces
        # the real problem instead of a misleading 409 "upload the video first".
        from botocore.exceptions import ClientError

        try:
            self._s3().head_object(
                Bucket=self._settings.s3_bucket, Key=self._source_key(job_id)
            )
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("404", "NoSuchKey", "NotFound") or status == 404:
                return False
            raise
        return True

    def put_clip(self, job_id: str, path: str | Path, name: str) -> str:
        key = self.clip_key(job_id, name)
        self._s3().upload_file(
            str(path),
            self._settings.s3_bucket,
            key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        return key

    def clip_key(self, job_id: str, name: str) -> str:
        """Durable S3 key for a clip — persist THIS, not a signed URL."""
        return self._key("clips", job_id, name)

    def clip_url(self, job_id: str, name_or_key: str) -> str:
        """Presigned GET, minted on demand. See the class contract note."""
        return self._s3().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._settings.s3_bucket,
                "Key": self._resolve_clip_key(job_id, name_or_key),
            },
            ExpiresIn=_PRESIGNED_GET_EXPIRES,
        )

    def delete_source(self, job_id: str) -> None:
        """Drop the source once the job is terminal: the bucket object and the
        tmp staging download. Never raises — cleanup must not mask the outcome.
        """
        try:
            self._s3().delete_object(
                Bucket=self._settings.s3_bucket, Key=self._source_key(job_id)
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup, incl. missing boto3
            pass
        try:
            (self._settings.tmp_dir / f"{job_id}.src").unlink(missing_ok=True)
        except OSError:
            pass

    # --- the clip library ------------------------------------------------ #

    def put_library_clip(self, user_id: str, name: str, path: str | Path) -> str:
        """Upload a saved clip under the library prefix; returns its key.

        Its own prefix, not ``clips/``, for the same reason the local backend
        uses its own directory: the job sweeps delete by job, and a saved clip
        must not be reachable by anything that sweeps.
        """
        key = self._key("library", user_id, name)
        extra = {}
        media_type = clip_media_type(name)
        if media_type:
            extra["ContentType"] = media_type
        self._s3().upload_file(
            str(path), self._settings.s3_bucket, key, ExtraArgs=extra or None
        )
        return key

    def library_clip_file(self, file_path: str) -> Path | None:
        """Nothing local to serve — the browser goes to the presigned URL."""
        return None

    def library_clip_url(self, file_path: str) -> str:
        """Presigned GET for a stored clip, minted on demand at read time."""
        if not file_path:
            return ""
        return self._s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": self._settings.s3_bucket, "Key": file_path},
            ExpiresIn=_PRESIGNED_GET_EXPIRES,
        )

    def fetch_library_clip(self, file_path: str, dest: Path) -> Path | None:
        """Download a stored clip to `dest` so a publish can stream it.

        The caller owns `dest` and deletes it afterwards (worker._publish_run):
        this is a staging copy of somebody's video, and leaving it in the tmp
        directory would slowly turn "the clip expired and was deleted" into a
        promise the disk does not keep. A missing object returns None, which the
        caller reports as an expired clip rather than as a crash.
        """
        from botocore.exceptions import ClientError

        if not file_path:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3().download_file(self._settings.s3_bucket, file_path, str(dest))
        except ClientError:
            return None
        return dest

    def delete_library_clip(self, file_path: str) -> None:
        """Delete one stored clip. Raises — see LocalStorage for why."""
        if not file_path:
            return
        self._s3().delete_object(Bucket=self._settings.s3_bucket, Key=file_path)
