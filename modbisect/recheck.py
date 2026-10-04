# SPDX-License-Identifier: MPL-2.0
"""复测导出层(v0.7): 把嫌疑(+共谋候选)及其依赖导出到干净实例。

职责边界(与 executor 同族的纯文件层):
- 引擎只管状态(choose/report 归因), 本层只管"哪些文件去哪";
- 纯同步, 无 Qt 依赖, 由 UI 包在工作线程中调用;
- 结果如实回报, 不做静默降级。

导出集合 = support(当前嫌疑 ∪ 复测排除史):
- 嫌疑按单元整成员导出(单元 = 强连通 jar 群, 拆开测试无意义);
- 历次复测 absent 的排除嫌疑一起带上 — 后续复测问的是"共谋
  组合是否复现"(max 泛化: 每次收敛都走三选一);
- 依赖链(support 闭包)整体保证 mod 能加载 — 缺依赖会把验证
  信号污染成"mod 根本没加载", 制造假 absent。

config 拷贝(可选, 默认关): 按 modid 前缀匹配源实例 config/ 下
<modid>* 条目(文件与目录都带)。默认不开的原因: config 内的
世界状态/机器缓存可能本身就是问题载体, 拷过去会把"mod 有 bug"
污染成"mod + 旧状态有 bug" — UI 上据此标注"不建议"。

契约:
- 源文件取磁盘现存形态(启/停改名任一), 目标一律落 base_name
  (干净实例里嫌疑必须启用 — 复测的就是它);
- 目标同名直接覆盖(导出重试幂等; 干净实例本应不含这些文件);
- 实例根必须已存在(它是带加载器的真实可玩实例, 程序无法代造)。

路径与拷贝范围不入会话(session v5 设计): 持久化走 AppConfig
的 ui_last_recheck_dir / ui_recheck_include_config 双键。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from .config import AppConfig
from .engine import BisectEngine
from .model import JarInfo
from .scanner import ScanResult


@dataclass
class RecheckReport:
    """一次导出的回执(UI 展示/日志用)。"""
    ok: bool
    export_set: list[str] = field(default_factory=list)     # 计划导出的 jar base 名(排序)
    copied_mods: list[str] = field(default_factory=list)    # 实际拷贝成功的 mod 文件
    copied_configs: list[str] = field(default_factory=list)  # 实际拷贝的 config 条目名
    missing: list[str] = field(default_factory=list)        # 磁盘上找不到的源 jar(测试材料不齐)
    errors: list[str] = field(default_factory=list)         # 错误描述


def _existing_source(jar: JarInfo, cfg: AppConfig) -> str | None:
    """源文件磁盘定位: 启用/禁用两形态取现存者; 双存在/双缺失 = None(异常态)。"""
    en = jar.path(True, cfg.disabled_suffix)
    dis = jar.path(False, cfg.disabled_suffix)
    if os.path.isfile(en) and not os.path.exists(dis):
        return en
    if os.path.isfile(dis) and not os.path.exists(en):
        return dis
    return None


def _config_sibling(mods_dir: str) -> str:
    """标准实例布局: config 与 mods 同级(实例根之下)。"""
    root = os.path.dirname(os.path.abspath(mods_dir))
    return os.path.join(root, "config")


def recheck_export_names(engine: BisectEngine) -> list[str]:
    """导出集合预览(排序 base 名) — UI 弹窗展示"将拷贝哪些 mod(含依赖)"。"""
    seeds = set(engine.suspects) | set(engine.excluded)
    if not seeds:
        return []
    return sorted(engine.graph.support(seeds))


def export_recheck(scan: ScanResult, engine: BisectEngine,
                   instance_root: str, include_config: bool,
                   cfg: AppConfig) -> RecheckReport:
    """把复测集合导出到干净实例(实例根下建/补 mods 与可选 config)。"""
    rep = RecheckReport(ok=False)

    # 0) 实例根必须已存在: 干净实例是带加载器的真实可玩实例,
    #    误填路径时不静默代造半成品目录
    if not os.path.isdir(instance_root):
        rep.errors.append(f"实例根目录不存在: {instance_root}")
        return rep

    # 1) 导出集合 = support(嫌疑 ∪ 复测排除史) — 共谋候选随行
    seeds = set(engine.suspects) | set(engine.excluded)
    if not seeds:
        rep.errors.append("导出集合为空(当前相位无嫌疑, 属调用时机错误)")
        return rep
    export = engine.graph.support(seeds)
    rep.export_set = sorted(export)
    by_base = {j.base_name: j for j in scan.jars}

    # 2) mods 导出: 源取现存形态, 目标落 base_name(干净实例全启用)
    mods_dst = os.path.join(instance_root, "mods")
    try:
        os.makedirs(mods_dst, exist_ok=True)
    except OSError as e:
        rep.errors.append(f"无法创建目标 mods 目录: {e}")
        return rep
    for base in rep.export_set:
        jar = by_base.get(base)
        if jar is None:
            # 引擎域与扫描清单对不齐: 哨兵上报(与 executor 同款防御)
            rep.errors.append(f"导出集合含未知 jar: {base}"
                              "(引擎与扫描数据不一致, 属程序缺陷)")
            continue
        src = _existing_source(jar, cfg)
        if src is None:
            rep.missing.append(base)
            continue
        dst = os.path.join(mods_dst, base)
        try:
            shutil.copy2(src, dst)
            rep.copied_mods.append(base)
        except OSError as e:
            rep.errors.append(f"{base}: 拷贝失败({e})")

    # 3) config 导出(可选): modid 前缀匹配, 文件与目录都带
    if include_config:
        src_cfg = _config_sibling(scan.mods_dir)
        if os.path.isdir(src_cfg):
            _export_config(src_cfg, instance_root, rep, by_base)
        # 源实例没有 config 目录 = 无可拷, 不算错误(如全新整合包)

    rep.ok = not rep.errors and not rep.missing
    return rep


def _export_config(src_cfg: str, instance_root: str,
                   rep: RecheckReport, by_base: dict[str, JarInfo]) -> None:
    """config/ 下 <modid>* 前缀条目(文件+目录)拷到目标实例。

    前缀匹配用列表扫描 + startswith, 不走 glob 通配 — modid
    含通配元字符(*?[, 例如形如 mod[1] 的怪 modid)时前缀语义
    仍精确, 不被通配解释污染。
    """
    dst_cfg = os.path.join(instance_root, "config")
    try:
        os.makedirs(dst_cfg, exist_ok=True)
    except OSError as e:
        rep.errors.append(f"无法创建目标 config 目录: {e}")
        return
    # 导出集合内全部 jar 的 modid(含 jarjar 内嵌 mod 的 modid)
    modids: set[str] = set()
    for base in rep.export_set:
        jar = by_base.get(base)
        if jar is not None:
            modids.update(jar.modids)
    if not modids:
        return
    try:
        entries = os.listdir(src_cfg)
    except OSError as e:
        rep.errors.append(f"无法读取源 config 目录: {e}")
        return
    for name in entries:
        if not any(name.startswith(m) for m in modids):
            continue
        src_p = os.path.join(src_cfg, name)
        dst_p = os.path.join(dst_cfg, name)
        try:
            if os.path.isdir(src_p):
                # 目录(config 子目录也是 mod 状态的一部分)整体拷贝
                shutil.copytree(src_p, dst_p, dirs_exist_ok=True)
            else:
                shutil.copy2(src_p, dst_p)
            rep.copied_configs.append(name)
        except OSError as e:
            rep.errors.append(f"config/{name}: 拷贝失败({e})")