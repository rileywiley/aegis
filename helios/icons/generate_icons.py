"""
Helios menu bar icon set generator.

Produces 12 template PNGs (6 states × 2 resolutions) per HELIOS.md §12 and §21:

  helios_not_running_template.png       (18×18, @1x)
  helios_not_running_template@2x.png    (36×36, @2x)
  helios_armed_template.png
  helios_armed_template@2x.png
  helios_recording_template.png
  helios_recording_template@2x.png
  helios_recording_voice_note_template.png
  helios_recording_voice_note_template@2x.png
  helios_paused_template.png
  helios_paused_template@2x.png
  helios_error_template.png
  helios_error_template@2x.png

Template PNGs use black pixels + variable alpha. macOS recolors them based on
menu bar appearance (dark → white, light → black) when the file is loaded with
NSImage's `template = true`. The `_template` suffix is conventional but the
true switch is the `template=True` flag passed to rumps.App in app.py.

Design language:
  - Base shape: circle, optical center 50%, 70% diameter of canvas
  - Stroke width: 12.5% of canvas (~2.25px @1x, 4.5px @2x), consistent across states
  - All glyphs share weight so swapping icons doesn't visually jump
  - Rendered at 8× supersample, downsampled with LANCZOS for clean AA
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Geometry constants (all proportions of canvas size)
# ---------------------------------------------------------------------------

SUPERSAMPLE = 8                  # render at 8× then downsample
RING_INSET = 0.15                # outer edge inset (15% padding around circle)
RING_STROKE = 0.115              # ring stroke width
DOT_RADIUS = 0.085               # "armed" center dot radius
RECORD_RADIUS = 0.34             # filled recording disc radius
PAUSE_BAR_W = 0.085              # pause bar half-width
PAUSE_BAR_H = 0.21               # pause bar half-height
PAUSE_BAR_OFFSET = 0.115         # x-offset of each pause bar from center
EXCL_BAR_W = 0.07                # error "!" bar half-width
EXCL_BAR_TOP = 0.225             # top of "!" stem (above center)
EXCL_BAR_BOT = 0.045             # bottom of "!" stem (above center)
EXCL_DOT_Y = -0.155              # y-position of "!" dot (negative = below center)
EXCL_DOT_R = 0.075               # "!" dot radius
# Voice-note microphone: filled disc with a vertical capsule cut out.
# Stem + foot are dropped — at 18px they're sub-pixel noise and just blur the
# capsule silhouette. The capsule alone reads cleanly as "microphone".
MIC_CAP_W = 0.105                # microphone capsule half-width
MIC_CAP_H = 0.18                 # microphone capsule half-height (vertical extent)

BLACK = (0, 0, 0, 255)
TRANSPARENT = (0, 0, 0, 0)

OUTPUT_DIR = Path("/home/claude/helios_icons")

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _new_canvas(target_size: int) -> Image.Image:
    """Transparent supersampled canvas."""
    s = target_size * SUPERSAMPLE
    return Image.new("RGBA", (s, s), TRANSPARENT)


def _downsample(img: Image.Image, target_size: int) -> Image.Image:
    """Downsample with Lanczos for crisp anti-aliased edges."""
    return img.resize((target_size, target_size), Image.LANCZOS)


def _center(s: int) -> tuple[float, float]:
    return s / 2, s / 2


def _ring(target_size: int) -> Image.Image:
    """Outline circle (the base shape under most states).

    Built by composing two filled ellipses: outer black, inner transparent.
    Produces a clean stroked ring with proper anti-aliasing.
    """
    s = target_size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), TRANSPARENT)
    d = ImageDraw.Draw(img)

    inset = s * RING_INSET
    stroke = s * RING_STROKE
    d.ellipse((inset, inset, s - inset, s - inset), fill=BLACK)
    d.ellipse(
        (inset + stroke, inset + stroke, s - inset - stroke, s - inset - stroke),
        fill=TRANSPARENT,
    )
    return img


def _filled_disc(target_size: int, radius_ratio: float) -> Image.Image:
    """Solid filled circle, used for recording state and components."""
    s = target_size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), TRANSPARENT)
    d = ImageDraw.Draw(img)
    cx, cy = _center(s)
    r = s * radius_ratio
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLACK)
    return img


def _composite(*layers: Image.Image) -> Image.Image:
    """Alpha-composite layers in order (first = bottom)."""
    base = layers[0]
    for layer in layers[1:]:
        base = Image.alpha_composite(base, layer)
    return base


# ---------------------------------------------------------------------------
# State renderers (each returns a supersampled RGBA image)
# ---------------------------------------------------------------------------


def render_not_running(target_size: int) -> Image.Image:
    """Plain outline ring — daemon stopped."""
    return _ring(target_size)


def render_armed(target_size: int) -> Image.Image:
    """Ring with center dot — daemon running, idle, ready."""
    s = target_size * SUPERSAMPLE
    base = _ring(target_size)
    dot = Image.new("RGBA", (s, s), TRANSPARENT)
    d = ImageDraw.Draw(dot)
    cx, cy = _center(s)
    r = s * DOT_RADIUS
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLACK)
    return _composite(base, dot)


def render_recording(target_size: int) -> Image.Image:
    """Solid filled disc — universal record glyph.

    No surrounding ring: the filled shape itself reads as a record button.
    Slightly larger radius than the inner area of the ring to feel weighted
    relative to the other states.
    """
    return _filled_disc(target_size, RECORD_RADIUS)


def render_recording_voice_note(target_size: int) -> Image.Image:
    """Filled disc with a vertical capsule cut out via negative space.

    The capsule shape — taller than wide, fully rounded ends — reads
    immediately as a microphone capsule. Stem and foot are intentionally
    omitted; they don't survive at 18px and just muddle the silhouette.
    """
    from PIL import ImageChops

    s = target_size * SUPERSAMPLE
    cx, cy = _center(s)

    # Layer 1: filled disc
    disc = _filled_disc(target_size, RECORD_RADIUS)

    # Layer 2: cut-out capsule (transparent shape punched through disc)
    cut = Image.new("RGBA", (s, s), TRANSPARENT)
    cd = ImageDraw.Draw(cut)

    cap_w = s * MIC_CAP_W
    cap_h = s * MIC_CAP_H
    # Fully-rounded capsule (radius = half the shorter dimension)
    cd.rounded_rectangle(
        (cx - cap_w, cy - cap_h, cx + cap_w, cy + cap_h),
        radius=cap_w,
        fill=(255, 255, 255, 255),
    )

    # Subtract `cut` from `disc` using alpha-multiply with inverted cut
    disc_alpha = disc.split()[3]
    cut_alpha = cut.split()[3]
    inverted_cut = ImageChops.invert(cut_alpha)
    new_alpha = ImageChops.multiply(disc_alpha, inverted_cut)

    result = Image.new("RGBA", (s, s), TRANSPARENT)
    black_layer = Image.new("RGBA", (s, s), (0, 0, 0, 255))
    result.paste(black_layer, (0, 0), mask=new_alpha)
    return result


def render_paused(target_size: int) -> Image.Image:
    """Ring with two vertical bars — universal pause glyph."""
    s = target_size * SUPERSAMPLE
    base = _ring(target_size)

    bars = Image.new("RGBA", (s, s), TRANSPARENT)
    d = ImageDraw.Draw(bars)
    cx, cy = _center(s)
    bar_w = s * PAUSE_BAR_W
    bar_h = s * PAUSE_BAR_H
    offset = s * PAUSE_BAR_OFFSET

    # Left bar
    left_x = cx - offset - bar_w
    d.rounded_rectangle(
        (left_x, cy - bar_h, left_x + bar_w * 2, cy + bar_h),
        radius=bar_w * 0.35,
        fill=BLACK,
    )
    # Right bar
    right_x = cx + offset - bar_w
    d.rounded_rectangle(
        (right_x, cy - bar_h, right_x + bar_w * 2, cy + bar_h),
        radius=bar_w * 0.35,
        fill=BLACK,
    )

    return _composite(base, bars)


def render_error(target_size: int) -> Image.Image:
    """Ring with exclamation mark — error/permission revoked."""
    s = target_size * SUPERSAMPLE
    base = _ring(target_size)

    excl = Image.new("RGBA", (s, s), TRANSPARENT)
    d = ImageDraw.Draw(excl)
    cx, cy = _center(s)

    # Stem of exclamation (rounded bar above center)
    bar_w = s * EXCL_BAR_W
    top = cy - s * EXCL_BAR_TOP
    bot = cy - s * EXCL_BAR_BOT  # negative offset, above center
    d.rounded_rectangle(
        (cx - bar_w, top, cx + bar_w, bot),
        radius=bar_w * 0.6,
        fill=BLACK,
    )

    # Dot of exclamation (below the stem, near bottom)
    dot_y = cy - s * EXCL_DOT_Y  # negative offset → below center
    dot_r = s * EXCL_DOT_R
    d.ellipse(
        (cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r),
        fill=BLACK,
    )

    return _composite(base, excl)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

STATES: dict[str, callable] = {
    "not_running": render_not_running,
    "armed": render_armed,
    "recording": render_recording,
    "recording_voice_note": render_recording_voice_note,
    "paused": render_paused,
    "error": render_error,
}


def generate_all(output_dir: Path) -> list[Path]:
    """Render all 12 PNGs (6 states × @1x, @2x). Returns list of paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for state, renderer in STATES.items():
        for suffix, size in (("", 18), ("@2x", 36)):
            supersampled = renderer(size)
            final = _downsample(supersampled, size)
            filename = f"helios_{state}_template{suffix}.png"
            path = output_dir / filename
            final.save(path, "PNG", optimize=True)
            written.append(path)

    return written


if __name__ == "__main__":
    paths = generate_all(OUTPUT_DIR)
    print(f"Generated {len(paths)} icons in {OUTPUT_DIR}:")
    for p in paths:
        size_kb = p.stat().st_size / 1024
        print(f"  {p.name}  ({size_kb:.1f} KB)")
