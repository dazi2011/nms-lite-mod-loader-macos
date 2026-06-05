# NMS Lite Mod Loader for macOS

A lightweight Steam-chain mod loader for **No Man's Sky on macOS**.

The loader is designed for one simple workflow:

1. Steam launches `No Man's Sky.app` as usual.
2. A small shim runs this loader first.
3. The loader scans the sibling `MODS/` folder.
4. Matching mod files are temporarily applied into the affected game PAKs.
5. The original game executable is launched with Steam's arguments and environment preserved.
6. When the game exits, the original PAKs are restored.

This avoids permanent PAK edits while still making EXML/MBIN style mods usable on macOS.

## Features

- Steam-friendly app shim: Steam still launches the same `.app` and executable name.
- Sibling `MODS/` folder: place the loader beside `No Man's Sky.app` and put mods in `MODS/MyMod/`.
- Temporary patching: affected PAKs are backed up before launch and restored after the game exits.
- Crash recovery: if a previous session did not restore cleanly, the next launch restores first.
- EXML/MXML support through MBINCompiler.
- Portable .NET fallback for Apple Silicon systems where system `dotnet` cannot start CoreCLR.
- Incremental PAK file-tree cache with optional SHA256 signatures.
- Mod priority control for overlapping mods.
- Repair command for Steam/game updates that overwrite the shim.
- Update command for syncing newer loader scripts into an installed loader.

## Requirements

- macOS
- Steam version of No Man's Sky
- Python 3.11 or newer
- Network access on first install for Python dependencies
- Rosetta 2 if the installer needs the portable x64 .NET fallback on Apple Silicon

The installer creates its own local Python venv in the game directory and installs `hgpaktool` there.

## Recommended Layout

After installation:

```text
No Man's Sky/
├── No Man's Sky.app/
├── MODS/
│   └── MyMod/
│       └── METADATA/...
├── NMSModLoader/
│   ├── nms_lite_loader.py
│   ├── nms_loader_mbin.py
│   ├── requirements.txt
│   ├── .venv/
│   └── tools/
└── _NMSModLoader/
    ├── cache/
    ├── logs/
    └── backups/
```

`MODS/MyMod/` should mirror the internal path used inside the game PAKs. Example:

```text
MODS/MyLanguagePatch/
└── LANGUAGE/
    └── NMS_LOC1_ENGLISH.MBIN
```

For global EXML snippets, this shorthand is supported:

```text
MODS/MyGlobalsPatch/
└── GLOBALS/
    └── GCBUILDABLESHIPGLOBALS.GLOBAL.EXML
```

## Install

Preview first:

```bash
python3 setup_nms_loader.py dry-run
```

Install into the standard macOS Steam library:

```bash
python3 setup_nms_loader.py install
```

Install into a custom Steam library:

```bash
python3 setup_nms_loader.py install --game-dir "/path/to/steamapps/common/No Man's Sky"
```

The installer will ask for confirmation unless `--yes` is passed.

What installation changes:

- Creates `NMSModLoader/`.
- Creates `MODS/`.
- Creates a local Python venv and installs `hgpaktool`.
- Installs MBINCompiler files according to the selected mode.
- Moves the original executable to `No Man's Sky.nms-loader-original`.
- Writes a shim at `No Man's Sky.app/Contents/MacOS/No Man's Sky`.
- Builds the PAK file-tree cache in `_NMSModLoader/cache/pak_index.json`.

Skip first-run index prewarm:

```bash
python3 setup_nms_loader.py install --skip-index
```

Fail the install if MBINCompiler cannot run:

```bash
python3 setup_nms_loader.py install --strict-deps
```

## MBINCompiler Modes

```bash
python3 setup_nms_loader.py install --mbin auto
python3 setup_nms_loader.py install --mbin bundled
python3 setup_nms_loader.py install --mbin release
python3 setup_nms_loader.py install --mbin source
python3 setup_nms_loader.py install --mbin skip
```

- `auto`: default; tries bundled files first, then GitHub release assets.
- `bundled`: uses the minimal files in `vendor/mbincompiler/`.
- `release`: downloads latest release assets from `monkeyman192/MBINCompiler`.
- `source`: clones and builds MBINCompiler from source. This requires a working .NET SDK and is the riskiest path.
- `skip`: does not install MBINCompiler. Direct MBIN replacement still works; EXML/MXML conversion is skipped.

On some Apple Silicon systems, system `dotnet` can list runtimes but fail with:

```text
Failed to create CoreCLR, HRESULT: 0x8007000C
```

When this happens, the installer can install an isolated .NET 8 x64 runtime under:

```text
NMSModLoader/tools/dotnet-osx-x64/
```

The wrapper then runs MBINCompiler through Rosetta without modifying system dotnet or Homebrew dotnet.

## PAK Index Cache

The loader does **not** fully unpack every PAK during indexing. It uses `hgpaktool -L` to list each PAK's file tree.

The cache stores:

- PAK file name
- size
- mtime
- optional SHA256
- file tree

On launch:

- unchanged PAKs reuse cached file trees;
- changed PAKs are hash-checked when prior hashes exist;
- only added, removed, or content-changed PAKs are re-indexed.

Manual refresh:

```bash
python3 setup_nms_loader.py index
python3 setup_nms_loader.py index --force-index
python3 setup_nms_loader.py index --index-no-hashes
```

Direct loader command:

```bash
python3 nms_lite_loader.py index --game-app "/path/to/No Man's Sky.app" --hashes
```

## Usage

After installation, launch the game from Steam normally.

The loader will show a small startup dialog or log window, scan mods, apply matched files, start the game, and restore original PAKs after exit.

Preview mod matching without launching:

```bash
python3 nms_lite_loader.py scan --game-app "/path/to/No Man's Sky.app"
```

Restore a stuck active session manually:

```bash
python3 nms_lite_loader.py restore --game-app "/path/to/No Man's Sky.app"
```

## Mod Priority

Default order is directory-name order. Later mods override earlier mods when they target the same internal file.

List current order:

```bash
python3 nms_lite_loader.py priority list --game-app "/path/to/No Man's Sky.app"
```

Set order:

```bash
python3 nms_lite_loader.py priority set --game-app "/path/to/No Man's Sky.app" BaseMod PatchMod FinalOverride
```

Reset to directory-name order:

```bash
python3 nms_lite_loader.py priority reset --game-app "/path/to/No Man's Sky.app"
```

## Repair After Game Updates

Steam updates can overwrite the shim. If the game launches without the loader after an update, run:

```bash
python3 setup_nms_loader.py repair --game-dir "/path/to/steamapps/common/No Man's Sky"
```

`repair` rotates the newly updated official executable into `.nms-loader-original` and writes a fresh shim.

## Update Installed Loader Scripts

After pulling or editing this project, sync the installed loader:

```bash
python3 setup_nms_loader.py update --game-dir "/path/to/steamapps/common/No Man's Sky"
```

This copies current project scripts into `NMSModLoader/`, refreshes dependencies, rewrites the shim, and refreshes the PAK index cache.

## Uninstall

```bash
python3 setup_nms_loader.py uninstall --game-dir "/path/to/steamapps/common/No Man's Sky"
```

Remove the copied loader directory too:

```bash
python3 setup_nms_loader.py uninstall --game-dir "/path/to/steamapps/common/No Man's Sky" --remove-loader-dir
```

## Diagnostics

```bash
python3 setup_nms_loader.py doctor --game-dir "/path/to/steamapps/common/No Man's Sky"
python3 nms_lite_loader.py doctor --game-app "/path/to/No Man's Sky.app"
```

Logs:

- setup/update logs: `NMSModLoader/setup.log`
- runtime logs: `_NMSModLoader/logs/loader-*.log`
- scan/index/doctor logs: `_NMSModLoader/logs/`

## Safety Notes

- This tool modifies game PAKs only while the game is running.
- Original PAKs are restored after the game exits.
- A crash-safe active-session file is used for recovery.
- The real game process keeps Steam's launch environment; helper tools scrub Steam/DYLD injection variables to avoid breaking MBINCompiler.
- Keep backups of important saves. This tool is cautious, not magic.

## Not Affiliated

This project is not affiliated with Hello Games, No Man's Sky, Steam, Valve, MBINCompiler, or hgpaktool.
