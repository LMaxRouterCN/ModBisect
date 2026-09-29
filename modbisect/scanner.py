# SPDX-License-Identifier: MPL-2.0
"""扫描层: mods 目录非递归枚举 + jar 内元数据解析。

职责边界:
- 只读文件系统(枚举 + zipfile 读取), 不做任何写入/重命名
- 产出 JarInfo 列表, 供依赖图与引擎消费
- 解析失败的 jar 不丢弃: 元数据留空, 仍然可被开关(罪魁可能正是它)

元数据优先级(高→低): neoforge.mods.toml > mods.toml > mcmod.info > unknown
- NeoForge 1.20.5+ 的 neoforge.mods.toml 与 Forge mods.toml 结构同族, 依赖判定双轨:
  新式 type=required / 旧式 mandatory=true, 两者任一命中即视为强制依赖
- side=SERVER 的依赖边丢弃(客户端排查工具用不到)
- jarjar 内嵌 jar 只解析一层; 内嵌 mod 的 modid 计入外层 jar 的提供集,
  其依赖并入外层 jar 依赖集(闭包宁过近似不欠近似, 防二分中途出现缺失依赖硬报错)
- tomllib 解析失败(老 mod 的非规范 TOML) → 降级为行级宽松解析
- 纯同步函数, 线程安全, 由 UI 层在工作线程中调用
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
import zlib
from dataclasses import dataclass, field

import tomllib

from .config import AppConfig
from .model import Dependency, JarInfo, ModInfo

# jar 内元数据候选路径(优先级序; 命中即止, 不叠加)
_TOML_CANDIDATES = (
    "META-INF/neoforge.mods.toml",
    "META-INF/mods.toml",
)
_MCMOD_INFO = "mcmod.info"
# jarjar 内嵌 jar 在 zip 内的存放目录前缀(1.16+ 机制)
_JARJAR_PREFIX = "META-INF/jarjar/"
# side 值中属于服务端的一侧(客户端排查无意义, 丢弃)
_SERVER_SIDE = "SERVER"


@dataclass
class ScanResult:
    """一次扫描的完整产物。"""
    mods_dir: str                     # 规范化后的 mods 目录绝对路径
    jars: list[JarInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def instance_root(self) -> str:
        """实例根目录 = mods 的父目录(latest.log / crash-reports 的所在地)。"""
        return os.path.dirname(self.mods_dir) or self.mods_dir


def scan_mods_dir(mods_dir: str, cfg: AppConfig) -> ScanResult:
    """扫描 mods 目录(不递归), 解析所有顶层 jar 的元数据。"""
    mods_dir = os.path.abspath(mods_dir)
    result = ScanResult(mods_dir=mods_dir)
    if not os.path.isdir(mods_dir):
        result.warnings.append(f"目录不存在: {mods_dir}")
        return result

    # 阶段一: 枚举原始记录 base_name -> (文件名, 启用态, 字节数)
    suffix = cfg.disabled_suffix
    suffix_low = suffix.lower()
    records: dict[str, tuple[str, bool, int]] = {}
    with os.scandir(mods_dir) as it:
        for entry in it:
            if not entry.is_file():
                continue  # 子目录一律不递归(含版本文件夹)
            name = entry.name
            low = name.lower()
            if low.endswith(".jar"):
                base, enabled = name, True
            elif low.endswith(".jar" + suffix_low):
                # 禁用态: 剥掉追加后缀还原出以 .jar 结尾的身份名
                base, enabled = name[: -len(suffix)], False
            else:
                continue  # 与 mod 开关无关的文件(txt/zip/litemod/临时文件等)
            if base in records:
                prev_name, prev_enabled, _ = records[base]
                if enabled and not prev_enabled:
                    # 启用副本是磁盘上的活跃文件, 以它为准
                    records[base] = (name, True, entry.stat().st_size)
                    result.warnings.append(
                        f"{base}: 启用与禁用副本同时存在, 以启用副本为准")
                else:
                    result.warnings.append(
                        f"{base}: 重复的禁用副本 {name}, 已忽略")
            else:
                records[base] = (name, enabled, entry.stat().st_size)

    # 阶段二: 逐 jar 解析元数据(按 base_name 排序保证输出确定性)
    for base in sorted(records):
        filename, enabled, size = records[base]
        jar = JarInfo(directory=mods_dir, base_name=base,
                      enabled=enabled, size=size)
        _parse_jar(jar, cfg, result.warnings)
        result.jars.append(jar)
    return result


# ---------------------------------------------------------------------------
# jar 解析
# ---------------------------------------------------------------------------

def _parse_jar(jar: JarInfo, cfg: AppConfig, warnings: list[str]) -> None:
    """打开 jar(zip) 解析元数据; 任何失败都不抛出, 降级为 unknown 但仍可开关。"""
    path = jar.current_path(cfg.disabled_suffix)
    try:
        with zipfile.ZipFile(path) as zf:
            _parse_toplevel(zf, jar, warnings)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
            OSError, EOFError, ValueError, zlib.error) as e:
        # 损坏 / 加密 / 被独占占用 → 保持 unknown, 开关功能不受影响
        warnings.append(f"{jar.base_name}: 无法读取({type(e).__name__})")


def _parse_toplevel(zf: zipfile.ZipFile, jar: JarInfo,
                    warnings: list[str]) -> None:
    """按优先级解析 jar 顶层元数据, 结果写入 jar.mods / jar.source。"""
    names = set(zf.namelist())

    # 1) mods.toml 系(Forge 1.13+ / NeoForge)
    for candidate in _TOML_CANDIDATES:
        if candidate not in names:
            continue
        text = _decode(zf.read(candidate))
        mods, parse_warnings = _parse_mods_toml(text)
        jar.mods = mods
        jar.source = candidate.rsplit("/", 1)[-1]
        for w in parse_warnings:
            warnings.append(f"{jar.base_name}: {w}")
        # 内嵌 jarjar mod 并入本 jar(同生共死)
        _parse_jarjar(zf, jar, warnings)
        return

    # 2) mcmod.info(1.12.2 及更早; 仅取展示信息, 不产依赖边)
    if _MCMOD_INFO in names:
        try:
            data = json.loads(_decode(zf.read(_MCMOD_INFO)))
        except (json.JSONDecodeError, ValueError):
            return  # 保持 unknown
        jar.mods = _mods_from_mcmod_info(data)
        jar.source = "mcmod.info"
        return

    # 3) 无任何已知元数据(纯库 / coremod / 资源 jar) → source 保持 "unknown"


def _parse_jarjar(zf: zipfile.ZipFile, jar: JarInfo,
                  warnings: list[str]) -> None:
    """解析 jarjar 内嵌 jar(只钻一层), 内嵌 mod 并入外层 jar。

    jarjar 是 1.16+ 机制, 其时代的内嵌 jar 只可能是 mods.toml 系,
    不存在 mcmod.info 内嵌的情况, 故此处不处理 mcmod.info。
    """
    nested = [n for n in zf.namelist()
              if n.startswith(_JARJAR_PREFIX) and n.lower().endswith(".jar")]
    for n in nested:
        try:
            raw = zf.read(n)
            with zipfile.ZipFile(io.BytesIO(raw)) as nzf:
                inner_names = set(nzf.namelist())
                for candidate in _TOML_CANDIDATES:
                    if candidate in inner_names:
                        mods, _ = _parse_mods_toml(
                            _decode(nzf.read(candidate)))
                        for m in mods:
                            m.nested = True  # 标记来源为内嵌
                        jar.mods.extend(mods)
                        break
        except (zipfile.BadZipFile, RuntimeError, OSError,
                EOFError, ValueError, zlib.error):
            warnings.append(f"{jar.base_name}: 内嵌 jar {n} 解析失败(已忽略)")


def _decode(data: bytes) -> str:
    """按 UTF-8(带 BOM 容忍)解码; 失败时替换坏字符(老 mod 的历史编码问题)。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8-sig", errors="replace")


# ---------------------------------------------------------------------------
# mods.toml 解析(规范路径 + 宽松降级)
# ---------------------------------------------------------------------------

def _parse_mods_toml(text: str) -> tuple[list[ModInfo], list[str]]:
    """解析 mods.toml / neoforge.mods.toml 文本。

    返回 (mods, warnings)。规范 TOML 失败 → 行级宽松解析兜底。
    """
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return _parse_mods_toml_lenient(text)

    mod_dicts = [m for m in data.get("mods", []) if isinstance(m, dict)]
    deps_by_parent: dict[str, list[dict]] = {}
    dep_tables = data.get("dependencies", {})
    if isinstance(dep_tables, dict):
        for parent, entries in dep_tables.items():
            if isinstance(entries, dict):
                entries = [entries]  # 畸形: 数组表写成了单表, 容忍
            if isinstance(entries, list):
                clean = [e for e in entries if isinstance(e, dict)]
                if clean:
                    key = str(parent).strip().lower()
                    deps_by_parent.setdefault(key, []).extend(clean)
    return _assemble_mods(mod_dicts, deps_by_parent), []


def _assemble_mods(mod_dicts: list[dict],
                   deps_by_parent: dict[str, list[dict]]) -> list[ModInfo]:
    """把原始 dict 组装为 ModInfo, 并把依赖表挂到对应 mod 上。"""
    mods: list[ModInfo] = []
    for m in mod_dicts:
        modid = str(m.get("modId", "")).strip().lower()
        if not modid:
            continue  # 无 modId 的条目(损坏元数据)跳过
        mods.append(ModInfo(
            modid=modid,
            display_name=str(m.get("displayName", "") or ""),
            version=str(m.get("version", "") or ""),
        ))
    for mod in mods:
        for e in deps_by_parent.get(mod.modid, ()):
            dep = _entry_to_dependency(e)
            if dep is not None:
                mod.dependencies.append(dep)
    return mods


def _entry_to_dependency(e: dict) -> Dependency | None:
    """单条依赖声明 → Dependency; 不参与启停传播的情况返回 None。

    强制判定(双轨, 命中任一即强制):
    - 新式: type = "required"(NeoForge)
    - 旧式: mandatory = true(Forge 1.13~1.16)
    type 与 mandatory 均缺失时保守按强制处理:
    过近似只是多禁几个 jar(浪费轮次), 欠近似会在二分中途制造
    缺失依赖硬报错, 污染"是否复现"信号 —— 宁过勿欠。
    """
    modid = str(e.get("modId", "")).strip().lower()
    if not modid:
        return None
    typ = str(e.get("type", "")).strip().lower()
    if typ == "incompatible":
        return None  # 互斥声明不参与启停传播(MVP 范围外)
    side = str(e.get("side", "BOTH")).strip().upper()
    if side == _SERVER_SIDE:
        return None  # 服务端侧依赖边丢弃
    mandatory = (typ == "required") or _truthy(e.get("mandatory"))
    if not typ and "mandatory" not in e:
        mandatory = True  # 双缺省 → 保守按强制
    return Dependency(modid=modid, mandatory=mandatory)


def _truthy(v) -> bool:
    """宽松布尔: 容忍字符串形态的 true/false(宽松解析路径产出)。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return False


# 宽松解析正则(仅覆盖 [[mods]] / [[dependencies.x]] 常规形态)
_LENIENT_MODS = re.compile(r"^\s*\[\[\s*mods\s*\]\]")
_LENIENT_DEP = re.compile(r"^\s*\[\[\s*dependencies\s*\.\s*([^\]\s]+)\s*\]\]")
_LENIENT_KV_STR = re.compile(
    r"""^\s*([A-Za-z0-9_\-]+)\s*=\s*(['"])(.*?)\2\s*(?:#.*)?$""")
_LENIENT_KV_BOOL = re.compile(
    r"^\s*([A-Za-z0-9_\-]+)\s*=\s*(true|false)\s*(?:#.*)?$")


def _parse_mods_toml_lenient(text: str) -> tuple[list[ModInfo], list[str]]:
    """非规范 TOML 的行级降级解析(老 mod 实际存在这种文件)。"""
    warn = ["mods.toml 非规范 TOML, 已降级为宽松解析(元数据可能不完整)"]
    mod_dicts: list[dict] = []
    deps_by_parent: dict[str, list[dict]] = {}
    current: dict | None = None
    dep_mode: str | None = None  # None=顶层 / "mod" / 父 modid

    for raw in text.splitlines():
        if _LENIENT_MODS.match(raw):
            current = {}
            mod_dicts.append(current)
            dep_mode = "mod"
            continue
        m = _LENIENT_DEP.match(raw)
        if m:
            current = {}
            deps_by_parent.setdefault(m.group(1).lower(), []).append(current)
            dep_mode = m.group(1).lower()
            continue
        if current is None:
            continue  # 顶层键(modLoader/loaderVersion 等)无需关注
        kv = _LENIENT_KV_STR.match(raw) or _LENIENT_KV_BOOL.match(raw)
        if kv:
            current[kv.group(1)] = kv.group(2)  # bool 也按字符串存, 统一走 _truthy

    return _assemble_mods(mod_dicts, deps_by_parent), warn


# ---------------------------------------------------------------------------
# mcmod.info(仅展示信息)
# ---------------------------------------------------------------------------

def _mods_from_mcmod_info(data) -> list[ModInfo]:
    """mcmod.info → ModInfo(无依赖边; 1.12.2 时代的元数据只用于展示)。"""
    if isinstance(data, dict):
        data = data.get("modList", [])
    if not isinstance(data, list):
        return []
    mods: list[ModInfo] = []
    for e in data:
        if not isinstance(e, dict):
            continue
        modid = str(e.get("modid", "")).strip().lower()
        if not modid:
            continue
        mods.append(ModInfo(
            modid=modid,
            display_name=str(e.get("name", "") or ""),
            version=str(e.get("version", "") or ""),
        ))
    return mods