"""Tests for local background artwork handling."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from background_art import (
    choose_background_art,
    discover_ck3_loading_screens,
    render_background_art,
    render_fallback_texture,
)


class _FirstChooser:
    def choice(self, values: list[Path]) -> Path:
        return values[0]


class BackgroundArtTests(unittest.TestCase):
    def test_discovers_base_and_dlc_loading_screens_only(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            base = root / "game/gfx/interface/illustrations/loading_screens"
            dlc = root / "game/dlc/example/gfx/interface/illustrations/loading_screens"
            unrelated = root / "game/gfx/interface/illustrations/events"
            base.mkdir(parents=True)
            dlc.mkdir(parents=True)
            unrelated.mkdir(parents=True)
            (base / "castle.dds").write_bytes(b"dds")
            (dlc / "court.png").write_bytes(b"png")
            (base / "notes.txt").write_text("ignore", encoding="utf-8")
            (unrelated / "event.dds").write_bytes(b"dds")

            found = discover_ck3_loading_screens(root)

            self.assertEqual(set(found), {base / "castle.dds", dlc / "court.png"})

    def test_resolves_modes_without_copying_artwork(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            root = Path(directory)
            base = root / "game/gfx/interface/illustrations/loading_screens"
            base.mkdir(parents=True)
            screen = base / "castle.dds"
            screen.write_bytes(b"dds")
            custom = root / "custom.jpg"
            custom.write_bytes(b"jpg")

            self.assertEqual(
                choose_background_art("ck3", root, chooser=_FirstChooser()),
                screen,
            )
            self.assertEqual(choose_background_art("custom", root, custom), custom)
            self.assertIsNone(choose_background_art("none", root, custom))
            self.assertIsNone(choose_background_art("custom", root, root / "missing.png"))

    def test_renders_artwork_and_fallback_at_requested_size(self) -> None:
        with TemporaryDirectory(dir=Path(__file__).parent) as directory:
            source = Path(directory) / "art.png"
            Image.new("RGB", (320, 180), "#A06040").save(source)

            artwork = render_background_art(source, (160, 100))
            fallback = render_fallback_texture((160, 100))

            self.assertEqual(artwork.size, (160, 100))
            self.assertEqual(fallback.size, (160, 100))
            self.assertNotEqual(artwork.getpixel((0, 0)), (160, 96, 64))


if __name__ == "__main__":
    unittest.main()
