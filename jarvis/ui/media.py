"""What is playing right now, for the HUD's music panel.

Read from Windows' media session (the same source as the volume flyout), so it
covers Spotify and any other player, and costs nothing to poll. The panel is
only shown while something is actually playing.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import threading
from typing import Any

from .hub import Hub

log = logging.getLogger(__name__)

_PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
_COVER_PX = 128

_APP_NAMES = {
    "spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "vlc": "VLC",
    "zunemusic": "Reproductor multimedia", "applemusic": "Apple Music", "itunes": "iTunes",
}


def app_label(app_id: str) -> str:
    """'SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify' -> 'Spotify'."""
    lowered = app_id.lower()
    for key, name in _APP_NAMES.items():
        if key in lowered:
            return name
    base = app_id.split("!")[0]
    if base.lower().endswith(".exe"):  # a classic program identifies itself by its executable
        return base[:-4].replace("/", "\\").rsplit("\\", 1)[-1] or "Reproductor"
    return base.split("_")[0].split(".")[-1] or "Reproductor"  # a Store app: Publisher.Name_hash


def cover_data_url(image_bytes: bytes) -> str:
    """A small JPEG data URL of the cover art. Empty string if the image cannot be read."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        image.thumbnail((_COVER_PX, _COVER_PX), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=82)
    except Exception:  # noqa: BLE001 - artwork is decoration; never worth an error
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


async def read_playing(with_cover_for: str | None = None) -> dict[str, Any] | None:
    """The track now playing, or None. The cover is only fetched when the track is not
    ``with_cover_for`` (its key), since it does not change during a song."""
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

    manager = await Manager.request_async()
    sessions = list(manager.get_sessions())
    current = manager.get_current_session()
    if current is not None:  # the session Windows considers active goes first
        sessions.sort(key=lambda s: s.source_app_user_model_id != current.source_app_user_model_id)
    for session in sessions:
        if int(session.get_playback_info().playback_status) != _PLAYING:
            continue
        props = await session.try_get_media_properties_async()
        title = (props.title or "").strip()
        if not title:
            continue
        track = {
            "title": title,
            "artist": (props.artist or "").strip(),
            "app": app_label(session.source_app_user_model_id),
        }
        track["key"] = f"{track['app']}|{track['artist']}|{track['title']}"
        if track["key"] != with_cover_for:
            track["cover"] = await _read_cover(props.thumbnail)
        return track
    return None


async def _read_cover(thumbnail: Any) -> str:
    if thumbnail is None:
        return ""
    try:
        from winrt.windows.storage.streams import Buffer, InputStreamOptions

        stream = await thumbnail.open_read_async()
        size = stream.size
        if not 0 < size <= 4_000_000:
            return ""
        buffer = Buffer(size)
        await stream.read_async(buffer, size, InputStreamOptions.READ_AHEAD)
        return cover_data_url(bytes(memoryview(buffer)[: buffer.length]))
    except Exception:  # noqa: BLE001 - some players expose no readable artwork
        log.debug("Could not read cover art", exc_info=True)
        return ""


class NowPlayingWatcher:
    """Publishes ``music`` to the hub: the playing track, or None when nothing plays."""

    def __init__(self, hub: Hub, interval_s: float = 2.0) -> None:
        self._hub = hub
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._last: dict[str, Any] | None = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name="ui-music", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def apply(self, track: dict[str, Any] | None) -> None:
        """Publish a reading if it differs from the last one. A reading for the same track
        arrives without its cover and keeps the one already known."""
        if track is not None and self._last is not None and track["key"] == self._last["key"]:
            return
        if track is None and self._last is None:
            return
        self._last = track
        self._hub.update(music=track)

    def _loop(self) -> None:
        try:
            import winrt.windows.media.control  # noqa: F401 - checked once, up front
        except ImportError:
            log.warning("winrt media packages missing; the HUD will not show what is playing")
            return
        while not self._stop.is_set():
            try:
                known = self._last["key"] if self._last else None
                self.apply(asyncio.run(read_playing(with_cover_for=known)))
            except Exception:  # noqa: BLE001 - a misbehaving player must not kill the panel
                log.debug("Now-playing read failed", exc_info=True)
            self._stop.wait(self._interval_s)
