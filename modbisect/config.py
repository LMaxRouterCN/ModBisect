# SPDX-License-Identifier: MPL-2.0
"""配置模块。

所有可调参数集中于此,程序其余部分一律从 AppConfig 读取,禁止硬编码魔法值。
配置文件为程序根目录下的 config.json(首次运行自动生成默认值,可手工编辑)。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

# 程序根目录 = 本包目录的父目录
PROGRAM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROGRAM_ROOT, "config.json")
# 会话持久化目录(排查中断可恢复)
SESSIONS_DIR = os.path.join(PROGRAM_ROOT, "sessions")


@dataclass
class AppConfig:
    # ---- 禁用机制 ----
    # 禁用 mod 的方式: 在 ".jar" 之后追加此后缀构成禁用文件名。
    # 默认 ".disabled" → 禁用态文件形如 "xxx.jar.disabled"(HMCL/PCL 通用约定)。
    # 语义注意: 此值不含 ".jar" 部分,启停路径一律按 base(以 .jar 结尾) + suffix 推导
    disabled_suffix: str = ".disabled"

    # ---- 执行层(重命名) ----
    # PermissionError(游戏占用/杀软扫描锁)的重试次数与退避间隔。
    # 退避只发生在 UI 提供的工作线程内,界面零阻塞
    rename_retries: int = 3
    rename_backoff_ms: int = 400

    # ---- 进程 / 轮次边界 ----
    # 游戏主类: cmdline 含此子串的 java 进程视为游戏本体
    # (启动器自身也是 java 进程,但主类不同,天然被过滤;自定义环境可改此项)
    game_main_class: str = "net.minecraft.client.main.Main"
    # 进程退出后的防抖窗口: 此窗口内出现新游戏进程 → 视为用户连续重启,继续等待
    launch_debounce_ms: int = 3000
    # latest.log 出现后绑定进程的宽限期(首次枚举不到时,一次性等待后重试)
    process_bind_grace_ms: int = 2000
    # 降级模式: 绑定失败时的进程快照轮询间隔(全程序唯一轮询,仅降级启用)。
    # 原 latest.log 文件锁探测方案不可靠(log4j2 共享写持锁),已弃用,见 GOAL-PLAN D3
    fallback_poll_interval_ms: int = 500

    # ---- UI 偏好持久化(config.json 平铺字段, UI 层读写, 程序逻辑不消费) ----
    # 窗口几何: QMainWindow.saveGeometry() 的 QByteArray -> base64 文本
    ui_window_geometry: str = ""
    # mod 表表头状态: 列宽/列序/当前排序列(saveState() -> base64 文本)
    ui_header_state: str = ""
    # 上次选择的 mods 目录(启动时预填目录框, 免重复浏览)
    ui_last_mods_dir: str = ""
    # ---- 依赖图 ----
    # 这些 modid 由加载器/JDK/游戏本体提供,不参与 mods 目录内的依赖传播
    ignore_modids: list[str] = field(default_factory=lambda: [
        "forge", "neoforge", "minecraft", "fml", "javafml", "java",
    ])


def load_config() -> AppConfig:
    """读取 config.json;缺失字段回退默认值;文件不存在则落盘一份默认配置。"""
    cfg = AppConfig()
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 只接受已知字段,未知字段忽略(手工编辑笔误不至于炸程序)
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except (json.JSONDecodeError, OSError):
            # 配置损坏 → 静默回退默认值,不让启动失败
            pass
    else:
        # 首次运行: 生成默认配置文件供用户查阅/编辑
        save_config(cfg)
    return cfg


def save_config(cfg: AppConfig) -> None:
    """把配置写回 config.json。"""
    os.makedirs(PROGRAM_ROOT, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)