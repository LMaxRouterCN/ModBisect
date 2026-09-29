# SPDX-License-Identifier: MPL-2.0
"""排序键计算(纯函数, 无 Qt 依赖): 表头点击排序的统一键源。

- name_key(): pypinyin 把中文逐字转拼音, 英文原样保留; 全串 casefold
  后仅留字母数字 → 中英混名统一按"拼音字母序"排列, 大小写/空格/标点
  不影响相对顺序(用户 v0.2 需求: 点击表头按拼音排序)
- version_key(): 按 . - _ + 分段, 段内先数值后字典("1.10.2" 正确大于
  "1.9.4"; 字符串直接比较会得出 1.10.2 < 1.9.4 的错误结果)

键由 UI 层 KeyItem(表格项子类)存入 UserRole, __lt__ 按键比较;
复合列(状态/嫌疑)由 UI 层组元组: (主键, name_key(名称))。
"""
from __future__ import annotations

import re

from pypinyin import lazy_pinyin

# 名称键: 滤掉字母数字以外的一切(空格/标点/全角字符)
_NON_ALNUM = re.compile(r"[^0-9a-z]")
# 版本分段: 按 . - _ + 切开
_VERSION_SPLIT = re.compile(r"[.\-+_]")
# 数字开头的段拆成(数值, 剩余文字), 使 "1a" < "1b" < "2"
_LEADING_NUM = re.compile(r"^(\d+)(.*)$")


def name_key(label: str) -> str:
    """名称拼音键: 中文→拼音(逐字), 英文原样, 拼接后仅字母数字(全小写)。"""
    parts = lazy_pinyin(label.strip().casefold())
    return _NON_ALNUM.sub("", "".join(parts))


def version_key(version: str) -> tuple:
    """版本分段键: 每段 (类型, 数值, 文字) 三元组组成的元组。

    数字开头段 (0, n, rest) 先按数值再按剩余文字比较; 纯文字段
    (1, 0, text) 恒排同位置数字段之后。空版本 → 空元组(恒排最前)。
    """
    segs: list[tuple[int, int, str]] = []
    for seg in _VERSION_SPLIT.split(version.strip().casefold()):
        if not seg:
            continue  # 连续分隔符产生的空段
        m = _LEADING_NUM.match(seg)
        if m:
            segs.append((0, int(m.group(1)), m.group(2)))
        else:
            segs.append((1, 0, seg))
    return tuple(segs)