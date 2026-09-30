# SPDX-License-Identifier: MPL-2.0
"""纯模块测试(无第三方依赖, 普通脚本而非 pytest)。

运行: python tests/test_core.py   (退出码 0 = 全部通过)
覆盖: scanner → depgraph → engine → executor → session 全链路。
watcher / processmon 依赖 PySide6/psutil/watchdog, 由冒烟测试另测, 不在此文件。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import zipfile

# 直接 python tests/test_core.py 运行时 sys.path[0]=tests/, 需补项目根
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modbisect.config import AppConfig
from modbisect.scanner import scan_mods_dir
from modbisect.depgraph import DependencyGraph
from modbisect.engine import (Answer, Action, Phase, BisectEngine,
                               ScanSpec)
from modbisect.executor import Executor
from modbisect import session as session_mod

_PASS = 0
_FAIL = 0


def check(name: str, cond, detail: str = "") -> None:
    """单条断言(带计数, 汇总决定退出码)。"""
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  [PASS] {name}")
    else:
        _FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 造假 jar

def make_jar(path: str, toml_text: str | None = None,
             mcmod_json: str | None = None) -> None:
    """生成测试 jar(zip); 无任何元数据时写占位条目保证 zip 合法。"""
    with zipfile.ZipFile(path, "w") as zf:
        if toml_text is not None:
            zf.writestr("META-INF/mods.toml", toml_text)
        if mcmod_json is not None:
            zf.writestr("mcmod.info", mcmod_json)
        if toml_text is None and mcmod_json is None:
            zf.writestr("placeholder.txt", "x")  # e.jar: unknown 来源


# 场景一: scanner 元数据解析(新旧双轨 / optional / SERVER 边 / mcmod.info)
TOML_A = """modLoader="javafml"
loaderVersion="[47,)"
[[mods]]
modId="moda"
version="1.0"
displayName="Mod A"
[[dependencies.moda]]
    modId="modb"
    type="required"
[[dependencies.moda]]
    modId="modc"
    type="optional"
[[dependencies.moda]]
    modId="modsrv"
    type="required"
    side="SERVER"
"""
TOML_B = """modLoader="javafml"
[[mods]]
modId="modb"
version="1.0"
displayName="Mod B"
"""
TOML_C = """modLoader="javafml"
[[mods]]
modId="modc"
version="1.0"
"""
TOML_G = """modLoader="javafml"
[[mods]]
modId="modg"
version="1.0"
[[dependencies.modg]]
    modId="moda"
    mandatory=true
    versionRange="[1,)"
"""
MCMOD_D = json.dumps([{"modid": "modd", "name": "Mod D", "version": "2.0"}])

# 场景二: 依赖图/引擎/执行器共用图
# x↔y 互为唯一强制依赖(SCC 绑定) / z 独立 / w 依赖 modq(无人提供,基线剔除)
# / p 初始禁用(出局, 不进 W)
TOML_X = """modLoader="javafml"
[[mods]]
modId="modx"
version="1.0"
[[dependencies.modx]]
    modId="mody"
    type="required"
"""
TOML_Y = """modLoader="javafml"
[[mods]]
modId="mody"
version="1.0"
[[dependencies.mody]]
    modId="modx"
    type="required"
"""
TOML_Z = """modLoader="javafml"
[[mods]]
modId="modz"
version="1.0"
"""
TOML_W = """modLoader="javafml"
[[mods]]
modId="modw"
version="1.0"
[[dependencies.modw]]
    modId="modq"
    type="required"
"""
TOML_P = """modLoader="javafml"
[[mods]]
modId="modp"
version="1.0"
"""


# ---------------------------------------------------------------- scanner

def test_scanner(tmp: str) -> None:
    print("[scanner]")
    mods = os.path.join(tmp, "mods1")
    os.makedirs(mods)
    make_jar(os.path.join(mods, "a.jar"), toml_text=TOML_A)
    make_jar(os.path.join(mods, "b.jar"), toml_text=TOML_B)
    make_jar(os.path.join(mods, "c.jar"), toml_text=TOML_C)
    os.rename(os.path.join(mods, "c.jar"),
              os.path.join(mods, "c.jar.disabled"))
    make_jar(os.path.join(mods, "d.jar"), mcmod_json=MCMOD_D)
    make_jar(os.path.join(mods, "e.jar"))
    make_jar(os.path.join(mods, "g.jar"), toml_text=TOML_G)
    with open(os.path.join(mods, "f.txt"), "w") as f:
        f.write("not a mod")

    cfg = AppConfig(disabled_suffix=".disabled")
    res = scan_mods_dir(mods, cfg)
    by = {j.base_name: j for j in res.jars}
    check("数量=6(忽略非jar)", len(res.jars) == 6,
          str([j.base_name for j in res.jars]))
    a = by.get("a.jar")
    check("a.source=mods.toml", a is not None and a.source == "mods.toml")
    check("a.modid/modname", a is not None and a.mods
          and a.mods[0].modid == "moda"
          and a.mods[0].display_name == "Mod A")
    deps = {d.modid: d.mandatory for d in a.mods[0].dependencies}
    check("新式required", deps.get("modb") is True)
    check("optional不强制", deps.get("modc") is False)
    check("SERVER边丢弃", "modsrv" not in deps, str(deps))
    c = by.get("c.jar")
    check("c初始禁用态", c is not None and c.enabled is False)
    d = by.get("d.jar")
    check("d.mcmod.info", d is not None and d.source == "mcmod.info"
          and d.mods and d.mods[0].modid == "modd"
          and d.mods[0].version == "2.0")
    e = by.get("e.jar")
    check("e.unknown", e is not None and e.source == "unknown")
    check("e.label退回文件名", e is not None and e.label == "e")
    g = by.get("g.jar")
    gdeps = {d.modid: d.mandatory for d in g.mods[0].dependencies}
    check("旧式mandatory", gdeps.get("moda") is True, str(gdeps))
    check("instance_root=父目录",
          os.path.normcase(res.instance_root) == os.path.normcase(tmp))


# ---------------------------------------------------------------- 场景二搭建

def build_graph_scene(tmp: str, cfg: AppConfig):
    """搭 x↔y/z/w/p 场景, 返回 (scan结果, W上的依赖图)。"""
    mods = os.path.join(tmp, "mods2")
    os.makedirs(mods)
    for name, toml in [("x.jar", TOML_X), ("y.jar", TOML_Y),
                       ("z.jar", TOML_Z), ("w.jar", TOML_W)]:
        make_jar(os.path.join(mods, name), toml_text=toml)
    make_jar(os.path.join(mods, "p.jar"), toml_text=TOML_P)
    os.rename(os.path.join(mods, "p.jar"),
              os.path.join(mods, "p.jar.disabled"))
    res = scan_mods_dir(mods, cfg)
    w_jars = [j for j in res.jars if j.enabled]  # W = 初始启用集
    graph = DependencyGraph(w_jars, cfg.ignore_modids)
    return res, graph


# ---------------------------------------------------------------- depgraph

def test_depgraph(graph: DependencyGraph, W: set) -> None:
    print("[depgraph]")
    check("universe=W", set(graph.universe) == W)
    check("x依赖mody", set(graph.jar_deps["x.jar"]) == {"mody"})
    check("y依赖modx", set(graph.jar_deps["y.jar"]) == {"modx"})
    check("z无依赖", set(graph.jar_deps["z.jar"]) == set())
    check("基线缺失边剔除", set(graph.jar_deps["w.jar"]) == set())
    check("剔除有警告", any("modq" in w for w in graph.warnings))
    check("单元数=3", len(graph.units) == 3,
          str([set(u) for u in graph.units]))
    check("x/y同单元(SCC)", set(graph.unit_members("x.jar")) == {"x.jar", "y.jar"})
    check("closure({y})拖死x", set(graph.closure({"y.jar"})) == {"x.jar", "y.jar"})
    check("closure({z})", set(graph.closure({"z.jar"})) == {"z.jar"})
    check("closure({x,y})", set(graph.closure({"x.jar", "y.jar"})) == {"x.jar", "y.jar"})
    check("closure(空)", set(graph.closure(set())) == set())
    check("support({x})={x,y}", set(graph.support({"x.jar"})) == {"x.jar", "y.jar"})
    check("support({z})", set(graph.support({"z.jar"})) == {"z.jar"})
    check("providers_of", set(graph.providers_of("modx")) == {"x.jar"})


# ---------------------------------------------------------------- engine

def test_engine(graph: DependencyGraph, W: set) -> None:
    print("[engine: 全流程收敛]")
    eng = BisectEngine(graph)
    p0 = eng.current_plan
    check("初始BASELINE", p0.phase == Phase.BASELINE)
    check("基准目标=初始状态", set(p0.target_enabled) == W)
    act = eng.report(Answer.PRESENT, frozenset())
    check("基准PRESENT→NEXT", act == Action.NEXT_PLAN)

    p1 = eng.current_plan
    check("进入BISECT", p1.phase == Phase.BISECT and p1.index == 1)
    # 同构手算期望: 按单元 min 排序均分, 禁后半(引擎规则镜像)
    ordered = sorted(range(len(graph.units)),
                     key=lambda i: min(graph.units[i]))
    mid = len(ordered) // 2
    expected_prop: set = set()
    for i in ordered[mid:]:
        expected_prop |= graph.units[i]
    check("提议=后半单元", set(p1.proposed_disabled) == expected_prop)
    expected_eff = graph.closure(expected_prop, W)
    check("计划目标=W-闭包", set(p1.target_enabled) == (W - expected_eff))
    # 执行层回报契约: actual = W - 实际目标启用集
    actual = frozenset(W - set(p1.target_enabled))
    act = eng.report(Answer.PRESENT, actual)
    check("一轮后进VERIFY", act == Action.NEXT_PLAN
          and eng.phase == Phase.VERIFY)
    p2 = eng.current_plan
    check("验证目标=嫌疑+支撑", set(p2.target_enabled)
          == set(graph.support({"w.jar"})))
    act = eng.report(Answer.PRESENT, frozenset(W - set(p2.target_enabled)))
    check("验证复现→DONE", act == Action.DONE and eng.phase == Phase.DONE)
    check("确诊罪魁=w", eng.verdict is not None
          and set(eng.verdict.culprit) == {"w.jar"})


def test_engine_branches(graph: DependencyGraph, W: set) -> None:
    print("[engine: 分支与保护]")
    # 基准 ABSENT → 前提动摇结案
    e1 = BisectEngine(graph)
    a = e1.report(Answer.ABSENT, frozenset())
    check("基准ABSENT→DONE", a == Action.DONE and e1.verdict is not None
          and "未复现" in e1.verdict.title)
    # 基准 SKIP → 信任前提直接二分
    e2 = BisectEngine(graph)
    a = e2.report(Answer.SKIP, frozenset())
    check("基准SKIP→BISECT", a == Action.NEXT_PLAN
          and e2.phase == Phase.BISECT)
    # 崩溃轮: 信号作废重测, 状态不推进
    e3 = BisectEngine(graph)
    a = e3.report(Answer.PRESENT, frozenset(), crashed=True)
    check("崩溃→RETEST", a == Action.RETEST_SAME
          and e3.phase == Phase.BASELINE)
    check("崩溃轮归档invalid", e3.history
          and e3.history[-1].answer == "invalid")
    # 空集保护: 二分 PRESENT 但实际禁用=全集 → 信号矛盾
    e4 = BisectEngine(graph)
    e4.report(Answer.PRESENT, frozenset())
    a = e4.report(Answer.PRESENT, frozenset(W))
    check("归算空→信号矛盾", a == Action.DONE and e4.verdict is not None
          and "矛盾" in e4.verdict.title)
    # 退化保护: 二分 ABSENT 且实际禁用=全集(闭包吞掉分割) → 转人工
    e5 = BisectEngine(graph)
    e5.report(Answer.PRESENT, frozenset())
    a = e5.report(Answer.ABSENT, frozenset(W))
    check("归算未缩小→退化", a == Action.DONE and e5.verdict is not None
          and "退化" in e5.verdict.title)
    # 验证轮不复现 → 交互问题结案
    e6 = BisectEngine(graph)
    e6.report(Answer.PRESENT, frozenset())
    p = e6.current_plan
    e6.report(Answer.PRESENT, frozenset(W - set(p.target_enabled)))
    a = e6.report(Answer.ABSENT, frozenset())
    check("验证不复现→交互", a == Action.DONE
          and e6.verdict is not None and "交互" in e6.verdict.title)


# ---------------------------------------------------------------- executor

def test_executor(res, cfg: AppConfig) -> None:
    print("[executor]")
    ex = Executor(res.jars, cfg)
    mods = res.mods_dir
    sfx = cfg.disabled_suffix
    # 1) diff: 只动状态变化的 jar
    r = ex.apply(frozenset({"x.jar", "y.jar"}))
    check("apply ok", r.ok, str(r.errors))
    check("禁用z+w", set(r.renamed) == {"z.jar", "w.jar"})
    check("z磁盘已禁", not os.path.exists(os.path.join(mods, "z.jar"))
          and os.path.exists(os.path.join(mods, "z.jar" + sfx)))
    check("p保持禁用(不在W)", not os.path.exists(os.path.join(mods, "p.jar")))
    check("回读actual={z,w}", set(r.actual_disabled) == {"z.jar", "w.jar"})
    # 2) 幂等: 同目标二次 apply 零改名
    r2 = ex.apply(frozenset({"x.jar", "y.jar"}))
    check("幂等零改名", r2.ok and len(r2.renamed) == 0, str(r2.renamed))
    # 3) 篡改检测: 外部改名后拒绝执行
    os.rename(os.path.join(mods, "x.jar"),
              os.path.join(mods, "hacked.jar"))
    r3 = ex.apply(frozenset({"x.jar", "y.jar"}))
    check("篡改被拒", (not r3.ok) and "x.jar" in r3.tampered)
    os.rename(os.path.join(mods, "hacked.jar"),
              os.path.join(mods, "x.jar"))
    # 4) 复原后正常
    r4 = ex.apply(frozenset({"x.jar", "y.jar"}))
    check("复原后ok", r4.ok, str(r4.errors))
    # 5) 还原初始状态
    r5 = ex.restore_initial()
    check("还原ok", r5.ok, str(r5.errors))
    # 还原语义 = 回到扫描时刻快照: W 内全部启用, 初始禁用者保持禁用
    w_ok = all(os.path.exists(os.path.join(mods, b))
               for b in ("x.jar", "y.jar", "z.jar", "w.jar"))
    check("还原=W全启+p保持禁",
          w_ok and os.path.exists(os.path.join(mods, "p.jar" + sfx)))
    check("还原actual空", set(r5.actual_disabled) == set())


# ---------------------------------------------------------------- session

def test_session(tmp: str, cfg: AppConfig) -> None:
    print("[session]")
    mods = os.path.join(tmp, "mods2")
    res = scan_mods_dir(mods, cfg)  # executor 已还原, 全启用
    w_jars = [j for j in res.jars if j.enabled]
    W = {j.base_name for j in w_jars}
    graph = DependencyGraph(w_jars, cfg.ignore_modids)
    eng = BisectEngine(graph)
    eng.report(Answer.PRESENT, frozenset())
    p = eng.current_plan
    eng.report(Answer.PRESENT, frozenset(W - set(p.target_enabled)))
    # 此刻 VERIFY 阶段, 历史两轮 —— 覆盖"中断恢复"场景

    # 测试内重定向持久化目录, 不污染真实 sessions/
    session_mod.SESSIONS_DIR = os.path.join(tmp, "sessions-test")
    path = session_mod.save_session(res, eng)
    check("保存成功", path is not None and os.path.isfile(path))
    lst = session_mod.list_sessions()
    check("列表可见", any(item["path"] == path for item in lst))
    rr = session_mod.restore_session(path, cfg)
    check("恢复ok", rr.ok, rr.reason)
    if rr.ok:
        check("恢复phase", rr.engine.phase == eng.phase)
        check("恢复轮次", rr.engine.round_index == eng.round_index)
        check("恢复嫌疑数", rr.engine.suspect_count == eng.suspect_count)
        check("恢复嫌疑集", set(rr.engine.suspects) == set(eng.suspects))
        check("恢复历史长度", len(rr.engine.history) == len(eng.history))
        check("恢复universe=W", set(rr.engine.universe) == W)

    # 指纹破坏: size 不符必须拒绝恢复
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["jars"][0]["size"] = data["jars"][0].get("size", 0) + 1
    broken = os.path.join(tmp, "broken.json")
    with open(broken, "w", encoding="utf-8") as f:
        json.dump(data, f)
    rr2 = session_mod.restore_session(broken, cfg)
    check("指纹不符拒绝", (not rr2.ok) and "大小" in rr2.reason, rr2.reason)
    # jar 缺失: 引用不存在的 base 必须拒绝
    data["jars"][0]["base"] = "ghost.jar"
    broken2 = os.path.join(tmp, "broken2.json")
    with open(broken2, "w", encoding="utf-8") as f:
        json.dump(data, f)
    rr3 = session_mod.restore_session(broken2, cfg)
    check("缺失jar拒绝", (not rr3.ok) and "不在" in rr3.reason, rr3.reason)


# ---------------------------------------------------------------- v0.2 新增

def test_sortkey() -> None:
    """v0.2: 拼音排序键与版本分段键(sortkey.py)。"""
    print("[sortkey]")
    from modbisect.sortkey import name_key, version_key
    check("拼音键中英统一", name_key("EMI Loot") == "emiloot")
    check("拼音键大小写不敏感", name_key("Just Enough Items")
          == name_key("just enough items"))
    check("拼音键滤空格符号", name_key("能源装置 Energy")
          == name_key("能源装置Energy"))
    check("拼音键纯中文", name_key("能源") == "nengyuan")
    check("空名键", name_key("  ") == "")
    check("版本数值分段", version_key("1.10.2") > version_key("1.9.4"))
    check("版本跨位", version_key("1.20.1-forge") < version_key("1.20.2"))
    check("版本缺位", version_key("2.0") < version_key("2.0.1"))
    check("空版本", version_key("") == ())
    check("版本数字后缀", version_key("1.0a") < version_key("1.0b")
          < version_key("2"))


def test_depgraph_missing(graph: DependencyGraph) -> None:
    """v0.2: depgraph.missing 结构化清单(修补系统的输入)。"""
    print("[depgraph.missing]")
    check("missing结构化", graph.missing == [("w.jar", "modq")],
          str(graph.missing))
    check("missing与警告同源",
          any("modq" in w and "w.jar" in w for w in graph.warnings))


def test_snapshots(tmp: str, cfg: AppConfig) -> None:
    """v0.2: 快照建/列/读/一致性检查/恢复目标(snapshots.py)。"""
    print("[snapshots]")
    from modbisect import snapshots as snap_mod
    from modbisect.model import JarInfo
    from modbisect.scanner import ScanResult
    res = scan_mods_dir(os.path.join(tmp, "mods2"), cfg)  # executor 已还原
    snap_mod.SNAPSHOTS_DIR = os.path.join(tmp, "snapshots-test")  # 重定向
    path = snap_mod.create_snapshot(res)
    check("快照落盘", path is not None and os.path.isfile(path))
    lst = snap_mod.list_snapshots()
    check("快照列表", len(lst) == 1 and lst[0]["count"] == 5, str(lst))
    data = snap_mod.load_snapshot(path)
    check("快照读取", data is not None
          and data["mods_dir"] == os.path.abspath(res.mods_dir))
    # 状态翻转后目标集仍取自快照(不是当前态)
    for j in res.jars:
        j.enabled = (j.base_name == "z.jar")
    check("恢复目标集", snap_mod.snapshot_target(data, res)
          == frozenset({"x.jar", "y.jar", "z.jar", "w.jar"}))
    # 一致性: 目录不符 / 快照后新增
    res2 = ScanResult(mods_dir=os.path.join(tmp, "other"), jars=list(res.jars))
    warns = snap_mod.snapshot_check(data, res2)
    check("目录不符警告", any("目录" in w for w in warns), str(warns))
    extra = JarInfo(directory=res.mods_dir, base_name="new.jar",
                    enabled=True, size=1)
    res3 = ScanResult(mods_dir=res.mods_dir, jars=list(res.jars) + [extra])
    warns3 = snap_mod.snapshot_check(data, res3)
    check("新增jar警告", any("新增" in w for w in warns3), str(warns3))
    # 磁盘未动, 重扫与快照一致 → 零警告
    res_new = scan_mods_dir(os.path.join(tmp, "mods2"), cfg)
    check("一致零警告", snap_mod.snapshot_check(data, res_new) == [])


def test_repair(tmp: str) -> None:
    """v0.2: 修补系统(文件名匹配 + mods.toml modId 改写, repair.py)。"""
    print("[repair]")
    from modbisect import repair
    check("规范化去中文符号",
          repair.normalize_stem("能源装置 Energy.jar") == "energy")
    # 场景: target_1.20.jar 的 modId 笔误("wrongid"), decl.jar 依赖 "target"
    d = os.path.join(tmp, "mods-rep")
    os.makedirs(d)

    def _mk(path: str, modid: str, deps: tuple[str, ...] = ()) -> None:
        t = f'modLoader="javafml"\n[[mods]]\nmodId="{modid}"\nversion="1.0"\n'
        for dep in deps:
            t += f'[[dependencies.{modid}]]\nmodId="{dep}"\n'
        make_jar(path, toml_text=t)

    _mk(os.path.join(d, "target_1.20.jar"), "wrongid")
    _mk(os.path.join(d, "decl.jar"), "decl", deps=("target",))
    cfg = AppConfig(disabled_suffix=".disabled")
    res = scan_mods_dir(d, cfg)
    w_jars = [j for j in res.jars if j.enabled]
    g = DependencyGraph(w_jars, cfg.ignore_modids)
    check("缺失依赖检出", g.missing == [("decl.jar", "target")], str(g.missing))
    cands, layer = repair.find_candidates("target", res, "decl.jar")
    check("前缀层匹配", cands == ["target_1.20.jar"] and layer == "prefix",
          str((cands, layer)))
    # 改写 + 幂等 + 防御 + 备份
    jp = os.path.join(d, "target_1.20.jar")
    r = repair.patch_modid(jp, "wrongid", "target")
    check("改写成功", r is None, str(r))
    with zipfile.ZipFile(jp) as zf:
        t = zf.read("META-INF/mods.toml").decode()
    check("modId已改", 'modId="target"' in t, t)
    check("旧id退场", "wrongid" not in t, t)
    check("原件备份", os.path.isfile(jp + ".orig"))
    check("幂等重复修补", repair.patch_modid(jp, "target", "target") is None)
    check("防御不符拒写", isinstance(
        repair.patch_modid(jp, "otherid", "thirdid"), str))
    # 改写后重扫: 依赖边接通, missing 清零
    res2 = scan_mods_dir(d, cfg)
    w2 = [j for j in res2.jars if j.enabled]
    g2 = DependencyGraph(w2, cfg.ignore_modids)
    check("修补后边接通", g2.missing == []
          and set(g2.jar_deps["decl.jar"]) == {"target"})


def test_engine_scan(graph: DependencyGraph) -> None:
    """卷帘模式(v0.4): 四组合公式/锁段转二分/卷尽未命中/静默底场。

    场景(与主测试同图, 实测): W={w,x,y,z}, 单元 {w},{x,y}(SCC),{z};
    x 与 y 互为依赖(闭包互拖), z 无依赖。锁序=字典序。
    """
    order = tuple(sorted(graph.universe))  # (w, x, y, z)

    # --- 禁用方向: 消失锁段(嫌疑收缩进实际被禁单元) ---
    e = BisectEngine(graph, ScanSpec(order=order, chunk=2, enable=False,
                                     from_top=True))
    check("卷帘默认基准", e.phase is Phase.BASELINE
          and e.current_plan.phase is Phase.BASELINE)
    a = e.report(Answer.SKIP, frozenset())
    check("基准跳过进SCAN", a is Action.NEXT_PLAN
          and e.phase is Phase.SCAN)
    b1 = graph.closure({"w.jar", "x.jar"}, set(graph.universe))
    p = e.current_plan
    check("禁向帘带闭包拖拽", p.proposed_disabled == frozenset(b1),
          str(sorted(p.proposed_disabled)))
    check("禁向其余全启用",
          p.target_enabled == graph.universe - frozenset(b1))
    a = e.report(Answer.ABSENT, frozenset(b1))
    check("禁向消失锁段转二分", a is Action.NEXT_PLAN
          and e.phase is Phase.BISECT)
    check("禁向嫌疑收缩", e.suspects == frozenset(b1),
          str(sorted(e.suspects)))
    check("锁段后二分计划", e.current_plan.phase is Phase.BISECT)

    # --- 禁用方向: 步长1逐卷, 帘带含已卷前缀的闭包重算 ---
    e2 = BisectEngine(graph, ScanSpec(order=order, chunk=1, enable=False,
                                      from_top=True))
    e2.report(Answer.SKIP, frozenset())
    p = e2.current_plan
    check("禁向步长1首带", p.proposed_disabled
          == frozenset({"w.jar"}))
    a = e2.report(Answer.PRESENT, frozenset({"w.jar"}))
    check("禁向还在出局w", a is Action.NEXT_PLAN
          and e2.phase is Phase.SCAN
          and e2.suspects == frozenset({"x.jar", "y.jar", "z.jar"}))
    p = e2.current_plan
    # 帘带={x}, 但闭包拖入 y, 且已卷的 w 仍在实际禁用集内
    check("禁向第2带含前缀闭包", p.proposed_disabled
          == frozenset({"w.jar", "x.jar", "y.jar"}),
          str(sorted(p.proposed_disabled)))
    a = e2.report(Answer.PRESENT, frozenset({"w.jar", "x.jar", "y.jar"}))
    check("禁向嫌疑独苗仍SCAN", a is Action.NEXT_PLAN
          and e2.phase is Phase.SCAN
          and e2.suspects == frozenset({"z.jar"}))
    p = e2.current_plan
    # 帘带={y}: 闭包仍拖 x, 嫌疑无收缩但指针恒进(无退化死循环)
    check("禁向第3带", p.proposed_disabled
          == frozenset({"w.jar", "x.jar", "y.jar"}))
    a = e2.report(Answer.PRESENT, frozenset({"w.jar", "x.jar", "y.jar"}))
    check("禁向第3带仍SCAN", a is Action.NEXT_PLAN
          and e2.phase is Phase.SCAN)
    p = e2.current_plan
    check("禁向末带={z}", p.proposed_disabled == frozenset(graph.universe))
    a = e2.report(Answer.PRESENT, frozenset(graph.universe))
    check("禁向卷尽矛盾终局", a is Action.DONE
          and e2.verdict is not None
          and "卷帘未命中" in e2.verdict.title)

    # --- 禁用方向: 一步卷尽 + 整域消失锁段 + 锁段后二分接管 ---
    e3 = BisectEngine(graph, ScanSpec(order=order, chunk=4, enable=False,
                                      from_top=True))
    e3.report(Answer.SKIP, frozenset())
    a = e3.report(Answer.ABSENT, frozenset(graph.universe))
    check("禁向整段消失锁段", a is Action.NEXT_PLAN
          and e3.phase is Phase.BISECT
          and e3.suspects == graph.universe)
    p = e3.current_plan
    a = e3.report(Answer.PRESENT, frozenset(p.proposed_disabled))
    check("锁段后二分正常收敛", a is Action.NEXT_PLAN
          and e3.suspect_count == 1)

    # --- 启用方向: 静默底场(仅首带支撑闭包启用) + 出现锁段 ---
    e4 = BisectEngine(graph, ScanSpec(order=order, chunk=2, enable=True,
                                      from_top=True))
    e4.report(Answer.SKIP, frozenset())
    p = e4.current_plan
    check("启用向静默底场", p.target_enabled
          == frozenset({"w.jar", "x.jar", "y.jar"}),
          str(sorted(p.target_enabled)))
    check("启用向压禁集", p.proposed_disabled == frozenset({"z.jar"}))
    a = e4.report(Answer.PRESENT, frozenset({"z.jar"}))
    check("启用向出现锁段", a is Action.NEXT_PLAN
          and e4.phase is Phase.BISECT)
    check("启用向嫌疑=实际启用", e4.suspects
          == frozenset({"w.jar", "x.jar", "y.jar"}),
          str(sorted(e4.suspects)))

    # --- 启用方向: 消失继续卷 + 支撑闭包提前上场 + 末段锁段 ---
    e5 = BisectEngine(graph, ScanSpec(order=order, chunk=2, enable=True,
                                      from_top=True))
    e5.report(Answer.SKIP, frozenset())
    a = e5.report(Answer.ABSENT, frozenset({"z.jar"}))
    check("启用向消失继续卷", a is Action.NEXT_PLAN
          and e5.phase is Phase.SCAN)
    check("启用向嫌疑剔除启用侧", e5.suspects == frozenset({"z.jar"}))
    p = e5.current_plan
    # 帘带={y,z}: support({y,z})={x,y,z} 提前上场, 加前缀 {w,x} = 全域
    check("启用向支撑闭包", p.target_enabled == graph.universe,
          str(sorted(p.target_enabled)))
    check("启用向全域启用压禁空", p.proposed_disabled == frozenset())
    a = e5.report(Answer.PRESENT, frozenset())
    check("启用向末段锁段直达验证", a is Action.NEXT_PLAN
          and e5.phase is Phase.VERIFY
          and e5.suspects == frozenset({"z.jar"}))


# ---------------------------------------------------------------- main

def main() -> int:
    tmp = tempfile.mkdtemp(prefix="modbisect-test-")
    cfg = AppConfig(disabled_suffix=".disabled")
    try:
        test_scanner(tmp)
        res2, graph = build_graph_scene(tmp, cfg)
        W = set(graph.universe)
        test_depgraph(graph, W)
        test_engine(graph, W)
        test_engine_branches(graph, W)
        test_engine_scan(graph)
        test_executor(res2, cfg)
        test_session(tmp, cfg)
        test_sortkey()
        test_depgraph_missing(graph)
        test_snapshots(tmp, cfg)
        test_repair(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n结果: {_PASS} 通过, {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())