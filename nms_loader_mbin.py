#!/usr/bin/env python3
"""MBINCompiler dependency helpers for the lightweight NMS loader."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional


MBIN_REPO = "monkeyman192/MBINCompiler"
MBIN_GIT_URL = "https://github.com/monkeyman192/MBINCompiler.git"
MBIN_RELEASE_API = "https://api.github.com/repos/monkeyman192/MBINCompiler/releases/latest"
DOTNET_INSTALL_SCRIPT_URL = "https://dot.net/v1/dotnet-install.sh"
REQUIRED_FILES = (
    "MBINCompiler",
    "MBINCompiler.dll",
    "MBINCompiler.exe",
    "MBINCompiler.deps.json",
    "libMBIN.dll",
    "MBINCompiler.runtimeconfig.json",
)
RELEASE_FILES = ("MBINCompiler.exe", "libMBIN.dll", "mapping.json", "report.json")
RUNTIMECONFIG = """{
  "runtimeOptions": {
    "tfm": "net8.0",
    "framework": { "name": "Microsoft.NETCore.App", "version": "8.0.0" },
    "rollForward": "LatestMajor"
  }
}
"""


def noop_log(message: str) -> None:
    print(message)


def dotnet_candidates(tools_dir: Optional[Path] = None) -> List[Dict[str, Optional[str]]]:
    candidates: List[Dict[str, Optional[str]]] = []
    if tools_dir:
        candidates.append({"path": str(tools_dir / "dotnet-osx-x64" / "dotnet"), "arch": "x86_64"})
    override = os.environ.get("DOTNET_HOST_OVERRIDE")
    if override:
        candidates.append({"path": str(Path(override).expanduser()), "arch": os.environ.get("DOTNET_HOST_ARCH")})
    found = shutil.which("dotnet")
    if found:
        candidates.append({"path": found, "arch": None})
    for candidate in (
        Path("/opt/homebrew/opt/dotnet/bin/dotnet"),
        Path("/opt/homebrew/opt/dotnet@10/bin/dotnet"),
        Path("/opt/homebrew/opt/dotnet@9/bin/dotnet"),
        Path("/opt/homebrew/opt/dotnet@8/bin/dotnet"),
        Path("/usr/local/share/dotnet/dotnet"),
        Path.home() / ".dotnet" / "dotnet",
    ):
        candidates.append({"path": str(candidate), "arch": None})

    seen = set()
    result: List[Dict[str, Optional[str]]] = []
    for candidate in candidates:
        key = (candidate["path"], candidate.get("arch"))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def dotnet_command(path: Path, arch_name: Optional[str], *args: str) -> List[str]:
    cmd = [str(path), *args]
    if arch_name == "x86_64":
        cmd = ["/usr/bin/arch", "-x86_64", *cmd]
    return cmd


def probe_dotnet(
    log: Callable[[str], None] = noop_log,
    tools_dir: Optional[Path] = None,
    need_sdk: bool = False,
) -> Dict[str, object]:
    searched: List[str] = []
    failures: List[Dict[str, object]] = []
    for item in dotnet_candidates(tools_dir):
        candidate = Path(str(item["path"]))
        arch_name = item.get("arch")
        label = f"{candidate} ({arch_name})" if arch_name else str(candidate)
        searched.append(label)
        if not candidate.exists() or not os.access(candidate, os.X_OK):
            continue
        try:
            runtimes = subprocess.run(
                dotnet_command(candidate, arch_name, "--list-runtimes"),
                capture_output=True,
                text=True,
                timeout=20,
            )
            info = subprocess.run(
                dotnet_command(candidate, arch_name, "--info"),
                capture_output=True,
                text=True,
                timeout=20,
            )
            version = None
            if need_sdk:
                version = subprocess.run(
                    dotnet_command(candidate, arch_name, "--version"),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
        except Exception as exc:
            failures.append({"path": str(candidate), "arch": arch_name, "error": repr(exc)})
            continue
        output = (runtimes.stdout + runtimes.stderr + info.stdout + info.stderr).strip()
        if version:
            output += "\n" + (version.stdout + version.stderr).strip()
        sdk_ok = not need_sdk or (version is not None and version.returncode == 0)
        if runtimes.returncode == 0 and info.returncode == 0 and sdk_ok and "Failed to create CoreCLR" not in output:
            host_line = next((line.strip() for line in info.stdout.splitlines() if line.strip().startswith("Version:")), "")
            suffix = f" via {arch_name}" if arch_name else ""
            log(f".NET host OK: {candidate}{suffix} {host_line}".strip())
            return {
                "ok": True,
                "path": str(candidate),
                "arch": arch_name,
                "version": host_line.replace("Version:", "").strip(),
                "searched": searched,
                "failures": failures,
            }
        failures.append(
            {
                "path": str(candidate),
                "arch": arch_name,
                "list_returncode": runtimes.returncode,
                "info_returncode": info.returncode,
                "version_returncode": version.returncode if version else None,
                "output": output[-2000:],
            }
        )
    return {"ok": False, "path": None, "searched": searched, "failures": failures}


def write_wrapper(tools_dir: Path) -> Path:
    wrapper = tools_dir / "MBINCompiler"
    text = """#!/bin/bash
set -u
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
APP="$HERE/MBINCompiler.dll"
if [ ! -f "$APP" ]; then
  APP="$HERE/MBINCompiler.exe"
fi
unset DYLD_INSERT_LIBRARIES
unset DYLD_LIBRARY_PATH
unset DYLD_FRAMEWORK_PATH
unset DYLD_FALLBACK_LIBRARY_PATH
unset DYLD_FALLBACK_FRAMEWORK_PATH
ARCH_PREFIX=()
if [ -n "${DOTNET_HOST_OVERRIDE:-}" ]; then
  DOTNET="$DOTNET_HOST_OVERRIDE"
  if [ "${DOTNET_HOST_ARCH:-}" = "x86_64" ]; then
    ARCH_PREFIX=(/usr/bin/arch -x86_64)
  fi
elif [ -x "$HERE/dotnet-osx-x64/dotnet" ]; then
  DOTNET="$HERE/dotnet-osx-x64/dotnet"
  DOTNET_ROOT="$HERE/dotnet-osx-x64"
  export DOTNET_ROOT
  ARCH_PREFIX=(/usr/bin/arch -x86_64)
else
  DOTNET="$(command -v dotnet || true)"
fi
if [ -z "${DOTNET:-}" ] || [ ! -x "$DOTNET" ]; then
  for cand in \
    /opt/homebrew/opt/dotnet/bin/dotnet \
    /opt/homebrew/opt/dotnet@10/bin/dotnet \
    /opt/homebrew/opt/dotnet@9/bin/dotnet \
    /opt/homebrew/opt/dotnet@8/bin/dotnet \
    /usr/local/share/dotnet/dotnet \
    "$HOME/.dotnet/dotnet"; do
    if [ -x "$cand" ]; then DOTNET="$cand"; break; fi
  done
fi
if [ -z "${DOTNET:-}" ] || [ ! -x "$DOTNET" ]; then
  echo "ERROR: dotnet host not found. Install Microsoft .NET 8 Runtime or SDK." >&2
  exit 127
fi
if [ "${1:-}" = "--self-test" ] || [ "${1:-}" = "self-test" ]; then
  "${ARCH_PREFIX[@]}" "$DOTNET" --list-runtimes >/dev/null || exit $?
  "${ARCH_PREFIX[@]}" "$DOTNET" "$APP" version
  exit $?
fi
exec "${ARCH_PREFIX[@]}" "$DOTNET" "$APP" "$@"
"""
    wrapper.write_text(text, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | 0o755)
    return wrapper


def rosetta_available() -> bool:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        run = subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"], timeout=10)
    except Exception:
        return False
    return run.returncode == 0


def install_portable_dotnet_x64(tools_dir: Path, log: Callable[[str], None] = noop_log) -> Dict[str, object]:
    if not rosetta_available():
        raise RuntimeError("Portable x64 .NET fallback requires Rosetta on Apple Silicon")
    install_dir = tools_dir / "dotnet-osx-x64"
    dotnet = install_dir / "dotnet"
    existing = probe_dotnet(log, tools_dir=tools_dir)
    if existing.get("ok") and existing.get("path") == str(dotnet):
        return existing
    tools_dir.mkdir(parents=True, exist_ok=True)
    script = tools_dir / "dotnet-install.sh"
    download_url(DOTNET_INSTALL_SCRIPT_URL, script, log)
    script.chmod(script.stat().st_mode | 0o755)
    cmd = [
        "/bin/bash",
        str(script),
        "--runtime",
        "dotnet",
        "--channel",
        "8.0",
        "--architecture",
        "x64",
        "--install-dir",
        str(install_dir),
        "--no-path",
    ]
    log("Installing portable .NET 8 x64 runtime: " + " ".join(cmd))
    subprocess.check_call(cmd)
    probe = probe_dotnet(log, tools_dir=tools_dir)
    if not probe.get("ok") or probe.get("path") != str(dotnet):
        raise RuntimeError("Portable .NET x64 runtime installed but did not pass probe")
    metadata = {
        "source": "dotnet-install",
        "url": DOTNET_INSTALL_SCRIPT_URL,
        "runtime": "dotnet",
        "channel": "8.0",
        "architecture": "x64",
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "probe": probe,
    }
    (tools_dir / "dotnet_runtime_source.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return probe


def ensure_dotnet_for_mbin(tools_dir: Path, log: Callable[[str], None] = noop_log) -> Dict[str, object]:
    probe = probe_dotnet(log, tools_dir=tools_dir)
    if probe.get("ok"):
        return probe
    log("No usable .NET host found for MBINCompiler; trying portable .NET 8 x64 runtime under Rosetta")
    return install_portable_dotnet_x64(tools_dir, log)


def copy_bundled(project_dir: Path, tools_dir: Path, log: Callable[[str], None] = noop_log) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    candidates = [
        project_dir / "vendor" / "mbincompiler",
        project_dir / "nms-mod-installer-macos" / "bin",
        project_dir / "old",
    ]
    found = False
    for source_dir in candidates:
        if not source_dir.exists():
            continue
        for name in REQUIRED_FILES:
            source = source_dir / name
            dest = tools_dir / name
            if source.exists():
                found = True
                if not dest.exists():
                    shutil.copy2(source, tools_dir / name)
                    log(f"Copied bundled MBINCompiler file: {name}")
    if not (tools_dir / "MBINCompiler.runtimeconfig.json").exists():
        (tools_dir / "MBINCompiler.runtimeconfig.json").write_text(RUNTIMECONFIG, encoding="utf-8")
        log("Wrote MBINCompiler.runtimeconfig.json")
    write_wrapper(tools_dir)
    if not found:
        raise RuntimeError("No bundled MBINCompiler files found under vendor/mbincompiler, nms-mod-installer-macos/bin, or old/")


def latest_release(log: Callable[[str], None] = noop_log) -> Dict[str, object]:
    with urllib.request.urlopen(MBIN_RELEASE_API, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    log(f"Latest MBINCompiler release: {data.get('tag_name')} ({data.get('published_at')})")
    return data


def download_url(url: str, dest: Path, log: Callable[[str], None] = noop_log) -> None:
    log(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        dest.write_bytes(response.read())


def download_release(tools_dir: Path, log: Callable[[str], None] = noop_log) -> None:
    tools_dir.mkdir(parents=True, exist_ok=True)
    release = latest_release(log)
    assets = release.get("assets", [])
    by_name = {asset.get("name"): asset for asset in assets if isinstance(asset, dict)}
    for name in RELEASE_FILES:
        asset = by_name.get(name)
        if not asset:
            if name in ("mapping.json", "report.json"):
                continue
            raise RuntimeError(f"Latest MBINCompiler release is missing asset: {name}")
        url = asset.get("browser_download_url")
        if not url:
            raise RuntimeError(f"Release asset has no browser_download_url: {name}")
        download_url(url, tools_dir / name, log)
    (tools_dir / "MBINCompiler.runtimeconfig.json").write_text(RUNTIMECONFIG, encoding="utf-8")
    write_wrapper(tools_dir)
    metadata = {
        "source": "github-release",
        "repo": MBIN_REPO,
        "tag": release.get("tag_name"),
        "published_at": release.get("published_at"),
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (tools_dir / "mbincompiler_source.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def build_from_source(tools_dir: Path, log: Callable[[str], None] = noop_log) -> None:
    dotnet = probe_dotnet(log, need_sdk=True)
    if not dotnet.get("ok"):
        raise RuntimeError("Cannot build MBINCompiler from source because no working dotnet host is available")
    dotnet_path = str(dotnet["path"])
    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mbincompiler-src-") as tmp:
        repo_dir = Path(tmp) / "MBINCompiler"
        subprocess.check_call(["git", "clone", "--depth", "1", MBIN_GIT_URL, str(repo_dir)])
        publish_dir = repo_dir / "publish"
        cmd = [
            dotnet_path,
            "publish",
            "MBINCompiler/MBINCompiler.csproj",
            "-c",
            "Release",
            "-f",
            "net8.0",
            "-r",
            "win-x64",
            "--no-self-contained",
            "/nowarn:cs0618",
            "/nowarn:cs0169",
            "/nowarn:cs0414",
            "-o",
            str(publish_dir),
        ]
        log("Building MBINCompiler from source: " + " ".join(cmd))
        subprocess.check_call(cmd, cwd=str(repo_dir))
        for candidate in ("MBINCompiler.exe", "libMBIN.dll"):
            found = next(publish_dir.rglob(candidate), None)
            if not found:
                raise RuntimeError(f"Build output missing {candidate}")
            shutil.copy2(found, tools_dir / candidate)
        (tools_dir / "MBINCompiler.runtimeconfig.json").write_text(RUNTIMECONFIG, encoding="utf-8")
        write_wrapper(tools_dir)
    metadata = {
        "source": "github-source",
        "repo": MBIN_REPO,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (tools_dir / "mbincompiler_source.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def ensure_mbincompiler(
    project_dir: Path,
    tools_dir: Path,
    mode: str,
    log: Callable[[str], None] = noop_log,
) -> None:
    if mode == "skip":
        log("Skipping MBINCompiler installation by request")
        return
    if mode == "bundled":
        copy_bundled(project_dir, tools_dir, log)
        ensure_dotnet_for_mbin(tools_dir, log)
    elif mode == "release":
        download_release(tools_dir, log)
        ensure_dotnet_for_mbin(tools_dir, log)
    elif mode == "source":
        build_from_source(tools_dir, log)
    elif mode == "auto":
        try:
            copy_bundled(project_dir, tools_dir, log)
        except Exception as exc:
            log(f"Bundled MBINCompiler failed: {exc}; trying GitHub release")
            download_release(tools_dir, log)
        ensure_dotnet_for_mbin(tools_dir, log)
    else:
        raise RuntimeError(f"Unknown MBINCompiler mode: {mode}")


def self_test(tools_dir: Path, log: Callable[[str], None] = noop_log) -> Dict[str, object]:
    wrapper = tools_dir / "MBINCompiler"
    exe = tools_dir / "MBINCompiler.exe"
    dll = tools_dir / "MBINCompiler.dll"
    lib = tools_dir / "libMBIN.dll"
    result: Dict[str, object] = {
        "wrapper": str(wrapper),
        "dll_exists": dll.exists(),
        "exe_exists": exe.exists(),
        "lib_exists": lib.exists(),
        "dotnet": probe_dotnet(log, tools_dir=tools_dir),
    }
    if not wrapper.exists() or not (dll.exists() or exe.exists()) or not lib.exists():
        result["ok"] = False
        result["error"] = "MBINCompiler wrapper/app/lib missing"
        return result
    try:
        run = subprocess.run([str(wrapper), "--self-test"], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        result["ok"] = False
        result["error"] = repr(exc)
        return result
    result["returncode"] = run.returncode
    result["stdout"] = run.stdout[-4000:]
    result["stderr"] = run.stderr[-4000:]
    result["ok"] = run.returncode == 0
    if run.returncode == 0:
        log("MBINCompiler self-test OK")
    else:
        log(f"MBINCompiler self-test failed: {run.stderr.strip() or run.stdout.strip()}")
    return result
