from __future__ import annotations

from types import ModuleType
import sys

from voice_input.tray import TrayController


class FakeMenuItem:
    def __init__(self, text, action, default=False):
        self._text = text
        self.action = action
        self.default = default

    @property
    def text(self):
        return self._text(self) if callable(self._text) else self._text


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.updated = 0
        self.stopped = 0

    def run(self):
        return None

    def update_menu(self):
        self.updated += 1

    def stop(self):
        self.stopped += 1


class FakePystray:
    def __init__(self):
        self.MenuItem = FakeMenuItem
        self.Menu = FakeMenu
        self.icon = None

    def Icon(self, name, image, title, menu):
        self.icon = FakeIcon(name, image, title, menu)
        return self.icon


class FakeImage:
    opened = 0

    class Resampling:
        LANCZOS = object()

    class Opened:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def convert(self, _mode):
            return self

        def resize(self, _size, _resampling):
            return self

    @classmethod
    def open(cls, _path):
        cls.opened += 1
        return cls.Opened()

    @staticmethod
    def new(*_args, **_kwargs):
        return object()


class FakeImageDraw:
    class Drawer:
        def ellipse(self, *_args, **_kwargs):
            return None

    @staticmethod
    def Draw(_image):
        return FakeImageDraw.Drawer()


def test_tray_menu_reflects_running_state(monkeypatch):
    fake_pystray = ModuleType("pystray")
    fake_pystray.__dict__.update({
        "MenuItem": FakeMenuItem,
        "Menu": FakeMenu,
        "Icon": FakePystray().Icon,
    })
    fake_pil = ModuleType("PIL")
    fake_pil.__path__ = []  # type: ignore[attr-defined]
    fake_image = ModuleType("PIL.Image")
    fake_image.new = FakeImage.new  # type: ignore[attr-defined]
    fake_image.open = FakeImage.open  # type: ignore[attr-defined]
    fake_image.Resampling = FakeImage.Resampling  # type: ignore[attr-defined]
    fake_draw = ModuleType("PIL.ImageDraw")
    fake_draw.Draw = FakeImageDraw.Draw  # type: ignore[attr-defined]
    fake_pil.Image = fake_image  # type: ignore[attr-defined]
    fake_pil.ImageDraw = fake_draw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image)
    monkeypatch.setitem(sys.modules, "PIL.ImageDraw", fake_draw)

    calls = {"show": 0, "toggle": 0, "exit": 0}

    tray = TrayController(
        on_show=lambda: calls.__setitem__("show", calls["show"] + 1),
        on_toggle=lambda: calls.__setitem__("toggle", calls["toggle"] + 1),
        on_exit=lambda: calls.__setitem__("exit", calls["exit"] + 1),
    )

    assert tray.start() is True
    assert FakeImage.opened == 1
    assert tray.available is True
    assert tray.icon.menu.items[0].text == "显示主窗口"
    assert tray.icon.menu.items[1].text == "开始识别"

    tray.set_running(True)
    assert tray.icon.updated == 1
    assert tray.icon.menu.items[1].text == "停止识别"

    tray.stop()
    assert tray.icon is None
    assert tray.available is False
