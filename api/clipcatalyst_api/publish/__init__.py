"""Posting a finished clip to a connected channel (PUBLISH.md Part 3).

One protocol, one adapter today. ``connections.py`` got a creator's channel
authorized and holds a live access token for it; everything below is what
happens after that — take the clip out of the library, hand it to a platform,
and tell the browser how it went.

The shape is deliberate. `PublishTarget` is what the queue talks to, so the
Celery task, the publish_jobs row, the progress writes and the error mapping
are written ONCE, against a protocol, and TikTok and Instagram arrive as
another module implementing it rather than as another branch through the
worker. That is the promise PUBLISH.md makes ("behind a `PublishTarget`
protocol so TikTok and Instagram slot in later without touching the queue"),
and it is only worth anything if nothing in the queue ever says "youtube".

Two ideas carry the rest:

  * **A capability is what a connection may ACTUALLY do right now**, and it is
    a server-side fact, not a preference. YouTube forces every upload from an
    unverified app to `private` — so while the review is outstanding the
    capability says `private` and nothing anywhere offers a choice. The day it
    lands, the same capability opens up and the UI follows it, because the UI
    reads it rather than keeping its own copy.
  * **A failure is a sentence somebody reads.** `PublishError` carries text
    written for a creator, exactly as `PipelineError` does for the render
    pipeline, and the task writes it straight onto the row. A quota refusal
    that reads "HTTP 403" is a support ticket; "try tomorrow" is an answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from ..settings import Settings


# --------------------------------------------------------------------------- #
# The sentences both halves of a publish need. Written once and imported by
# both, because the API refuses an expired clip BEFORE queueing anything and
# the worker refuses one that expired in the seconds after — the same event
# twice, and a user who saw two different explanations for it would reasonably
# conclude that one of them was a bug.
# --------------------------------------------------------------------------- #

CLIP_EXPIRED = (
    "This clip's video has expired and been deleted, so there's nothing to "
    "post. Its title, score, hooks and transcript are still in your library."
)
CLIP_UNKNOWN = "That clip is no longer in your library."
CONNECTION_GONE = (
    "That channel was disconnected before this post started — connect it again "
    "from your account page and try once more."
)


def not_connected(label: str) -> str:
    """What to say when there is no channel to post to yet."""
    return (
        f"Connect your {label} channel on your account page before posting a "
        "clip to it."
    )


def unsupported(label: str) -> str:
    """What to say for a platform ClipCatalyst knows but cannot post to."""
    return (
        f"ClipCatalyst can't post to {label} yet — use the share button on a "
        "clip to post it from your phone."
    )


class PublishError(RuntimeError):
    """This post did not happen, and here is what to tell the user.

    Terminal by definition: the message goes onto the publish row as its
    user-facing error, so it is written to be READ — what went wrong and what,
    if anything, to do about it. Anything that is genuinely worth retrying
    automatically is retried inside the adapter (see the 5xx policy in
    youtube.py) and never reaches here.
    """


@dataclass(frozen=True)
class Capability:
    """What posting to one platform can currently do, on this deployment.

    ``privacy_choices`` is the whole list a user may pick from, in the order a
    UI should show it, and ``forced`` is set when the platform gives no choice
    at all — in which case ``privacy_choices`` holds that one value and nothing
    else, so a client that renders the list can never render an option that
    would be quietly overridden.

    ``note`` is the sentence the account page and the post sheet show. It lives
    here rather than in the frontend because whether it is true is a fact about
    the server (PUBLISH.md's rule that the UI never promises what the code
    cannot do).
    """

    privacy_choices: tuple[str, ...]
    forced: str
    note: str

    def resolve(self, requested: str) -> str:
        """The privacy this post will really have, given what was asked for.

        Forced wins, and it wins SILENTLY on purpose: the value it forces is
        always the more private one, and refusing the whole post because
        somebody asked for `public` on an unverified app would lose them the
        upload to protect a setting they can change on YouTube in one click.
        An unrecognised value falls back to the most private choice offered
        rather than to the platform's default, which is not ours to assume.
        """
        if self.forced:
            return self.forced
        if requested in self.privacy_choices:
            return requested
        return self.privacy_choices[-1] if self.privacy_choices else "private"


@dataclass(frozen=True)
class PublishRequest:
    """One clip, on its way to one channel. Everything the adapter is given.

    ``file`` is a LOCAL path: the worker stages the library clip (downloading it
    from S3 when that is where it lives) before an adapter is ever called, so no
    adapter has to know how this product stores video.

    ``media_type`` comes with it because a clip is not always an MP4 — a clip
    saved from the browser engine is whatever the device's MediaRecorder could
    write, which is often WebM — and an upload that declares the wrong type is
    refused by the platform rather than converted.
    """

    clip: Mapping[str, object]
    file: Path
    size_bytes: int
    title: str
    description: str
    privacy: str
    media_type: str = "video/mp4"


@dataclass(frozen=True)
class PublishResult:
    """Where the post ended up. Both fields are shown to the user."""

    video_id: str
    url: str


#: ``(fraction, detail)`` — called as bytes land, for the row the browser polls.
ProgressFn = Callable[[float, str], None]


class PublishTarget(Protocol):
    """One platform, as the queue sees it.

    Three methods, and the split between them is the point: `capability` and
    `metadata` are pure and are answered by the API process (the post sheet asks
    what a post will do BEFORE anything is queued), while `publish` is the only
    one that talks to a provider and the only one that runs on the worker.
    """

    #: The `connections.Provider.platform` this target posts to.
    platform: str

    def capability(self, settings: Settings) -> Capability:
        """What a post to this platform can do on this box, right now."""
        ...

    def metadata(self, request: PublishRequest) -> dict:
        """The provider's own metadata document for this post.

        Exposed on the protocol rather than buried in `publish` so it can be
        asserted on directly — the title truncation and the description
        fallback are rules with edge cases, and a rule you can only observe by
        watching a network call is a rule that goes untested.
        """
        ...

    def publish(
        self,
        settings: Settings,
        connection: Mapping[str, object],
        request: PublishRequest,
        on_progress: ProgressFn,
    ) -> PublishResult:
        """Upload the clip and return where it landed, or raise PublishError."""
        ...


def _targets() -> dict[str, PublishTarget]:
    """The registry, built lazily to keep the import graph one-directional.

    ``youtube`` imports ``connections``, which imports ``db``; importing the
    adapter at module scope would drag that whole chain into anything that
    merely wants the protocol.
    """
    from . import youtube

    return {youtube.TARGET.platform: youtube.TARGET}


def target_for(platform: str) -> PublishTarget | None:
    """The adapter that posts to `platform`, or None when nothing does yet.

    None is the ordinary answer for TikTok and Instagram: ``connections.py``
    knows them (so the account page can say why they are unavailable) and this
    module does not, which is exactly the state PUBLISH.md describes.
    """
    return _targets().get(platform.strip().lower())


def capability_for(settings: Settings, platform: str) -> Capability | None:
    """What posting to `platform` can currently do, or None if it cannot."""
    target = target_for(platform)
    return None if target is None else target.capability(settings)
