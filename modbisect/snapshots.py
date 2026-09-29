# SPDX-License-Identifier: MPL-2.0
"""快照层: mod 启停状态全集的存档与读取(与排查会话互补)。

会话(sessions/)保存的是二分进度; 快照(snapshots/)保存的是
"某时刻全部 mod 的启停状态" — 自由开关之后一键回到已知状态。

设计:
- 快照内容 = 全部 jar 的 (base_name, enabled, size):
  enabled 是恢复目标, size 是指纹(目录内容变化时拦截状态错配)
- 恢复执行不在此层: 目标启用集交给 executor.apply 幂等 diff
  (与排查启停共用同一条物理执行路径, 单一执行机构)
- 纯文件 IO, 无 Qt 依赖, 由 UI 层在工作线程中调用
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from .config import PROGRAM_ROOT
from .scanner import ScanResult

SNAPSHOT_VERSION = 1
SNAPSHOTS_DIR = os.path.join(PROGRAM_ROOT, "snapshots")


def create_snapshot(scan: ScanResult) -> str | None:
    """把当前全部 jar 启停状态落盘; 返回文件路径(失败 None)。"""
    try:
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        data = {
            "version": SNAPSHOT_VERSION,
            "created": datetime.now().isoformat(timespec="seconds"),
            "mods_dir": scan.mods_dir,
            "jars": [{"base": j.base_name, "enabled": j.enabled,
                      "size": j.size} for j in scan.jars],
        }
        fname = f"snapshot-{datetime.now():%Y%m%d-%H%M%S}.json"
        path = os.path.join(SNAPSHOTS_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return path
    except OSError:
        return None


def list_snapshots() -> list[dict]:
    """按文件名倒序(新→旧)列出全部可读快照(path/created/mods_dir/count)。"""
    out: list[dict] = []
    if not os.path.isdir(SNAPSHOTS_DIR):
        return out
    for fn in os.listdir(SNAPSHOTS_DIR):
        if not (fn.startswith("snapshot-") and fn.endswith(".json")):
            continue
        path = os.path.join(SNAPSHOTS_DIR, fn)
        data = load_snapshot(path)
        if data is None:
            continue  # 损坏/版本不符: 跳过
        out.append({
            "path": path,
            "created": data.get("created", ""),
            "mods_dir": data.get("mods_dir", ""),
            "count": len(data.get("jars", [])),
        })
    out.sort(key=lambda d: d["path"], reverse=True)
    return out


def load_snapshot(path: str) -> dict | None:
    """读单个快照; 版本不符/损坏返回 None(调用方跳过或提示)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != SNAPSHOT_VERSION:
            return None
        return data
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def snapshot_check(data: dict, scan: ScanResult) -> list[str]:
    """快照与当前扫描的一致性检查; 返回警告清单(空 = 完全匹配)。

    - 目录不同: 快照可能属于另一个实例(是否仍恢复由用户决定)
    - size 不符 / 缺失: 目录内容已变化, 指纹拦截防错配
    - 快照后新增: 恢复语义 = 回到快照态, 新增 mod 将被禁用(如实告知)
    """
    warns: list[str] = []
    if data.get("mods_dir") and data["mods_dir"] != scan.mods_dir:
        warns.append(f"快照属于目录 {data['mods_dir']}, 当前目录是 {scan.mods_dir}")
    by_base = {j.base_name: j for j in scan.jars}
    for jd in data.get("jars", []):
        base = jd.get("base")
        cur = by_base.get(base)
        if cur is None:
            warns.append(f"快照中的 {base} 已不在当前目录(该 mod 无法恢复)")
        elif cur.size != jd.get("size"):
            warns.append(f"{base} 大小与快照记录不符(文件可能已被更新)")
    snap_bases = {jd.get("base") for jd in data.get("jars", [])}
    extra = [j.base_name for j in scan.jars if j.base_name not in snap_bases]
    if extra:
        head = ", ".join(extra[:5]) + ("…" if len(extra) > 5 else "")
        warns.append(f"快照后新增 {len(extra)} 个 mod(恢复时将被禁用): {head}")
    return warns


def snapshot_target(data: dict, scan: ScanResult) -> frozenset[str]:
    """恢复目标启用集 = 快照中 enabled 且当前仍存在的 base 集。

    只含已知 jar(executor.apply 要求目标集 ⊆ 已扫描域)。
    """
    known = {j.base_name for j in scan.jars}
    return frozenset(
        jd.get("base") for jd in data.get("jars", [])
        if jd.get("enabled") and jd.get("base") in known)