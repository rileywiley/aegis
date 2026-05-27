# Helios menu bar icons

12 PNGs covering the 6 menu bar states defined in HELIOS.md §12, at @1x (18×18) and @2x (36×36).

## Files

```
helios_not_running_template.png
helios_not_running_template@2x.png
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
```

Drop these in `helios/icons/` and reference them per HELIOS.md §21 (`setup.py`'s `DATA_FILES`).

## State → glyph

| State                   | Glyph                                  | Trigger                                   |
| ----------------------- | -------------------------------------- | ----------------------------------------- |
| `not_running`           | Open ring                              | Daemon stopped                            |
| `armed`                 | Ring + center dot                      | Daemon running, no active session         |
| `recording`             | Solid filled disc                      | Active calendar/continuous capture        |
| `recording_voice_note`  | Solid disc with vertical capsule hole  | Active voice note (overrides `recording`) |
| `paused`                | Ring + two vertical bars               | Capture paused                            |
| `error`                 | Ring + exclamation mark                | Permission revoked or other fault         |

## Template PNG behavior

These are macOS template images — black pixels with variable alpha. The OS recolors them automatically based on menu bar appearance (white on dark, black on light). The `_template` suffix is a naming convention; the actual switch is the `template=True` flag passed to `rumps.App`:

```python
super().__init__(
    "Helios",
    icon=str(ICONS_DIR / "helios_not_running_template.png"),
    template=True,   # ← this is what makes macOS treat it as a template
    quit_button=None,
)
```

## Design language

- **Base shape**: circle, 70% diameter of canvas, optical-center aligned. All six states share the same outer circle so swapping icons during state transitions doesn't visually jump.
- **Stroke weight**: ~12% of canvas width (~2.2px @1x, ~4.4px @2x). Consistent across `not_running`, `armed`, `paused`, `error`.
- **Fill states** (`recording`, `recording_voice_note`) use a slightly smaller solid disc so visual weight stays comparable to the ringed states.
- **Voice note differentiator**: vertical pill cut out of the recording disc. Stem and foot were intentionally dropped — at 18px they reduce to sub-pixel noise and blur the silhouette. The capsule alone reads as "microphone."
- Rendered at 8× supersample, downsampled with Lanczos for clean anti-aliasing.

## Regenerating

The whole set rebuilds in under a second:

```bash
python generate_icons.py
```

Tweak the geometry constants at the top of the script (`RING_INSET`, `RING_STROKE`, `RECORD_RADIUS`, etc.) to adjust proportions. All values are fractions of the canvas, so changes affect both @1x and @2x consistently.

## What this does NOT include

- `Helios.icns` (the app bundle icon, referenced as `iconfile` in `setup.py`). That's a different artifact — multi-resolution shield logo per Track 0G — and worth a separate design pass.
- Real paid-design polish. These are clean and functional, but a designer with a tablet would do better. Per HELIOS.md Appendix D, real menu bar icons are a deferrable design task.
