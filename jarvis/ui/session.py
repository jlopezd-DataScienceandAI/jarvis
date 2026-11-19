"""Brings the HUD up next to the assistant and takes it down again."""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING

from jarvis.brain.tools.memory import get_store
from jarvis.config import Config

from .hub import Hub
from .media import NowPlayingWatcher
from .monitor import SystemMonitor
from .server import UiServer
from .window import open_window

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class HudSession:
    """The web server, the side-panel feed and the window, as one unit.

    It is created before the assistant loads its models, so the window appears at
    once and shows the start-up progress; ``attach`` wires the controls when ready.
    """

    def __init__(self, config: Config, hub: Hub) -> None:
        self.server = UiServer(hub)
        self.server.start()
        self.monitor = SystemMonitor(hub, get_store(config.memory_dir))
        self.monitor.start()
        self.music = NowPlayingWatcher(hub)
        self.music.start()
        self.window = open_window(self.server.url, config.data_dir / "hud-profile", config.ui.width, config.ui.height)
        self._closing = False

    def attach(self, app: JarvisApp) -> None:
        self.server.on_activate = lambda: app.activate("orbe")
        self.server.on_say = app.submit_text
        if self.window is not None:
            threading.Thread(target=self._quit_when_closed, args=(app,), name="hud-window", daemon=True).start()

    def _quit_when_closed(self, app: JarvisApp) -> None:
        """Closing the HUD window is how the user quits Jarvis."""
        self.window.wait()
        if not self._closing:
            app.request_quit()

    def close(self) -> None:
        self._closing = True
        self.monitor.stop()
        self.music.stop()
        self.server.stop()
        if self.window is not None and self.window.poll() is None:
            self.window.terminate()
            try:
                self.window.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.window.kill()
