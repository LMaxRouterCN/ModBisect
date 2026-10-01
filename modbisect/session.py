# SPDX-License-Identifier: MPL-2.0
"""会话层: 排查会话的持久化与恢复。

恢复策略 = 重放, 不序列化引擎私有状态:
  会话文件只保存 (a) jar 指纹(base+size) (b) 扫描时刻启停状态 (c) 每轮记录
  (答案 / 实际禁用集 / 是否崩溃)。
  恢复时: 重扫目录 → 指纹比对(全部匹配才有效) → 在初始启用集(W)上重建依赖图
  → 逐轮 replay engine.report()。engine.report 是纯状态转移,
  重放确定性收敛到中断前的同一状态 —— 无需触碰引擎内部,
  且天然自校验: 目录若已变化, 指纹先行拦截, 不会把新目录错接到旧状态上。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

from .config import SESSIONS_DIR, AppConfig
from .depgraph import DependencyGraph
from .engine import Answer, BisectEngine, ScanSpec
from .scanner import ScanResult, scan_mods_dir

SESSION_VERSION = 3  # v0.5.2: history 加 kind/jars(手动冻结显式事件); v1/v2 旧会话仍可恢复


@dataclass
class RestoreResult:
    """恢复结果(ok=False 时只填 reason)。"""
    ok: bool
    reason: str = ""
    scan: ScanResult | None = None
    graph: DependencyGraph | None = None
    engine: BisectEngine | None = None


def _record_item(r) -> dict:
    """RoundRecord → 会话 JSON 项(v0.5.2: kind/jars 显式事件)。"""
    item = {
        "answer": r.answer,
        "disabled": sorted(r.actual_disabled),
        # 崩溃轮 answer 恒为 "invalid"; 显式冗余存 crashed 提高前向兼容性
        "crashed": r.answer == "invalid",
        "kind": r.kind,
    }
    if r.kind != "round":
        # 手动冻结/解冻事件: 附 jar 集, 重放时直接调引擎公共体
        item["jars"] = sorted(r.jars)
    return item


def save_session(scan: ScanResult, engine: BisectEngine) -> str | None:
    """把当前会话落盘; 返回文件路径(失败返回 None)。"""
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        data = {
            "version": SESSION_VERSION,
            "mods_dir": scan.mods_dir,
            "created": datetime.now().isoformat(timespec="seconds"),
            "jars": [
                # enabled 位锚定 engine.universe(会话冻结 W):
                # scan.jars 的启用位是磁盘实时态, 会话中途不等于 W
                {"base": j.base_name, "size": j.size,
                 "enabled": j.base_name in engine.universe}
                for j in scan.jars
            ],
            # v0.5.2: 每条记录带 kind(round/freeze/unfreeze); 手动事件附 jars
            "history": [
                _record_item(r)
                for r in engine.history
            ],
        # v0.4: 卷帘规格(仅卷帘会话存在; 恢复时重建 ScanSpec 再重放,
        # _scan_pos 由重放自身推进, 无需落盘)
        **({
            "scan_spec": {
                "order": list(engine.scan_spec.order),
                "chunk": engine.scan_spec.chunk,
                "enable": engine.scan_spec.enable,
                "from_top": engine.scan_spec.from_top,
            },
        } if engine.scan_spec is not None else {}),
            # 以下为人类可读快照(恢复不依赖它们, 仅列表展示用)
            "phase": engine.phase.value,
            "round": engine.round_index,
        }
        fname = f"session-{datetime.now():%Y%m%d-%H%M%S}.json"
        path = os.path.join(SESSIONS_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return path
    except OSError:
        return None


def list_sessions() -> list[dict]:
    """按文件名倒序(新→旧)列出全部可读会话(含 path/mods_dir/created/rounds)。"""
    out: list[dict] = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    for fn in os.listdir(SESSIONS_DIR):
        if not (fn.startswith("session-") and fn.endswith(".json")):
            continue
        path = os.path.join(SESSIONS_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") not in (1, SESSION_VERSION):
                continue  # 未来版本的会话: 跳过而不是硬解析
            out.append({
                "path": path,
                "mods_dir": data.get("mods_dir", ""),
                "created": data.get("created", ""),
                "rounds": len(data.get("history", [])),
                "phase": data.get("phase", ""),
            })
        except (OSError, json.JSONDecodeError, AttributeError):
            continue  # 损坏会话文件直接跳过
    out.sort(key=lambda d: d["path"], reverse=True)
    return out


def restore_session(path: str, cfg: AppConfig) -> RestoreResult:
    """从会话文件恢复引擎状态(重放策略)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return RestoreResult(ok=False, reason=f"会话文件不可读: {e}")
    if data.get("version") not in (1, 2, SESSION_VERSION):
        return RestoreResult(ok=False, reason="会话版本不兼容")

    # 重扫当前目录(当前启停态可以与会话不同: 中断时正处于二分中间态)
    scan = scan_mods_dir(data.get("mods_dir", ""), cfg)
    if not scan.jars:
        return RestoreResult(ok=False, reason="mods 目录为空或不可访问")

    # 指纹校验: 会话记录的每个 jar 必须仍以相同大小存在(文件更新/删除即失效)
    by_base = {j.base_name: j for j in scan.jars}
    for jd in data.get("jars", []):
        cur = by_base.get(jd.get("base"))
        if cur is None:
            return RestoreResult(
                ok=False, reason=f"会话中的 jar 已不在目录里: {jd.get('base')}")
        if cur.size != jd.get("size"):
            return RestoreResult(
                ok=False,
                reason=f"jar 大小与会话记录不符(可能被更新): {jd.get('base')}")

    # 在初始启用集(W)上重建图与引擎
    initial_enabled = [jd["base"] for jd in data.get("jars", []) if jd.get("enabled")]
    if not initial_enabled:
        return RestoreResult(ok=False, reason="会话初始启用集为空")
    w_jars = [by_base[b] for b in initial_enabled]
    graph = DependencyGraph(w_jars, cfg.ignore_modids)
    # v0.4: 卷帘规格重建(v2 会话); v1 无此字段 → 纯二分重放
    sd = data.get("scan_spec")
    spec = None
    if sd:
        spec = ScanSpec(order=tuple(sd.get("order", ())),
                        chunk=int(sd.get("chunk", 1)),
                        enable=bool(sd.get("enable", False)),
                        from_top=bool(sd.get("from_top", True)))
    engine = BisectEngine(graph, spec)

    # 重放历史: 纯状态转移 + 显式事件, 确定性重建中断前状态
    for rec in data.get("history", []):
        # v0.5.2: 手动冻结/解冻 = 自由意志显式事件, 不可由 report 推导,
        # 直接调引擎公共体重放(记账 _frozen_was_suspect 随之确定式重建)
        kind = rec.get("kind", "round")
        if kind in ("freeze", "unfreeze"):
            jars = frozenset(rec.get("jars", []))
            if kind == "freeze":
                engine.freeze(jars)
            else:
                engine.unfreeze(jars)
            continue
        crashed = bool(rec.get("crashed")) or rec.get("answer") == "invalid"
        # 崩溃轮的 answer 字段值无意义("invalid" 非 Answer 成员), 填什么都被 crashed 分支忽略
        answer = Answer.PRESENT if crashed else Answer(rec.get("answer", ""))
        engine.report(answer, frozenset(rec.get("disabled", [])), crashed)

    return RestoreResult(ok=True, scan=scan, graph=graph, engine=engine)