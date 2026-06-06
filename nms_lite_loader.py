#!/usr/bin/env python3
"""
Lightweight Steam-chain launcher for No Man's Sky macOS mods.

Design target:
- A shim inside No Man's Sky.app starts this script first.
- This script scans a sibling MODS directory, patches affected MACOSBANKS PAKs,
  launches the original game executable with all inherited arguments/env, then
  restores the original PAKs when the game exits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import nms_loader_mbin


APP_NAME = "No Man's Sky.app"
EXECUTABLE_NAME = "No Man's Sky"
REAL_EXECUTABLE_SUFFIX = ".nms-loader-original"
MACOSBANKS_REL = Path("Contents/Resources/GAMEDATA/MACOSBANKS")
STATE_DIR_NAME = "_NMSModLoader"
MODS_DIR_NAME = "MODS"
ACTIVE_SESSION_FILE = "active_session.json"
PAK_INDEX_FILE = "pak_index.json"
PAK_INDEX_SCHEMA = 2
MOD_PRIORITY_FILE = "mod_priority.json"
LOCK_FILE = "session.lock"
GLOBAL_PREFIXES = ("globals/",)
SKIP_FILENAMES = {
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    "readme",
    "readme.txt",
    "readme.md",
}
STRIP_PREFIXES = (
    "gamedata/pcbanks/",
    "gamedata/macosbanks/",
    "pcbanks/",
    "macosbanks/",
)
HELPER_ENV_DROP = {
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_FALLBACK_FRAMEWORK_PATH",
}


class LoaderError(RuntimeError):
    pass


class Logger:
    def __init__(self, mirror_stdout: bool = True, log_file: Optional[Path] = None):
        self.mirror_stdout = mirror_stdout
        self.log_file = log_file

    def attach_file(self, log_file: Path) -> None:
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        if self.mirror_stdout:
            print(line, flush=True)
        if self.log_file is not None:
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


@dataclass(frozen=True)
class GamePaths:
    game_app: Path
    game_root: Path
    macos_dir: Path
    banks_dir: Path
    mods_dir: Path
    state_dir: Path
    cache_dir: Path
    backups_dir: Path
    real_exe: Path


@dataclass(frozen=True)
class ModMatch:
    mod_name: str
    source: Path
    internal_path: str
    pak_name: str
    needs_convert: bool


class PakIndex:
    def __init__(self, entries: Dict[str, Dict[str, str]], pak_contents: Dict[str, List[str]]):
        self.entries = entries
        self.pak_contents = pak_contents

    def resolve(self, internal_path: str) -> Optional[Tuple[str, str]]:
        hit = self.entries.get(normalize_internal(internal_path))
        if hit:
            return hit["pak"], hit["path"]
        return None

    def resolve_unique_suffix(self, suffix_path: str) -> Optional[Tuple[str, str]]:
        suffix = normalize_internal(suffix_path)
        suffix_with_slash = "/" + suffix
        matches: List[Tuple[str, str]] = []
        for lower, hit in self.entries.items():
            if lower == suffix or lower.endswith(suffix_with_slash):
                matches.append((hit["pak"], hit["path"]))
        if len(matches) == 1:
            return matches[0]
        return None

    def resolve_global(self, filename: str) -> Optional[Tuple[str, str]]:
        return self.resolve_unique_suffix(filename)


def human_size(nbytes: int) -> str:
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_internal(path: str) -> str:
    normalized = path.replace("\\", "/").strip().strip("/")
    lower = normalized.lower()
    for prefix in STRIP_PREFIXES:
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    while lower.startswith("./"):
        lower = lower[2:]
    return lower


def is_exml(path: str | Path) -> bool:
    return str(path).lower().endswith((".exml", ".mxml"))


def exml_to_mbin(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".exml"):
        return path[:-5] + ".mbin"
    if lower.endswith(".mxml"):
        return path[:-5] + ".mbin"
    return path


def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def clean_mod_file_name(path: Path) -> bool:
    name = path.name.lower()
    return name not in SKIP_FILENAMES and not name.startswith(".")


def resolve_game_paths(game_app_arg: Optional[str], real_exe_arg: Optional[str]) -> GamePaths:
    script_dir = Path(__file__).resolve().parent

    if game_app_arg:
        game_app = Path(game_app_arg).expanduser().resolve()
    elif os.environ.get("NMS_LOADER_GAME_APP"):
        game_app = Path(os.environ["NMS_LOADER_GAME_APP"]).expanduser().resolve()
    else:
        candidates = [
            script_dir / APP_NAME,
            script_dir.parent / APP_NAME,
            Path.cwd() / APP_NAME,
        ]
        game_app = next((p.resolve() for p in candidates if p.exists()), candidates[0].resolve())

    if not game_app.exists():
        raise LoaderError(f"Game app not found: {game_app}")
    banks_dir = game_app / MACOSBANKS_REL
    if not banks_dir.is_dir():
        raise LoaderError(f"MACOSBANKS not found: {banks_dir}")

    game_root = game_app.parent
    macos_dir = game_app / "Contents" / "MacOS"
    if real_exe_arg:
        real_exe = Path(real_exe_arg).expanduser().resolve()
    else:
        backup = macos_dir / (EXECUTABLE_NAME + REAL_EXECUTABLE_SUFFIX)
        real_exe = backup if backup.exists() else macos_dir / EXECUTABLE_NAME
    if not real_exe.exists():
        raise LoaderError(f"Real game executable not found: {real_exe}")

    state_dir = game_root / STATE_DIR_NAME
    return GamePaths(
        game_app=game_app,
        game_root=game_root,
        macos_dir=macos_dir,
        banks_dir=banks_dir,
        mods_dir=game_root / MODS_DIR_NAME,
        state_dir=state_dir,
        cache_dir=state_dir / "cache",
        backups_dir=state_dir / "backups",
        real_exe=real_exe,
    )


def find_hgpaktool() -> Optional[str]:
    candidates: List[Optional[str]] = [
        os.environ.get("NMS_LOADER_HGPAKTOOL"),
        shutil.which("hgpaktool"),
        str(Path(sys.executable).resolve().parent / "hgpaktool"),
        str(Path(__file__).resolve().parent / ".venv" / "bin" / "hgpaktool"),
        str(Path.home() / "Library" / "Python" / "3.14" / "bin" / "hgpaktool"),
        str(Path.home() / "Library" / "Python" / "3.13" / "bin" / "hgpaktool"),
        str(Path.home() / "Library" / "Python" / "3.12" / "bin" / "hgpaktool"),
        str(Path.home() / "Library" / "Python" / "3.11" / "bin" / "hgpaktool"),
        str(Path.home() / "Library" / "Python" / "3.10" / "bin" / "hgpaktool"),
        str(Path.home() / "Library" / "Python" / "3.9" / "bin" / "hgpaktool"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def helper_env() -> Dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in HELPER_ENV_DROP:
            env.pop(key, None)
    return env


def run_hgpaktool(tool_path: str, args: Sequence[str], cwd: Optional[Path], log: Logger) -> str:
    cmd = [tool_path, *map(str, args)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd) if cwd else None, env=helper_env())
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LoaderError(f"hgpaktool failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    if result.stderr.strip():
        log(f"hgpaktool: {result.stderr.strip()}")
    return result.stdout


def pak_signature(
    banks_dir: Path,
    cached_signature: Optional[Dict[str, Dict[str, object]]] = None,
    include_hashes: bool = False,
    log: Optional[Logger] = None,
) -> Dict[str, Dict[str, object]]:
    cached_signature = cached_signature or {}
    sig: Dict[str, Dict[str, object]] = {}
    for pak in sorted(banks_dir.glob("*.pak")):
        if pak.name.startswith("_"):
            continue
        st = pak.stat()
        item: Dict[str, object] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
        cached = cached_signature.get(pak.name, {})
        same_quick = cached.get("size") == st.st_size and cached.get("mtime_ns") == st.st_mtime_ns
        cached_hash = cached.get("sha256")
        if same_quick and cached_hash:
            item["sha256"] = cached_hash
        elif include_hashes or cached_hash:
            if log:
                log(f"计算 PAK hash：{pak.name} ({human_size(st.st_size)})")
            item["sha256"] = sha256_file(pak)
        sig[pak.name] = item
    return sig


def pak_signature_changed(previous: Optional[Dict[str, object]], current: Dict[str, object]) -> bool:
    if not previous:
        return True
    if previous.get("sha256") and current.get("sha256"):
        return previous.get("sha256") != current.get("sha256")
    return previous.get("size") != current.get("size") or previous.get("mtime_ns") != current.get("mtime_ns")


def rebuild_entries_from_contents(pak_contents: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    entries: Dict[str, Dict[str, str]] = {}
    for pak_name in sorted(pak_contents):
        for internal in pak_contents[pak_name]:
            entries[normalize_internal(internal)] = {"pak": pak_name, "path": internal}
    return entries


def list_pak_contents(pak: Path, tool_path: str, tmpdir: Path, listing_path: Path, log: Logger) -> List[str]:
    if listing_path.exists():
        listing_path.unlink()
    run_hgpaktool(tool_path, ["-L", str(pak)], cwd=tmpdir, log=log)
    if not listing_path.exists():
        raise LoaderError(f"hgpaktool did not produce filenames.json for {pak.name}")
    with listing_path.open("r", encoding="utf-8") as f:
        listing = json.load(f)
    files: List[str] = []
    if isinstance(listing, dict):
        for value in listing.values():
            if isinstance(value, list):
                files.extend(str(x) for x in value)
    elif isinstance(listing, list):
        files.extend(str(x) for x in listing)
    return files


def build_pak_index(paths: GamePaths, tool_path: str, log: Logger, force: bool = False, include_hashes: bool = False) -> PakIndex:
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = paths.cache_dir / PAK_INDEX_FILE
    cached = load_json(cache_path, None)
    cached_signature = cached.get("signature", {}) if isinstance(cached, dict) else {}
    current_sig = pak_signature(paths.banks_dir, cached_signature, include_hashes=include_hashes or force, log=log)

    if cached and not force:
        old_names = set(cached_signature)
        new_names = set(current_sig)
        removed = sorted(old_names - new_names)
        changed = sorted(name for name in new_names if pak_signature_changed(cached_signature.get(name), current_sig[name]))
        if not removed and not changed:
            entries = cached.get("entries", {})
            contents = cached.get("pak_contents", {})
            if cached.get("signature") != current_sig or cached.get("schema") != PAK_INDEX_SCHEMA:
                cached["schema"] = PAK_INDEX_SCHEMA
                cached["signature"] = current_sig
                cached["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_json_atomic(cache_path, cached)
            hash_note = "，含 SHA256" if any("sha256" in item for item in current_sig.values()) else ""
            log(f"使用缓存 PAK 索引：{len(entries)} 个文件条目{hash_note}")
            return PakIndex(entries, contents)
    else:
        removed = []
        changed = sorted(current_sig)

    cached_contents = cached.get("pak_contents", {}) if isinstance(cached, dict) and not force else {}
    pak_contents: Dict[str, List[str]] = {str(k): [str(x) for x in v] for k, v in cached_contents.items() if k in current_sig and k not in changed}

    if cached and not force:
        log(f"PAK 索引增量刷新：变更/新增 {len(changed)} 个，移除 {len(removed)} 个，沿用 {len(pak_contents)} 个")
    else:
        log("正在扫描 PAK 索引，这一步首次运行会比较久")

    pak_files = [paths.banks_dir / name for name in changed]
    with tempfile.TemporaryDirectory(prefix="nms-index-") as tmp:
        tmpdir = Path(tmp)
        listing_path = tmpdir / "filenames.json"
        for idx, pak in enumerate(pak_files, start=1):
            log(f"索引 [{idx}/{len(pak_files)}] {pak.name}")
            pak_contents[pak.name] = list_pak_contents(pak, tool_path, tmpdir, listing_path, log)

    entries = rebuild_entries_from_contents(pak_contents)
    save_json_atomic(
        cache_path,
        {
            "schema": PAK_INDEX_SCHEMA,
            "created_at": cached.get("created_at") if isinstance(cached, dict) and cached.get("created_at") else time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "signature": current_sig,
            "entries": entries,
            "pak_contents": pak_contents,
        },
    )
    if removed:
        log(f"已从索引移除 {len(removed)} 个不存在的 PAK：{', '.join(removed[:8])}")
    log(f"PAK 索引完成：{len(entries)} 个文件条目，{len(pak_contents)} 个 PAK")
    return PakIndex(entries, pak_contents)


def attach_run_log(log: Logger, paths: GamePaths, prefix: str) -> Path:
    if log.log_file is not None:
        log(f"日志文件：{log.log_file}")
        return log.log_file
    log_path = paths.state_dir / "logs" / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log.attach_file(log_path)
    log(f"日志文件：{log_path}")
    return log_path


def discover_mod_dirs(paths: GamePaths) -> List[Path]:
    if not paths.mods_dir.is_dir():
        return []
    return sorted((p for p in paths.mods_dir.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name.lower())


def load_mod_order(paths: GamePaths) -> List[str]:
    data = load_json(paths.state_dir / MOD_PRIORITY_FILE, {"order": []})
    order = data.get("order", []) if isinstance(data, dict) else []
    return [str(item) for item in order if str(item).strip()]


def save_mod_order(paths: GamePaths, order: Sequence[str]) -> None:
    save_json_atomic(
        paths.state_dir / MOD_PRIORITY_FILE,
        {
            "order": list(order),
            "note": "Mods are applied in this order; later mods override earlier mods on the same internal path.",
        },
    )


def ordered_mod_dirs(paths: GamePaths, log: Optional[Logger] = None, persist: bool = True) -> List[Path]:
    mods = discover_mod_dirs(paths)
    by_name = {mod.name: mod for mod in mods}
    saved = load_mod_order(paths)
    ordered: List[Path] = []
    seen = set()
    for name in saved:
        mod = by_name.get(name)
        if mod and name not in seen:
            ordered.append(mod)
            seen.add(name)
    for mod in mods:
        if mod.name not in seen:
            ordered.append(mod)
            seen.add(mod.name)
    if persist and mods:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        save_mod_order(paths, [mod.name for mod in ordered])
    if log and ordered:
        log("模组应用顺序（后面的覆盖前面的）：")
        for index, mod in enumerate(ordered, start=1):
            log(f"  {index}. {mod.name}")
    return ordered


def iter_mod_files(paths: GamePaths, log: Optional[Logger] = None) -> Iterable[Tuple[str, Path, str]]:
    for mod_dir in ordered_mod_dirs(paths, log=log):
        for root, _dirs, files in os.walk(mod_dir):
            root_path = Path(root)
            for fname in sorted(files, key=str.lower):
                source = root_path / fname
                if not clean_mod_file_name(source):
                    continue
                rel = source.relative_to(mod_dir).as_posix()
                yield mod_dir.name, source, rel


def resolve_mod_target(index: PakIndex, rel_path: str) -> Tuple[Optional[Tuple[str, str]], bool]:
    normalized = normalize_internal(rel_path)
    needs_convert = False
    search_path = normalized
    if is_exml(search_path):
        search_path = exml_to_mbin(search_path)
        needs_convert = True

    resolved = index.resolve(search_path)
    if resolved:
        return resolved, needs_convert

    for prefix in GLOBAL_PREFIXES:
        if normalized.startswith(prefix):
            filename = normalized[len(prefix):]
            if is_exml(filename):
                filename = exml_to_mbin(filename)
                needs_convert = True
            resolved = index.resolve_global(filename)
            if resolved:
                return resolved, needs_convert

    resolved = index.resolve_unique_suffix(search_path)
    if resolved:
        return resolved, needs_convert

    return None, needs_convert


def scan_mods(paths: GamePaths, index: PakIndex, log: Logger) -> Tuple[Dict[str, List[ModMatch]], List[str]]:
    pak_map: Dict[str, List[ModMatch]] = {}
    unmatched: List[str] = []
    if not paths.mods_dir.is_dir():
        log(f"未找到 MODS 文件夹：{paths.mods_dir}，将原版启动")
        return pak_map, unmatched

    seen_targets: Dict[str, List[str]] = {}
    file_count = 0
    for mod_name, source, rel in iter_mod_files(paths, log):
        file_count += 1
        resolved, needs_convert = resolve_mod_target(index, rel)
        if not resolved:
            unmatched.append(f"{mod_name}/{rel}")
            continue
        pak_name, internal_path = resolved
        match = ModMatch(mod_name, source, internal_path, pak_name, needs_convert)
        pak_map.setdefault(pak_name, []).append(match)
        seen_targets.setdefault(normalize_internal(internal_path), []).append(mod_name)

    if file_count == 0:
        log("MODS 文件夹存在，但没有可应用的文件")
        return pak_map, unmatched

    if pak_map:
        log(f"扫描完成：将影响 {len(pak_map)} 个 PAK，共 {sum(len(v) for v in pak_map.values())} 个文件")
    if unmatched:
        log(f"有 {len(unmatched)} 个文件没有匹配到官方 PAK，将跳过")
        for item in unmatched[:20]:
            log(f"  跳过：{item}")
        if len(unmatched) > 20:
            log(f"  ...还有 {len(unmatched) - 20} 个未匹配文件")

    conflicts = {path: mods for path, mods in seen_targets.items() if len(set(mods)) > 1}
    if conflicts:
        log(f"检测到 {len(conflicts)} 个覆盖冲突；同一路径按优先级列表中靠后的模组为准")
        for path, mods in list(conflicts.items())[:20]:
            log(f"  冲突：{path} <= {' -> '.join(mods)}")
    return pak_map, unmatched


def find_mbincompiler() -> Optional[Path]:
    script_dir = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("NMS_LOADER_MBINCOMPILER"),
        str(script_dir / "tools" / "MBINCompiler"),
        str(script_dir / "MBINCompiler"),
        str(script_dir.parent / "old" / "MBINCompiler"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def run_mbincompiler(input_path: Path, output_dir: Path, log: Logger) -> Optional[Path]:
    mbinc = find_mbincompiler()
    if not mbinc:
        return None

    lower = input_path.suffix.lower()
    if lower in (".exml", ".mxml"):
        tmp_name = input_path.with_suffix(".MXML").name
        expected_suffixes = (".MBIN", ".mbin")
    else:
        tmp_name = input_path.name
        expected_suffixes = (".MXML", ".EXML", ".mxml", ".exml")

    with tempfile.TemporaryDirectory(prefix="nms-mbinc-") as tmp:
        tmpdir = Path(tmp)
        tmp_in = tmpdir / tmp_name
        shutil.copy2(input_path, tmp_in)
        result = subprocess.run([str(mbinc), str(tmp_in)], capture_output=True, text=True, cwd=str(tmpdir), env=helper_env())
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            log(f"MBINCompiler 失败：{detail[:500]}")
            return None
        for suffix in expected_suffixes:
            candidate = tmp_in.with_suffix(suffix)
            if candidate.exists():
                dest = output_dir / candidate.name
                shutil.copy2(candidate, dest)
                return dest
        log("MBINCompiler 执行成功，但没有找到预期输出文件")
        return None


def is_partial_exml(exml_path: Path) -> bool:
    try:
        root = ET.parse(exml_path).getroot()
    except Exception:
        return False
    props = root.findall(".//Property")
    has_id = any(p.get("_id") is not None for p in props)
    total_top_level = len(root.findall("./Property"))
    deep_count = len(props)
    if has_id and deep_count < 50:
        return True
    if not has_id and deep_count < 20 and total_top_level > 0:
        return True
    return False


def merge_exml(original_exml: Path, mod_exml: Path, output_exml: Path, log: Logger) -> bool:
    try:
        orig_tree = ET.parse(original_exml)
        mod_tree = ET.parse(mod_exml)
    except ET.ParseError as exc:
        log(f"EXML 解析失败：{exc}")
        return False

    orig_root = orig_tree.getroot()
    mod_root = mod_tree.getroot()

    def find_by_id(parent, id_value: str):
        for child in parent.iter("Property"):
            if child.get("_id") == id_value:
                return child
        return None

    def find_by_name(parent, name_value: str):
        wanted = name_value.lower()
        for child in parent:
            if child.tag == "Property" and child.get("name", "").lower() == wanted:
                return child
        return None

    def merge_properties(orig_parent, mod_parent) -> None:
        for mod_prop in mod_parent:
            if mod_prop.tag != "Property":
                continue
            mod_id = mod_prop.get("_id")
            mod_name = mod_prop.get("name")
            mod_value = mod_prop.get("value")
            if mod_id:
                target = find_by_id(orig_parent, mod_id)
                if target is not None:
                    for key, value in mod_prop.attrib.items():
                        if key != "_id":
                            target.set(key, value)
                    merge_properties(target, mod_prop)
                    continue
            if mod_name:
                target = find_by_name(orig_parent, mod_name)
                if target is not None:
                    if mod_value is not None:
                        target.set("value", mod_value)
                    if len(mod_prop) > 0:
                        merge_properties(target, mod_prop)
                    continue
                orig_parent.append(mod_prop)

    merge_properties(orig_root, mod_root)
    ET.indent(orig_tree, space="  ")
    orig_tree.write(output_exml, encoding="utf-8", xml_declaration=True)
    return True


def convert_exml_to_mbin(exml_path: Path, target_mbin: Path, log: Logger) -> bool:
    if not find_mbincompiler():
        log("发现 EXML 模组，但没有可用 MBINCompiler；该文件将跳过")
        return False

    with tempfile.TemporaryDirectory(prefix="nms-exml-") as tmp:
        tmpdir = Path(tmp)
        if is_partial_exml(exml_path) and target_mbin.exists():
            log(f"  EXML 片段合并：{exml_path.name}")
            original_exml = run_mbincompiler(target_mbin, tmpdir, log)
            if not original_exml:
                return False
            merged = tmpdir / "merged.MXML"
            if not merge_exml(original_exml, exml_path, merged, log):
                return False
            converted = run_mbincompiler(merged, tmpdir, log)
        else:
            log(f"  EXML 完整编译：{exml_path.name}")
            converted = run_mbincompiler(exml_path, tmpdir, log)

        if not converted or not converted.exists():
            return False
        shutil.copy2(converted, target_mbin)
        return True


def find_case_insensitive(root: Path, internal_path: str) -> Optional[Path]:
    direct = root / internal_path
    if direct.exists():
        return direct
    parts = internal_path.replace("\\", "/").split("/")
    current = root
    for part in parts:
        if not current.is_dir():
            return None
        lower = part.lower()
        match = next((child for child in current.iterdir() if child.name.lower() == lower), None)
        if match is None:
            return None
        current = match
    return current


def restore_active_session(paths: GamePaths, log: Logger) -> bool:
    active_path = paths.state_dir / ACTIVE_SESSION_FILE
    session = load_json(active_path, None)
    if not session:
        return False

    backup_dir = Path(session.get("backup_dir", ""))
    affected = session.get("affected_paks", [])
    log(f"发现上次未恢复的会话，先恢复 {len(affected)} 个 PAK")
    for pak_name in affected:
        backup_pak = backup_dir / pak_name
        target_pak = paths.banks_dir / pak_name
        if backup_pak.exists():
            shutil.copy2(backup_pak, target_pak)
            log(f"  已恢复：{pak_name}")
        else:
            log(f"  警告：缺少备份，无法恢复 {pak_name}")
    active_path.unlink(missing_ok=True)
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    return True


class SessionLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8").strip()
                pid = int(raw) if raw else -1
                os.kill(pid, 0)
            except ProcessLookupError:
                self.path.unlink(missing_ok=True)
            except (ValueError, PermissionError):
                self.path.unlink(missing_ok=True)
            except OSError:
                self.path.unlink(missing_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode("utf-8"))
        except FileExistsError as exc:
            raise LoaderError(f"Loader lock already exists: {self.path}") from exc

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


def create_active_session(paths: GamePaths, pak_names: Sequence[str], log: Logger) -> Path:
    session_id = time.strftime("session-%Y%m%d-%H%M%S")
    backup_dir = paths.backups_dir / session_id
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed_up: List[str] = []
    log("正在备份将被修改的 PAK")
    for pak_name in pak_names:
        src = paths.banks_dir / pak_name
        dst = backup_dir / pak_name
        shutil.copy2(src, dst)
        backed_up.append(pak_name)
        log(f"  备份：{pak_name} ({human_size(src.stat().st_size)})")
    save_json_atomic(
        paths.state_dir / ACTIVE_SESSION_FILE,
        {
            "session_id": session_id,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "game_app": str(paths.game_app),
            "backup_dir": str(backup_dir),
            "affected_paks": backed_up,
        },
    )
    return backup_dir


def patch_one_pak(pak_name: str, matches: Sequence[ModMatch], paths: GamePaths, tool_path: str, log: Logger) -> int:
    pak_path = paths.banks_dir / pak_name
    replaced = 0
    with tempfile.TemporaryDirectory(prefix=f"nms-patch-{pak_name}-") as tmp:
        tmpdir = Path(tmp)
        extract_dir = tmpdir / "extract"
        extract_dir.mkdir()
        log(f"处理 {pak_name}：解包")
        run_hgpaktool(tool_path, ["-U", "-M", str(pak_path), "-O", str(extract_dir)], cwd=extract_dir, log=log)

        manifest = extract_dir / f"{pak_name}.manifest"
        if not manifest.exists():
            manifests = sorted(extract_dir.glob("*.manifest"))
            if manifests:
                manifest = manifests[0]
            else:
                raise LoaderError(f"Manifest not found after extracting {pak_name}")

        by_target: Dict[str, ModMatch] = {}
        for match in matches:
            by_target[normalize_internal(match.internal_path)] = match

        for _target_key, match in by_target.items():
            target = find_case_insensitive(extract_dir, match.internal_path)
            if target is None:
                log(f"  跳过：解包后未找到 {match.internal_path}")
                continue
            before = target.stat().st_size
            if match.needs_convert:
                ok = convert_exml_to_mbin(match.source, target, log)
                if not ok:
                    log(f"  跳过 EXML：{match.mod_name}/{match.source.name}")
                    continue
            else:
                shutil.copy2(match.source, target)
            after = target.stat().st_size
            replaced += 1
            log(f"  替换：{match.internal_path} ({human_size(before)} -> {human_size(after)})")

        if replaced == 0:
            log(f"{pak_name} 没有实际替换文件，跳过重打包")
            return 0

        output_pak = tmpdir / pak_name
        log(f"处理 {pak_name}：LZ4 重打包")
        run_hgpaktool(tool_path, ["-R", "-Z", str(manifest), "-O", str(output_pak)], cwd=extract_dir, log=log)
        if not output_pak.exists():
            raise LoaderError(f"Repacked PAK missing: {output_pak}")
        shutil.copy2(output_pak, pak_path)
        log(f"  已应用：{pak_name} ({human_size(output_pak.stat().st_size)})")
    return replaced


def apply_mod_session(paths: GamePaths, pak_map: Dict[str, List[ModMatch]], tool_path: str, log: Logger) -> int:
    if not pak_map:
        return 0
    pak_names = sorted(pak_map)
    create_active_session(paths, pak_names, log)
    total = 0
    for pak_name in pak_names:
        total += patch_one_pak(pak_name, pak_map[pak_name], paths, tool_path, log)
    if total == 0:
        raise LoaderError("No files were replaced in any PAK")
    log(f"模组应用完成：替换 {total} 个文件")
    return total


def launch_real_game(paths: GamePaths, passthrough_args: Sequence[str], log: Logger) -> int:
    if not os.access(paths.real_exe, os.X_OK):
        raise LoaderError(f"Real game executable is not executable: {paths.real_exe}")
    cmd = [str(paths.real_exe), *passthrough_args]
    log(f"正在启动游戏：{paths.real_exe.name}，参数数量 {len(passthrough_args)}")
    proc = subprocess.Popen(cmd, cwd=str(paths.macos_dir), env=os.environ.copy())
    log(f"游戏进程已启动，PID {proc.pid}")
    code = proc.wait()
    log(f"游戏进程已退出，退出码 {code}")
    return code


def launch_flow(args: argparse.Namespace, passthrough_args: Sequence[str], log: Logger) -> int:
    paths = resolve_game_paths(args.game_app, args.real_exe)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    attach_run_log(log, paths, "loader")
    lock = SessionLock(paths.state_dir / LOCK_FILE)
    lock.acquire()
    try:
        log("模组加载器已启动")
        log(f"Game app: {paths.game_app}")
        log(f"Real executable: {paths.real_exe}")
        log(f"Steam/game 参数：{list(passthrough_args)!r}")
        restore_active_session(paths, log)

        tool = find_hgpaktool()
        if not tool:
            log("未找到 hgpaktool，无法应用模组；将原版启动")
            return launch_real_game(paths, passthrough_args, log)
        log(f"hgpaktool: {tool}")

        if args.force_reindex:
            cache = paths.cache_dir / PAK_INDEX_FILE
            cache.unlink(missing_ok=True)

        pak_map: Dict[str, List[ModMatch]] = {}
        if not args.skip_mods:
            index = build_pak_index(paths, tool, log, force=args.force_reindex)
            pak_map, _unmatched = scan_mods(paths, index, log)
        else:
            log("已按参数跳过模组应用")

        applied = False
        try:
            if pak_map:
                apply_mod_session(paths, pak_map, tool, log)
                applied = True
            else:
                log("没有可应用的模组，将原版启动")
            return launch_real_game(paths, passthrough_args, log)
        except Exception as exc:
            log(f"应用模组失败：{exc}")
            if applied or (paths.state_dir / ACTIVE_SESSION_FILE).exists():
                restore_active_session(paths, log)
            log("为避免阻断 Steam，已恢复后启动原版游戏")
            return launch_real_game(paths, passthrough_args, log)
        finally:
            if (paths.state_dir / ACTIVE_SESSION_FILE).exists():
                log("正在恢复原始 PAK")
                restore_active_session(paths, log)
    finally:
        lock.release()


def scan_command(args: argparse.Namespace, log: Logger) -> int:
    paths = resolve_game_paths(args.game_app, args.real_exe)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    attach_run_log(log, paths, "scan")
    tool = find_hgpaktool()
    if not tool:
        raise LoaderError("hgpaktool not found")
    index = build_pak_index(paths, tool, log, force=args.force_reindex)
    pak_map, unmatched = scan_mods(paths, index, log)
    print()
    print("Matched PAKs:")
    for pak_name, matches in sorted(pak_map.items()):
        print(f"  {pak_name}")
        for match in matches:
            tag = " [EXML->MBIN]" if match.needs_convert else ""
            print(f"    {match.mod_name}: {match.internal_path}{tag}")
    if unmatched:
        print()
        print("Unmatched:")
        for item in unmatched:
            print(f"  {item}")
    return 0


def index_command(args: argparse.Namespace, log: Logger) -> int:
    paths = resolve_game_paths(args.game_app, args.real_exe)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    attach_run_log(log, paths, "index")
    tool = find_hgpaktool()
    if not tool:
        raise LoaderError("hgpaktool not found")
    index = build_pak_index(paths, tool, log, force=args.force_reindex, include_hashes=args.hashes)
    hashed = sum(1 for item in pak_signature(paths.banks_dir, load_json(paths.cache_dir / PAK_INDEX_FILE, {}).get("signature", {})).values() if item.get("sha256"))
    print(f"Indexed {len(index.entries)} files across {len(index.pak_contents)} PAKs.")
    print(f"PAK hashes recorded: {hashed}/{len(index.pak_contents)}")
    return 0


def restore_command(args: argparse.Namespace, log: Logger) -> int:
    paths = resolve_game_paths(args.game_app, args.real_exe)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    attach_run_log(log, paths, "restore")
    restored = restore_active_session(paths, log)
    if not restored:
        log("没有需要恢复的活动会话")
    return 0


def doctor_command(args: argparse.Namespace, log: Logger) -> int:
    paths = resolve_game_paths(args.game_app, args.real_exe)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    attach_run_log(log, paths, "doctor")
    print(f"Game app:   {paths.game_app}")
    print(f"Banks:      {paths.banks_dir} ({human_size(sum(p.stat().st_size for p in paths.banks_dir.glob('*.pak')))})")
    print(f"MODS:       {paths.mods_dir} ({'exists' if paths.mods_dir.exists() else 'missing'})")
    print(f"State:      {paths.state_dir}")
    print(f"Real exe:   {paths.real_exe}")
    print(f"hgpaktool:  {find_hgpaktool() or 'missing'}")
    print(f"MBINCompiler: {find_mbincompiler() or 'missing'}")
    tools_dir = Path(__file__).resolve().parent / "tools"
    if tools_dir.exists():
        dotnet = nms_loader_mbin.probe_dotnet(log)
        print(f".NET host:  {dotnet.get('path') if dotnet.get('ok') else 'broken/missing'}")
        test = nms_loader_mbin.self_test(tools_dir, log)
        print(f"MBIN self-test: {'ok' if test.get('ok') else 'failed'}")
    print(f"Priority file: {paths.state_dir / MOD_PRIORITY_FILE}")
    for index, mod in enumerate(ordered_mod_dirs(paths, persist=False), start=1):
        print(f"  {index}. {mod.name}")
    active = paths.state_dir / ACTIVE_SESSION_FILE
    print(f"Active session: {'yes' if active.exists() else 'no'}")
    return 0


def priority_command(args: argparse.Namespace, log: Logger) -> int:
    paths = resolve_game_paths(args.game_app, args.real_exe)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    attach_run_log(log, paths, "priority")
    current_mods = discover_mod_dirs(paths)
    current_names = [mod.name for mod in current_mods]
    if args.action == "list":
        order = ordered_mod_dirs(paths, log=log)
        if not order:
            print("No mods found.")
            return 0
        print("Apply order; later mods override earlier mods:")
        for index, mod in enumerate(order, start=1):
            print(f"  {index}. {mod.name}")
        return 0
    if args.action == "reset":
        save_mod_order(paths, current_names)
        print("Priority reset to directory name order.")
        return 0
    if args.action == "set":
        requested = list(args.mods)
        if not requested:
            raise LoaderError("priority set requires one or more mod folder names")
        missing = [name for name in requested if name not in current_names]
        if missing:
            raise LoaderError("Unknown mod folder(s): " + ", ".join(missing))
        remainder = [name for name in current_names if name not in requested]
        save_mod_order(paths, [*requested, *remainder])
        print("Priority saved. Apply order; later mods override earlier mods:")
        for index, name in enumerate([*requested, *remainder], start=1):
            print(f"  {index}. {name}")
        return 0
    raise LoaderError(f"Unknown priority action: {args.action}")


def terminal_watch_command(log_path: Path, done_path: Path) -> str:
    log_arg = shlex.quote(str(log_path))
    done_arg = shlex.quote(str(done_path))
    shell = (
        "clear; "
        "printf '\\033]0;NMS Mod Loader\\007'; "
        "printf 'NMS Mod Loader\\nWaiting for loader output...\\n\\n'; "
        f"/usr/bin/tail -n +1 -F {log_arg} & tail_pid=$!; "
        f"while [ ! -f {done_arg} ]; do /bin/sleep 0.2; done; "
        f"/bin/rm -f {done_arg}; "
        "/bin/kill \"$tail_pid\" >/dev/null 2>&1 || true; "
        "wait \"$tail_pid\" >/dev/null 2>&1 || true; "
        "printf '\\nPAK restore complete. Closing this window...\\n'; "
        "/bin/sleep 1"
    )
    return f"/bin/bash -lc {shlex.quote(shell)}"


def terminal_open_script(command: str) -> str:
    return (
        'tell application "Terminal"\n'
        "  activate\n"
        f"  set loaderTab to do script {json.dumps(command)}\n"
        '  set custom title of loaderTab to "NMS Mod Loader"\n'
        "  set loaderId to id of front window\n"
        "end tell\n"
        "try\n"
        '  tell application "System Events"\n'
        '    set frontmost of process "Terminal" to true\n'
        "  end tell\n"
        "end try\n"
        "return loaderId"
    )


def terminal_close_script(window_id: int) -> str:
    return (
        'tell application "Terminal"\n'
        f"  if exists (first window whose id is {window_id}) then\n"
        f"    close (first window whose id is {window_id})\n"
        "  end if\n"
        "end tell"
    )


def open_terminal_window(log_path: Path, done_path: Path) -> Optional[int]:
    if sys.platform != "darwin":
        return None
    command = terminal_watch_command(log_path, done_path)
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", terminal_open_script(command)],
            capture_output=True,
            text=True,
            timeout=10,
            env=helper_env(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"\d+", result.stdout)
    return int(match.group()) if match else None


def close_terminal_window(window_id: Optional[int]) -> None:
    if sys.platform != "darwin" or window_id is None:
        return
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", terminal_close_script(window_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            env=helper_env(),
        )
    except Exception:
        pass


def run_with_terminal(worker: Callable[[Logger], int], state_dir: Path) -> int:
    logs_dir = state_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    token = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    log_path = logs_dir / f"loader-{token}.log"
    done_path = logs_dir / f".loader-{token}.done"
    done_path.unlink(missing_ok=True)
    log_path.touch()
    window_id = open_terminal_window(log_path, done_path)
    log = Logger(log_file=log_path)
    if window_id is None and sys.platform == "darwin":
        log("无法打开 Terminal 日志窗口，继续在后台运行；请查看日志文件")
    try:
        return worker(log)
    except Exception:
        log(traceback.format_exc())
        return 1
    finally:
        log("加载器流程结束，PAK 恢复阶段已完成")
        done_path.touch()
        if window_id is not None:
            time.sleep(1.5)
            close_terminal_window(window_id)


def split_passthrough(argv: Sequence[str]) -> Tuple[List[str], List[str]]:
    if "--" in argv:
        idx = list(argv).index("--")
        return list(argv[:idx]), list(argv[idx + 1 :])
    return list(argv), []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lightweight No Man's Sky macOS mod loader")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--game-app", help="Path to No Man's Sky.app")
        p.add_argument("--real-exe", help="Path to the original game executable")
        p.add_argument("--force-reindex", action="store_true", help="Rebuild the PAK index")

    p_launch = sub.add_parser("launch", help="Patch mods, launch game, restore PAKs after exit")
    add_common(p_launch)
    p_launch.add_argument(
        "--no-terminal",
        "--no-gui",
        dest="no_terminal",
        action="store_true",
        help="Do not open the foreground Terminal log window",
    )
    p_launch.add_argument("--skip-mods", action="store_true", help="Launch without scanning/applying mods")
    p_launch.add_argument("passthrough", nargs=argparse.REMAINDER, help="Arguments after -- are passed to the game")

    p_scan = sub.add_parser("scan", help="Preview matching without applying")
    add_common(p_scan)

    p_index = sub.add_parser("index", help="Build or refresh the cached PAK file tree")
    add_common(p_index)
    p_index.add_argument("--hashes", action="store_true", help="Record SHA256 for PAKs; first run reads every PAK")

    p_restore = sub.add_parser("restore", help="Restore any active unclosed mod session")
    add_common(p_restore)

    p_doctor = sub.add_parser("doctor", help="Check paths and tool availability")
    add_common(p_doctor)

    p_priority = sub.add_parser("priority", help="List or set mod application priority")
    add_common(p_priority)
    p_priority.add_argument("action", choices=("list", "set", "reset"))
    p_priority.add_argument("mods", nargs="*", help="Mod folder names for `set`; later names override earlier names")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "launch":
            passthrough = list(args.passthrough)
            if passthrough and passthrough[0] == "--":
                passthrough = passthrough[1:]
            if args.no_terminal:
                return launch_flow(args, passthrough, Logger())
            paths = resolve_game_paths(args.game_app, args.real_exe)
            return run_with_terminal(lambda log: launch_flow(args, passthrough, log), paths.state_dir)
        if args.command == "scan":
            return scan_command(args, Logger())
        if args.command == "index":
            return index_command(args, Logger())
        if args.command == "restore":
            return restore_command(args, Logger())
        if args.command == "doctor":
            return doctor_command(args, Logger())
        if args.command == "priority":
            return priority_command(args, Logger())
        parser.error("unknown command")
    except LoaderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
