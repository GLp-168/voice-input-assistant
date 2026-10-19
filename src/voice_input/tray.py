"""Optional system tray integration."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading

from .resources import bundled_app_icon


LOGGER = logging.getLogger(__name__)


class TrayController:
    """Small pystray wrapper that degrades gracefully when unavailable."""

    def __init__(
        self,
        *,
        on_show: Callable[[], None],
        on_toggle: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.on_show = on_show
        self.on_toggle = on_toggle
        self.on_exit = on_exit
        self.icon = None
        self.thread: threading.Thread | None = None
        self.available = False
        self._running = False

    def start(self) -> bool:
        self.available = False
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            LOGGER.info("system tray unavailable; install pystray and pillow to enable it")
            return False

        try:
            with Image.open(bundled_app_icon()) as source:
                image = source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        except Exception:
            LOGGER.warning("unable to load bundled tray icon; using brand fallback", exc_info=True)
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            ImageDraw.Draw(image).ellipse((7, 7, 57, 57), fill=(15, 127, 114, 255))

        def menu_text_show(_item) -> str:
            return "显示主窗口"

        def menu_text_toggle(_item) -> str:
            return "停止识别" if self._running else "开始识别"

        self.icon = pystray.Icon(
            "voice-input",
            image,
            "语音转文字",
            menu=pystray.Menu(
                pystray.MenuItem(menu_text_show, lambda _icon, _item: self.on_show(), default=True),
                pystray.MenuItem(menu_text_toggle, lambda _icon, _item: self.on_toggle()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda _icon, _item: self.on_exit()),
            ),
        )
        self.thread = threading.Thread(target=self.icon.run, name="voice-input-tray", daemon=True)
        self.thread.start()
        self.available = True
        return True

    def set_running(self, running: bool) -> None:
        self._running = running
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                LOGGER.exception("failed to refresh tray menu")

    def stop(self) -> None:
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                LOGGER.exception("failed to stop tray icon")
            self.icon = None
        self.available = False
        self._running = False
