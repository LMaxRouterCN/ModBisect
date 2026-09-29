# SPDX-License-Identifier: MPL-2.0
"""修补层: 缺失依赖的"文件名反查 + mods.toml 修正"。

场景: 某声明的依赖 modid 在目录中无人提供, 常见根因是提供者 jar 的
mods.toml 里 modId 与依赖声明不一致(作者笔误/改名后遗症)。
本层用文件名做人类常识级匹配, 用户确认后改写提供者 jar 的 modId,
使依赖边重新接通。

设计边界:
- 匹配只产出"提议", 决定权在用户(RepairDialog 确认)
- 改写 = 重写整个 zip; 原件备份为 *.orig(已存在则不覆盖 = 永远是最早原件)
- 多 [[mods]] 块 / 无 mods.toml 的 jar 不改写(结构复杂, 仅展示)
- modId 行只在第一个 [[mods]] 块内定位, 避免误伤
  [[dependencies.x]] 表里同名的 modId 行
- 纯文件 IO, 无 Qt; 由 UI 层在工作线程中调用
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile

from .scanner import ScanResult

# 文件名 → 匹配键: 去非 ASCII 字母数字(中文/符号/空格全去)再小写
_STRIP = re.compile(r"[^0-9a-z]")
# 与 scanner 同优先级的元数据候选
_TOML_CANDIDATES = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
_MODID_LINE = re.compile(r'(?m)^(\s*modId\s*=\s*["\'])([^"\']+)(["\'])')
_MODS_BLOCK = re.compile(r'(?m)^\s*\[\[\s*mods\s*\]\]')
_NEXT_TABLE = re.compile(r'(?m)^\s*\[\[')
_DEP_TABLE = re.compile(r'(?m)^(\s*\[\[\s*dependencies\s*\.\s*)([^\]\s]+)(\s*\]\])')


def normalize_stem(base_name: str) -> str:
    """文件名 → 匹配键: 去 .jar 尾巴, 去中文/符号, 小写。"""
    stem = base_name[:-4] if base_name.endswith(".jar") else base_name
    return _STRIP.sub("", stem.casefold())


def find_candidates(missing_modid: str, scan: ScanResult,
                    declaring_base: str) -> tuple[list[str], str]:
    """为缺失 modid 找"文件名像它"的可修补候选 jar。

    两层匹配(精确层空则前缀层):
    - exact: 规范化文件名 == 规范化 modid
    - prefix: 规范化文件名以规范化 modid 开头(兼容 "modid-1.20.1" 命名)
    候选限定: 有 mods.toml 系元数据且恰含一个 mod(可安全改写);
    声明依赖的 jar 自身排除(自引用不是修补)。
    返回 (候选 base 列表, 命中层 ""/"exact"/"prefix")。
    """
    want = _STRIP.sub("", missing_modid.casefold())
    if not want:
        return [], ""
    patchable = [j for j in scan.jars
                 if j.base_name != declaring_base
                 and len(j.mods) == 1
                 and j.source in ("mods.toml", "neoforge.mods.toml")]
    exact = [j.base_name for j in patchable
             if normalize_stem(j.base_name) == want]
    if exact:
        return exact, "exact"
    prefix = [j.base_name for j in patchable
              if normalize_stem(j.base_name).startswith(want)]
    return prefix, ("prefix" if prefix else "")


def patch_modid(jar_path: str, old_modid: str, new_modid: str) -> str | None:
    """把 jar 内唯一 mod 的 modId(old→new)及同名依赖表键改写, 重写整个 zip。

    返回 None = 成功; 返回字符串 = 失败原因(原件不动)。
    幂等: old == new 直接成功(零改写)。
    """
    if old_modid == new_modid:
        return None
    tmp = jar_path + ".patching"
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = zf.namelist()
            entry = next((c for c in _TOML_CANDIDATES if c in names), None)
            if entry is None:
                return "jar 内没有 mods.toml 系元数据"
            text = zf.read(entry).decode("utf-8-sig", errors="replace")
            if len(_MODS_BLOCK.findall(text)) > 1:
                return "mods.toml 含多个 mods 块, 不自动改写"
            hit = _find_mods_block_modid(text)
            if hit is None:
                return "第一个 mods 块内没找到 modId 行"
            start, end, current = hit
            cur = current.strip().casefold()
            if cur == new_modid.casefold():
                return None  # 已是目标(重复修补幂等通过)
            if cur != old_modid.casefold():
                # 文件实际 modId 与扫描记录不符(外部已改动), 不盲目写
                return f"modId 实际是 {current}, 与预期的 {old_modid} 不符, 建议重扫"
            new_text = (text[:start] + _MODID_LINE.search(text[start:end]).group(1)
                        + new_modid
                        + _MODID_LINE.search(text[start:end]).group(3) + text[end:])
            # 依赖表键 [[dependencies.旧id]] 同步改名(键挂在旧 id 下会失联)
            new_text = _DEP_TABLE.sub(_rekey_dep(new_modid, old_modid), new_text)
            backup = jar_path + ".orig"
            if not os.path.exists(backup):
                shutil.copy2(jar_path, backup)  # 首次备份 = 真原件, 永不覆盖
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zf.infolist():
                    data = zf.read(item.filename)
                    if item.filename == entry:
                        data = new_text.encode("utf-8")
                    zout.writestr(item, data)
        os.replace(tmp, jar_path)  # 原子替换
        return None
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        return f"改写失败: {type(e).__name__}: {e}"


def _find_mods_block_modid(text: str) -> tuple[int, int, str] | None:
    """定位第一个 mods 块内的 modId 行; 返回 (match起点, match终点, 当前值)。

    只在块内(到下一个表头为止)搜索 — 依赖表里也有 modId= 行, 不能误伤。
    """
    mb = _MODS_BLOCK.search(text)
    if mb is None:
        return None
    rest = text[mb.end():]
    nxt = _NEXT_TABLE.search(rest)
    region = rest[:nxt.start()] if nxt else rest
    m = _MODID_LINE.search(region)
    if m is None:
        return None
    return (mb.end() + m.start(), mb.end() + m.end(), m.group(2))


def _rekey_dep(new_modid: str, old_modid: str):
    """依赖表键替换函数: 仅命中旧 modid 键(其他 mod 的依赖表不动)。"""
    def _sub(m):
        key = m.group(2)
        keep = key if key.strip().casefold() != old_modid.casefold() else new_modid
        return m.group(1) + keep + m.group(3)
    return _sub