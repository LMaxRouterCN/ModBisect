# SPDX-License-Identifier: MPL-2.0
"""数据模型层。

两层映射(核心关系,全程序遵守):
- 文件系统开关以 *jar* 为单位(重命名文件)
- 依赖关系以 *modid* 为单位(一个 jar 可内含多个 mod / 内嵌 jarjar mod)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Dependency:
    """mods.toml 中的一条依赖声明。"""
    modid: str
    # 强制依赖(type=required 或旧式 mandatory=true):
    # 提供者全部被禁时,依赖它的 mod 无法加载 → 禁用会沿此边传播
    mandatory: bool


@dataclass
class ModInfo:
    """jar 内的一个 mod 条目([[mods]] 表)。"""
    modid: str
    display_name: str
    version: str
    dependencies: list[Dependency] = field(default_factory=list)
    # 该 mod 是否来自 jarjar 内嵌 jar(内嵌 mod 与外层 jar 同生共死)
    nested: bool = False

    @property
    def label(self) -> str:
        """展示名: 优先人类可读名称,退回 modid。"""
        return self.display_name or self.modid


@dataclass
class JarInfo:
    """mods 目录顶层的一个 jar 文件及其解析结果。

    base_name 是恒定身份(恒以 .jar 结尾);实际文件名随启停在 base_name 上追加禁用后缀。
    """
    directory: str          # mods 目录绝对路径
    base_name: str          # 身份文件名,如 "jei-1.20.1.jar"
    enabled: bool           # 扫描时刻的磁盘状态
    size: int               # 文件字节数(会话恢复时的指纹校验用)
    mods: list[ModInfo] = field(default_factory=list)
    source: str = "unknown" # 元数据来源: mods.toml / neoforge.mods.toml / mcmod.info / unknown

    # ---- 路径计算 ----
    def path(self, enabled: bool, disabled_suffix: str) -> str:
        """指定状态对应的磁盘路径。"""
        name = self.base_name if enabled else self.base_name + disabled_suffix
        return os.path.join(self.directory, name)

    def current_path(self, disabled_suffix: str) -> str:
        """当前状态对应的磁盘路径。"""
        return self.path(self.enabled, disabled_suffix)

    # ---- 展示辅助 ----
    @property
    def modids(self) -> list[str]:
        return [m.modid for m in self.mods]

    @property
    def label(self) -> str:
        """表格/日志展示名: 各 mod 名称(去重);无元数据时退回文件名。"""
        if self.mods:
            return ", ".join(dict.fromkeys(m.label for m in self.mods))
        stem = self.base_name[:-4] if self.base_name.endswith(".jar") else self.base_name
        return stem

    @property
    def version(self) -> str:
        return self.mods[0].version if self.mods else ""