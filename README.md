# Filament Profile Tool

A simple desktop app for storing filament settings in one shared library and sending them to OrcaSlicer as user presets.

## What it does

You enter the settings for each filament (temperatures, flow ratio, volumetric speed, pressure advance, cooling, retraction) in a small form. The tool saves each filament as a JSON file in a library folder, which can be local or on a shared network drive. With one click, the profiles are written to OrcaSlicer, where they appear in the filament list like any preset you created yourself. Existing OrcaSlicer presets can also be imported into the library.

## Requirements

Python 3.8 or newer with Tkinter, and OrcaSlicer. No other packages are needed.

## Usage

```bash
python filament_tool.py                   # open the app
python filament_tool.py --sync            # send all profiles to OrcaSlicer
python filament_tool.py --sync --launch   # send all profiles, then open OrcaSlicer
```

OrcaSlicer loads presets at startup, so restart it after sending profiles if it's already open.

## Technical features

- **Single file, standard library only.** The whole app is one Python script built on `tkinter`, `json`, `pathlib`, `threading` and `subprocess`, so it runs anywhere Python does.
- **Native OrcaSlicer format.** Profiles are exported in the same JSON structure OrcaSlicer uses for its own user presets, including the `.info` sidecar file. The preset version is read from existing presets so exported files match the installed OrcaSlicer.
- **Preset inheritance.** Each profile builds on an OrcaSlicer base profile, and empty fields are left out of the export so they are inherited from it. Base profiles are validated against the installed system presets, because OrcaSlicer silently skips presets whose parent is missing.
- **Lossless import.** Settings not shown in the form, such as custom G-code, are kept in an `extra` field and written back unchanged on export.
- **Safe on network drives.** All files are written atomically (temporary file, then replace), so an interrupted save never leaves a broken profile.
- **Change history.** Every save records the time and user and keeps the last 20 versions of each profile in a `.history` folder.
- **Cloud sync aware.** Presets that OrcaSlicer has already synced to the cloud are marked as updated, so OrcaSlicer uploads the new version instead of restoring the old one.
- **Input normalization.** Decimal commas are converted to dots, colours are stored as `#RRGGBB`, and values are checked before saving.
- **Auto-detection.** The OrcaSlicer data folder, active user and executable are found automatically on Windows, macOS and Linux (including Flatpak). The system preset scan runs in a background thread to keep the UI responsive.

## How it works

The tool writes presets to OrcaSlicer's user folder:

| System  | Location |
|---------|----------|
| Windows | `%APPDATA%\OrcaSlicer\user\<user>\filament\` |
| macOS   | `~/Library/Application Support/OrcaSlicer/user/<user>/filament/` |
| Linux   | `~/.config/OrcaSlicer/user/<user>/filament/` |

`<user>` is `default` when you aren't signed in to OrcaSlicer. App settings are stored in `~/.filament_profile_tool.json`.

## License

<!-- Add a license, e.g. MIT -->
