"""Local background artwork discovery and rendering."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


SUPPORTED_IMAGE_SUFFIXES = frozenset({".bmp", ".dds", ".jpeg", ".jpg", ".png", ".webp"})
BACKGROUND_MODES = frozenset({"ck3", "custom", "none"})


class _Chooser(Protocol):
    def choice(self, values: list[Path]) -> Path: ...


def _safe_image_file(path: Path) -> bool:
    return (
        path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
        and path.is_file()
        and not path.is_symlink()
    )


def discover_ck3_loading_screens(game_root: Path) -> list[Path]:
    """Find base-game and installed-DLC loading screens without following links."""
    root = Path(game_root)
    directories = [
        root / "game" / "gfx" / "interface" / "illustrations" / "loading_screens",
    ]
    dlc_root = root / "game" / "dlc"
    if dlc_root.is_dir():
        directories.extend(
            child / "gfx" / "interface" / "illustrations" / "loading_screens"
            for child in dlc_root.iterdir()
            if child.is_dir() and not child.is_symlink()
        )

    screens: list[Path] = []
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            continue
        screens.extend(path for path in directory.iterdir() if _safe_image_file(path))
    return sorted(set(screens), key=lambda path: str(path).casefold())


def choose_background_art(
    mode: str,
    game_root: Path | None,
    custom_path: Path | None = None,
    chooser: _Chooser | None = None,
) -> Path | None:
    """Resolve a local artwork choice for the requested background mode."""
    normalized = mode.casefold()
    if normalized not in BACKGROUND_MODES:
        normalized = "ck3"
    if normalized == "none":
        return None
    if normalized == "custom":
        candidate = Path(custom_path) if custom_path else None
        return candidate if candidate and _safe_image_file(candidate) else None
    if not game_root:
        return None
    screens = discover_ck3_loading_screens(game_root)
    if not screens:
        return None
    return (chooser or random.SystemRandom()).choice(screens)


def render_background_art(
    source: Path,
    size: tuple[int, int],
    *,
    dark: bool = True,
) -> Image.Image:
    """Crop and mute artwork so interface text and panels remain dominant."""
    width, height = size
    if width < 1 or height < 1:
        raise ValueError("Background size must be positive")
    with Image.open(source) as opened:
        artwork = ImageOps.exif_transpose(opened).convert("RGB")
    fitted = ImageOps.fit(artwork, (width, height), method=Image.Resampling.LANCZOS)
    fitted = ImageEnhance.Color(fitted).enhance(0.50)
    fitted = ImageEnhance.Contrast(fitted).enhance(0.82)
    fitted = fitted.filter(ImageFilter.GaussianBlur(radius=1.15))
    overlay = Image.new("RGB", fitted.size, "#0F1214" if dark else "#DED6BF")
    return Image.blend(fitted, overlay, 0.74 if dark else 0.78)


def render_fallback_texture(size: tuple[int, int], *, dark: bool = True) -> Image.Image:
    """Create the built-in neutral texture used when no local artwork is available."""
    width, height = size
    if width < 1 or height < 1:
        raise ValueError("Background size must be positive")
    base_color = "#0F1214" if dark else "#E7E1D3"
    texture = Image.effect_noise((max(1, width // 3), max(1, height // 3)), 5.0)
    texture = texture.resize((width, height), Image.Resampling.BILINEAR).convert("RGB")
    low = "#0B0D0E" if dark else "#D8D0BF"
    high = "#29241F" if dark else "#F2EDE2"
    texture = ImageOps.colorize(texture.convert("L"), low, high)
    base = Image.new("RGB", (width, height), base_color)
    return Image.blend(base, texture, 0.13)
