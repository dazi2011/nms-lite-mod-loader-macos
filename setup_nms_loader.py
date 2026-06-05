#!/usr/bin/env python3
"""
One-time installer for the lightweight No Man's Sky macOS mod loader.

The installer is intentionally explicit:
- `dry-run` shows exactly what would change.
- `install` prompts unless `--yes` is provided.
- `uninstall` restores the original game executable from the backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import venv
from pathlib import Path
from typing import Optional

import nms_loader_mbin


DEFAULT_GAME_DIRS = (
    Path.home() / "Library" / "Application Support" / "Steam" / "steamapps" / "common" / "No Man's Sky",
    Path.home() / "Library" / "Application Support" / "Steam" / "SteamApps" / "common" / "No Man's Sky",
)
APP_NAME = "No Man's Sky.app"
EXECUTABLE_NAME = "No Man's Sky"
REAL_SUFFIX = ".nms-loader-original"
LOADER_DIR_NAME = "NMSModLoader"
MODS_DIR_NAME = "MODS"
SHIM_MARKER = "NMS_LITE_LOADER_SHIM"
INSTALL_STATE = "install_state.json"


class SetupError(RuntimeError):
    pass


class Plan:
    def __init__(self, dry_run: bool, log_path: Optional[Path] = None):
        self.dry_run = dry_run
        self.log_path = log_path

    def say(self, message: str) -> None:
        print(message)
        if self.log_path and not self.dry_run:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")

    def run(self, message: str, fn, *args, **kwargs):
        prefix = "[dry-run]" if self.dry_run else "[run]"
        self.say(f"{prefix} {message}")
        if not self.dry_run:
            return fn(*args, **kwargs)
        return None


def resolve_game_dir(raw: Optional[str]) -> Path:
    if raw:
        game_dir = Path(raw).expanduser().resolve()
    else:
        game_dir = next((p for p in DEFAULT_GAME_DIRS if p.exists()), DEFAULT_GAME_DIRS[0])
    app = game_dir / APP_NAME
    if not app.exists():
        raise SetupError(f"Game app not found: {app}")
    exe = app / "Contents" / "MacOS" / EXECUTABLE_NAME
    if not exe.exists():
        raise SetupError(f"Game executable not found: {exe}")
    return game_dir


def paths_for(game_dir: Path):
    app = game_dir / APP_NAME
    macos_dir = app / "Contents" / "MacOS"
    exe = macos_dir / EXECUTABLE_NAME
    real = macos_dir / (EXECUTABLE_NAME + REAL_SUFFIX)
    loader_dir = game_dir / LOADER_DIR_NAME
    return app, macos_dir, exe, real, loader_dir


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_state_path(loader_dir: Path) -> Path:
    return loader_dir / INSTALL_STATE


def write_install_state(game_dir: Path, app: Path, exe: Path, real: Path, loader_dir: Path) -> None:
    data = {
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game_dir": str(game_dir),
        "app": str(app),
        "shim": str(exe),
        "original_executable": str(real),
        "original_sha256": sha256_file(real) if real.exists() else None,
        "original_size": real.stat().st_size if real.exists() else None,
    }
    install_state_path(loader_dir).write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_shim(path: Path) -> bool:
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return SHIM_MARKER in data


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o755)


def create_venv_and_install(loader_dir: Path, log=print) -> None:
    env_dir = loader_dir / ".venv"
    if not env_dir.exists():
        log(f"Creating Python venv: {env_dir}")
        venv.EnvBuilder(with_pip=True).create(env_dir)
    py = env_dir / "bin" / "python3"
    if not py.exists():
        py = env_dir / "bin" / "python"
    req = loader_dir / "requirements.txt"
    for cmd in (
        [str(py), "--version"],
        [str(py), "-m", "pip", "install", "--upgrade", "pip"],
        [str(py), "-m", "pip", "install", "-r", str(req)],
    ):
        log("Running: " + " ".join(cmd))
        subprocess.check_call(cmd)


def venv_python(loader_dir: Path) -> Path:
    py = loader_dir / ".venv" / "bin" / "python3"
    if not py.exists():
        py = loader_dir / ".venv" / "bin" / "python"
    return py


def ensure_loader_files(project_dir: Path, loader_dir: Path, plan: Plan) -> None:
    plan.run("Create loader and MODS directories", lambda: ((loader_dir / "tools").mkdir(parents=True, exist_ok=True), (loader_dir.parent / MODS_DIR_NAME).mkdir(exist_ok=True)))
    for filename in ("nms_lite_loader.py", "nms_loader_mbin.py", "requirements.txt", "README.md"):
        plan.run(f"Copy {filename}", copy_file, project_dir / filename, loader_dir / filename)


def ensure_dependencies(project_dir: Path, loader_dir: Path, args: argparse.Namespace, plan: Plan, dry_run: bool) -> None:
    if dry_run:
        plan.say(f"[dry-run] Create/update Python venv and install {loader_dir / 'requirements.txt'}")
        plan.say(f"[dry-run] MBINCompiler mode: {args.mbin}")
        return
    plan.say("[run] Create/update Python venv and install Python dependencies")
    create_venv_and_install(loader_dir, plan.say)
    plan.say(f"[run] MBINCompiler mode: {args.mbin}")
    nms_loader_mbin.ensure_mbincompiler(Path(__file__).resolve().parent, loader_dir / "tools", args.mbin, plan.say)
    if not getattr(args, "no_mbin_self_test", False) and args.mbin != "skip":
        result = nms_loader_mbin.self_test(loader_dir / "tools", plan.say)
        (loader_dir / "tools" / "mbincompiler_self_test.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if not result.get("ok") and getattr(args, "strict_deps", False):
            raise SetupError("MBINCompiler self-test failed under --strict-deps")


def preindex_cache(app: Path, loader_dir: Path, args: argparse.Namespace, plan: Plan, dry_run: bool) -> None:
    if getattr(args, "skip_index", False):
        plan.say("Skipping PAK index prewarm by request")
        return
    py = venv_python(loader_dir)
    loader = loader_dir / "nms_lite_loader.py"
    cmd = [str(py), str(loader), "index", "--game-app", str(app)]
    if getattr(args, "force_index", False):
        cmd.append("--force-reindex")
    if not getattr(args, "index_no_hashes", False):
        cmd.append("--hashes")
    plan.run("Build or refresh PAK file tree cache", subprocess.check_call, cmd)


def shim_text(loader_dir: Path, app: Path, real: Path) -> str:
    return f"""#!/bin/bash
# {SHIM_MARKER} v1
set -u

GAME_APP="{app}"
LOADER_DIR="{loader_dir}"
PY="$LOADER_DIR/.venv/bin/python3"
if [ ! -x "$PY" ]; then
  PY="$LOADER_DIR/.venv/bin/python"
fi
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "${{PY:-}}" ] || [ ! -x "$PY" ]; then
  osascript -e 'display dialog "NMS Mod Loader: python3 not found" buttons {{"OK"}} default button "OK"' >/dev/null 2>&1 || true
  exit 127
fi

exec "$PY" "$LOADER_DIR/nms_lite_loader.py" launch --game-app "$GAME_APP" --real-exe "{real}" -- "$@"
"""


def confirm_install(force_yes: bool, game_dir: Path) -> None:
    if force_yes:
        return
    print()
    print("This will modify the game app bundle by replacing the executable with a shim.")
    print("Backup/rollback:")
    print(f"  - Original executable is moved to: {game_dir / APP_NAME / 'Contents' / 'MacOS' / (EXECUTABLE_NAME + REAL_SUFFIX)}")
    print("  - `setup_nms_loader.py uninstall` restores it.")
    print("It does not modify PAK files during setup.")
    answer = input("Continue? Type YES to install: ").strip()
    if answer != "YES":
        raise SetupError("Install cancelled")


def install(args: argparse.Namespace, dry_run: bool = False) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    app, _macos_dir, exe, real, loader_dir = paths_for(game_dir)
    plan = Plan(dry_run, loader_dir / "setup.log")
    project_dir = Path(__file__).resolve().parent
    confirm_install(args.yes or dry_run, game_dir)

    plan.say(f"Game dir:   {game_dir}")
    plan.say(f"Loader dir: {loader_dir}")
    plan.say(f"MODS dir:   {game_dir / MODS_DIR_NAME}")

    ensure_loader_files(project_dir, loader_dir, plan)
    ensure_dependencies(project_dir, loader_dir, args, plan, dry_run)
    preindex_cache(app, loader_dir, args, plan, dry_run)

    if real.exists():
        plan.say(f"Original executable backup already exists: {real}")
        if not is_shim(exe) and not args.force:
            raise SetupError("Backup exists but current executable is not our shim. Steam likely updated the game. Run `repair` to rotate the new executable into the backup and reinstall the shim.")
    else:
        plan.run("Move original executable to backup", shutil.move, str(exe), str(real))

    plan.run("Write executable shim", write_text, exe, shim_text(loader_dir, app, real))
    plan.run("Mark shim executable", make_executable, exe)
    if not dry_run:
        write_install_state(game_dir, app, exe, real, loader_dir)

    if args.codesign:
        plan.run("Ad-hoc codesign app bundle", subprocess.check_call, ["codesign", "--force", "--deep", "--sign", "-", str(app)])
    elif not dry_run:
        print("[note] Skipped codesign. If macOS complains, rerun install with --codesign.")

    print()
    print("Install plan complete." if dry_run else "Installed.")
    return 0


def uninstall(args: argparse.Namespace, dry_run: bool = False) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    _app, _macos_dir, exe, real, loader_dir = paths_for(game_dir)
    plan = Plan(dry_run, loader_dir / "setup.log")
    if not real.exists():
        raise SetupError(f"Original executable backup not found: {real}")
    if exe.exists() and not is_shim(exe) and not args.force:
        raise SetupError("Current executable is not our shim. Use --force only if you are sure.")
    plan.run("Remove shim", lambda: exe.unlink(missing_ok=True))
    plan.run("Restore original executable", shutil.move, str(real), str(exe))
    plan.run("Mark original executable executable", make_executable, exe)
    if args.remove_loader_dir:
        plan.run("Remove loader directory", shutil.rmtree, loader_dir, True)
    print("Uninstall plan complete." if dry_run else "Uninstalled.")
    return 0


def repair(args: argparse.Namespace, dry_run: bool = False) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    app, _macos_dir, exe, real, loader_dir = paths_for(game_dir)
    plan = Plan(dry_run, loader_dir / "setup.log")
    project_dir = Path(__file__).resolve().parent
    plan.say("Repair mode handles Steam/game updates that overwrote the shim.")
    ensure_loader_files(project_dir, loader_dir, plan)
    ensure_dependencies(project_dir, loader_dir, args, plan, dry_run)
    preindex_cache(app, loader_dir, args, plan, dry_run)

    if is_shim(exe):
        plan.say("Current executable is already the loader shim.")
        if not real.exists():
            raise SetupError(f"Shim exists but original executable backup is missing: {real}")
    else:
        if real.exists():
            archive = real.with_name(f"{real.name}.previous.{time.strftime('%Y%m%d-%H%M%S')}")
            plan.run(f"Archive previous original executable to {archive.name}", shutil.move, str(real), str(archive))
        plan.run("Move current game executable to loader original backup", shutil.move, str(exe), str(real))

    plan.run("Write executable shim", write_text, exe, shim_text(loader_dir, app, real))
    plan.run("Mark shim executable", make_executable, exe)
    if not dry_run:
        write_install_state(game_dir, app, exe, real, loader_dir)
    if args.codesign:
        plan.run("Ad-hoc codesign app bundle", subprocess.check_call, ["codesign", "--force", "--deep", "--sign", "-", str(app)])
    print("Repair plan complete." if dry_run else "Repaired.")
    return 0


def deps(args: argparse.Namespace, dry_run: bool = False) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    _app, _macos_dir, _exe, _real, loader_dir = paths_for(game_dir)
    plan = Plan(dry_run, loader_dir / "setup.log")
    project_dir = Path(__file__).resolve().parent
    ensure_loader_files(project_dir, loader_dir, plan)
    ensure_dependencies(project_dir, loader_dir, args, plan, dry_run)
    print("Dependency plan complete." if dry_run else "Dependencies updated.")
    return 0


def index_cache_command(args: argparse.Namespace, dry_run: bool = False) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    app, _macos_dir, _exe, _real, loader_dir = paths_for(game_dir)
    plan = Plan(dry_run, loader_dir / "setup.log")
    if not loader_dir.exists():
        raise SetupError(f"Loader directory not found; install first: {loader_dir}")
    preindex_cache(app, loader_dir, args, plan, dry_run)
    print("Index plan complete." if dry_run else "Index updated.")
    return 0


def update_installed_loader(args: argparse.Namespace, dry_run: bool = False) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    app, _macos_dir, exe, real, loader_dir = paths_for(game_dir)
    plan = Plan(dry_run, loader_dir / "setup.log")
    project_dir = Path(__file__).resolve().parent
    if not real.exists():
        raise SetupError(f"Original executable backup not found: {real}; run install or repair first")
    if exe.exists() and not is_shim(exe) and not args.force:
        raise SetupError("Current executable is not our shim. Steam likely updated the game; run `repair` or pass --force if you know what changed.")
    plan.say("Update mode syncs the current project files into the installed loader.")
    ensure_loader_files(project_dir, loader_dir, plan)
    ensure_dependencies(project_dir, loader_dir, args, plan, dry_run)
    plan.run("Rewrite executable shim", write_text, exe, shim_text(loader_dir, app, real))
    plan.run("Mark shim executable", make_executable, exe)
    if not dry_run:
        write_install_state(game_dir, app, exe, real, loader_dir)
    preindex_cache(app, loader_dir, args, plan, dry_run)
    print("Update plan complete." if dry_run else "Updated.")
    return 0


def doctor(args: argparse.Namespace) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    app, _macos_dir, exe, real, loader_dir = paths_for(game_dir)
    venv_py = loader_dir / ".venv" / "bin" / "python3"
    hgpak = loader_dir / ".venv" / "bin" / "hgpaktool"
    print(f"Game dir:       {game_dir}")
    print(f"App:            {app} ({'ok' if app.exists() else 'missing'})")
    print(f"Executable:     {exe} ({'shim' if exe.exists() and is_shim(exe) else 'not shim'})")
    print(f"Original exe:   {real} ({'ok' if real.exists() else 'missing'})")
    print(f"Loader dir:     {loader_dir} ({'ok' if loader_dir.exists() else 'missing'})")
    print(f"MODS dir:       {game_dir / MODS_DIR_NAME} ({'ok' if (game_dir / MODS_DIR_NAME).exists() else 'missing'})")
    print(f"Venv python:    {venv_py} ({'ok' if venv_py.exists() else 'missing'})")
    print(f"hgpaktool:      {hgpak} ({'ok' if hgpak.exists() else 'missing'})")
    print(f"MBINCompiler:   {loader_dir / 'tools' / 'MBINCompiler'} ({'ok' if (loader_dir / 'tools' / 'MBINCompiler').exists() else 'missing'})")
    state = install_state_path(loader_dir)
    print(f"Install state:  {state} ({'ok' if state.exists() else 'missing'})")
    if exe.exists() and not is_shim(exe) and real.exists():
        print("Update status:  current executable is not shim but backup exists; run `repair` after Steam updates.")
    tools_dir = loader_dir / "tools"
    dotnet = nms_loader_mbin.probe_dotnet(tools_dir=tools_dir)
    dotnet_label = dotnet.get("path") if dotnet.get("ok") else "broken/missing"
    if dotnet.get("arch"):
        dotnet_label = f"{dotnet_label} ({dotnet.get('arch')})"
    print(f".NET host:      {dotnet_label}")
    if not dotnet.get("ok"):
        failures = dotnet.get("failures") or []
        if failures:
            last = failures[-1]
            print(f".NET detail:    {str(last.get('output') or last.get('error') or last)[:500]}")
    if tools_dir.exists():
        test = nms_loader_mbin.self_test(tools_dir)
        print(f"MBIN self-test: {'ok' if test.get('ok') else 'failed'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install or remove the lightweight NMS mod loader shim")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--game-dir", help=f"Directory containing {APP_NAME}; defaults to the standard macOS Steam library")
        p.add_argument("--force", action="store_true", help="Allow replacing an unexpected current executable")

    def deps_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mbin", choices=("auto", "bundled", "release", "source", "skip"), default="auto", help="How to install MBINCompiler")
        p.add_argument("--strict-deps", action="store_true", help="Fail if MBINCompiler self-test fails")
        p.add_argument("--no-mbin-self-test", action="store_true", help="Skip MBINCompiler self-test after install")

    def index_opts(p: argparse.ArgumentParser) -> None:
        p.add_argument("--skip-index", action="store_true", help="Do not prewarm the PAK file tree cache")
        p.add_argument("--index-no-hashes", "--no-hashes", action="store_true", help="Index file trees without recording PAK SHA256 hashes")
        p.add_argument("--force-index", action="store_true", help="Rebuild all PAK file tree cache entries")

    p_dry = sub.add_parser("dry-run", help="Show install changes without modifying files")
    common(p_dry)
    deps_opts(p_dry)
    index_opts(p_dry)
    p_dry.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    p_dry.add_argument("--codesign", action="store_true", help="Include optional ad-hoc codesign in the plan")

    p_install = sub.add_parser("install", help="Install the app executable shim")
    common(p_install)
    deps_opts(p_install)
    index_opts(p_install)
    p_install.add_argument("--yes", action="store_true", help="Skip the interactive confirmation")
    p_install.add_argument("--codesign", action="store_true", help="Run ad-hoc codesign after installing the shim")

    p_uninstall = sub.add_parser("uninstall", help="Restore the original executable")
    common(p_uninstall)
    p_uninstall.add_argument("--remove-loader-dir", action="store_true", help="Also remove the copied loader directory")

    p_doctor = sub.add_parser("doctor", help="Inspect install state")
    common(p_doctor)

    p_repair = sub.add_parser("repair", help="Reinstall shim after Steam/game updates overwrite it")
    common(p_repair)
    deps_opts(p_repair)
    index_opts(p_repair)
    p_repair.add_argument("--yes", action="store_true", help="Reserved for symmetry; repair is explicit")
    p_repair.add_argument("--codesign", action="store_true", help="Run ad-hoc codesign after repairing the shim")
    p_repair.add_argument("--dry-run", action="store_true", help="Show repair changes without modifying files")

    p_deps = sub.add_parser("deps", help="Install or refresh Python/MBINCompiler dependencies only")
    common(p_deps)
    deps_opts(p_deps)
    p_deps.add_argument("--dry-run", action="store_true", help="Show dependency changes without modifying files")

    p_index = sub.add_parser("index", help="Build or refresh the installed loader PAK file tree cache")
    common(p_index)
    index_opts(p_index)
    p_index.add_argument("--dry-run", action="store_true", help="Show index command without modifying cache")

    p_update = sub.add_parser("update", help="Sync current project scripts into the installed loader")
    common(p_update)
    deps_opts(p_update)
    index_opts(p_update)
    p_update.add_argument("--dry-run", action="store_true", help="Show update changes without modifying files")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "dry-run":
            return install(args, dry_run=True)
        if args.command == "install":
            return install(args, dry_run=False)
        if args.command == "uninstall":
            return uninstall(args, dry_run=False)
        if args.command == "doctor":
            return doctor(args)
        if args.command == "repair":
            return repair(args, dry_run=args.dry_run)
        if args.command == "deps":
            return deps(args, dry_run=args.dry_run)
        if args.command == "index":
            return index_cache_command(args, dry_run=args.dry_run)
        if args.command == "update":
            return update_installed_loader(args, dry_run=args.dry_run)
    except SetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: command failed with exit {exc.returncode}: {exc.cmd}", file=sys.stderr)
        return exc.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
