# GOAL-PLAN — MC Mod 二分排查工具 (modbisect)

仓库: mcmoddebughelper | 许可证: MPL-2.0 | 平台: Windows | 维护者: Max (LMaxRouterCN)
技术栈: Python 3.14 + PySide6 + psutil + watchdog

## 1. 任务总结
辅助 MC 玩家用二分法定位问题 mod 的桌面工具。
目标问题域: **不报错不崩溃的逻辑 bug 与视觉 bug**(渲染异常/行为异常/性能异常),
这类问题加载器零提示,只能靠人工开关 mod 排查。
工具价值: 把"人肉开关 → 开游戏 → 观察记录"循环自动化为状态机,
以**游戏进程生命周期**为轮次边界, log2(n) 轮收敛到罪魁 mod。

## 2. 用户故事
1. 玩家选中 mods 目录
2. 工具解析全部顶层 jar(不递归)的 mods.toml 系元数据
3. 玩家点开始 → 基准轮(可跳过)确认 bug 存在 → 工具按二分禁用一半 → 提示玩家启动游戏
4. 玩家测完关游戏 → 工具检测到进程退出 → 弹窗: 还在 / 消失了 / 重新测试本轮
5. 循环收敛 → 验证轮 → 报告罪魁 → 一键还原全部

## 3. 非目标(MVP 明确不做)
- Fabric / 1.7.10 (mcmod.info 仅做展示信息解析)
- 多罪魁组合搜索(验证轮失败只报告"疑似交互问题")
- incompatible 依赖处理 / versionRange 校验
- 启动器实例自动发现 / 自由开关记录模式(后续迭代)

## 4. 架构(五层,计算与调度解耦,全链路事件驱动)
```
扫描层 scanner ──枚举/解析──▶ 模型层 model/depgraph ──图/闭包──▶ 引擎层 engine(纯函数,不碰FS)
                                     │                              │ RoundPlan(目标状态)
                                     ▼                              ▼
观察层 watcher/processmon ◀──事件── 执行层 executor(diff重命名,幂等)
                                     ▼
                               UI层 PySide6(信号槽,全程序唯一有"时间"概念的地方)
```

## 5. 模块清单
| 文件 | 职责 |
|---|---|
| modbisect/config.py | 全部可调参数,config.json 加载/生成 |
| modbisect/model.py | JarInfo/ModInfo/Dependency 数据模型 |
| modbisect/scanner.py | 非递归枚举 + zipfile 解析 mods.toml/neoforge.mods.toml/mcmod.info/jarjar |
| modbisect/depgraph.py | modid 提供者映射 + mandatory 闭包 + 绑定单元(SCC) |
| modbisect/engine.py | 二分状态机(纯函数): RoundPlan/答案归算/判决 |
| modbisect/executor.py | diff 重命名执行(幂等,可重试),外部篡改检测 |
| modbisect/watcher.py | watchdog: latest.log 启动信号 + crash-reports 崩溃信号 |
| modbisect/processmon.py | psutil 进程绑定 + 内核级 wait + 文件锁降级 |
| modbisect/session.py | 会话 JSON 持久化/恢复 |
| modbisect/ui/app.py | 主窗口,状态机调度,信号枢纽 |
| modbisect/ui/dialogs.py | 结果弹窗/判决弹窗 |
| main.py | 入口 |

v0.2 新增外围层(独立模块, 不侵入五层主链):
- modbisect/sortkey.py     拼音/版本排序键(纯函数)
- modbisect/snapshots.py   mod 启停状态全集快照(建/列/读/一致性检查/目标集)
- modbisect/repair.py      缺失依赖修补(文件名反查候选 + mods.toml modId 改写)
- modbisect/ui/style.py    黑金直角主题(QSS + 语义色常量, 唯一色源)
- modbisect/ui/panels.py   依赖关系画框(双击行展开)

## 6. 关键设计决策(已与用户确认)
- D1 轮次边界 = 游戏进程生命周期,用户手动启动,工具被动观察,不碰启动器
- D2 启动信号 = watchdog 监听实例根/logs/latest.log 创建/重写(实例根 = mods 父目录)
- D3 结束信号 = latest.log 出现瞬间枚举 java 进程,cmdline 含主类者绑定 PID,
  后台线程内核级 wait;找不到进程 → 降级为进程快照轮询(fallback_poll_interval_ms,
  全程序唯一轮询,仅降级模式启用)。
  修订: 原方案的 latest.log 文件锁探测弃用 —— log4j2 以共享写持锁,
  Windows 上独占打开测试无法区分运行/退出(假信号源), 进程快照才是确定性信号
- D4 防抖 = 进程退出后 3s 窗口内出现新游戏进程 → 视为连续重启,继续等待
- D5 信号防污染三件套 = 崩溃检测(轮内 crash-reports 新文件 → 弹窗警告) /
  按钮文案带因果链("还在=问题在启用侧") / 重新测试本轮(幂等重放)
- D6 禁用 = 文件名在 ".jar" 后追加配置后缀(默认 ".disabled" → "xxx.jar.disabled")
  后缀语义: config.disabled_suffix 不含 ".jar" 部分,路径一律按 base + suffix 推导
- D7 依赖传播 = mandatory 闭包,禁用提议被闭包扩大,引擎以执行层回报的"实际生效集"归算答案
  工作域 W = 初始启用的 jar 全集(初始禁用者不加载即不可能为罪魁,直接出局);
  基线无人提供的依赖边在 W 上剔除,初始状态满足闭包不动点(推理自洽)
- D8 绑定单元 = 唯一提供者拖拽图的 SCC: 互为强制依赖的 jar 物理上不可分离,二分以单元为粒度
- D9 每轮 apply 前校验磁盘实际状态 vs 引擎记录,不一致 → 警告重扫
- D10 会话自动保存 sessions/*.json,可恢复(快照指纹 = 文件名+大小)
- D11 [v0.2 授权裁量, 醒后确认] 级联启停对称: 双击状态格的级联集 = base + 传递依赖它的;
  禁用=拖死整条依赖链(与引擎闭包同向), 启用=连带拉起(两方向波及, 字面执行)
- D12 [v0.2 授权裁量, 醒后确认] 快照恢复: 一致性警告=闸门而非静默排除(指纹不符的 jar
  用户确认后仍可恢复); 修补 .orig 备份永不覆盖(首次=真原件), 修补失败入忽视表
  防"重扫→再弹"死循环, 在途串行(完成一次重扫再查下一缺失)
- D13 [v0.2 授权裁量, 醒后确认] 判决非模态化(右列按钮), JUDGING 态可中止(对等原弹窗
  "关窗=中止"路径); 主控按钮移驻右列; UI 偏好走 config.json 平铺字段(不碰注册表)


## 7. 二分语义(引擎正确性核心)
- S = 嫌疑集(不变量: 罪魁 ∈ S)
- 每轮禁 B⊂S,闭包扩为 D';实际生效集由执行层回报,不由计划书自封
- "还在" → S := S \ D';"消失了" → S := S ∩ D'
- 收敛(嫌疑只剩一个绑定单元) → 验证轮: 只启用该单元 + 其依赖闭包
  复现 = 单因罪魁;无复现 = 交互问题或信号污染 → 报告并结案
- 退化保护: S 变空 → 信号矛盾报错; S 不变 → 闭包吞掉分割,转人工
- 基准轮(第0轮,可跳过): 目标状态 = 初始状态(W 全启);不复现 → 前提动摇(间歇性/非 mod 因素)结案

## 8. 事件链(无轮询无阻塞)
选目录 → ScanWorker(线程) → 建图 → 引擎首计划 → ExecutorWorker(diff 重命名)
→ watcher.arm() → [latest.log 重写] → 进程绑定(宽限 2s 重试一次) → wait 线程(内核等待)
→ 退出 → QTimer 3s 防抖 → 无新进程 → 结果弹窗 → 引擎.report → 下一轮/判决 → 循环
崩溃检测: 轮内 crash-reports 新文件 → 弹窗红色警告横幅

## 9. 已知边界/限制
- versionRange 不校验(错误版本由 loader 自己报错,不属本工具问题域)
- jarjar 内嵌 jar 只解析一层,内嵌 mod 的 modid/依赖并入外层 jar(闭包宁过勿欠,防二分中途缺依赖硬报错)
- 一个 jar 多 mod 时,开关粒度是 jar
- 假设同一时刻只有一个游戏实例
- 启动器若用非常规禁用后缀,改 config.disabled_suffix
- apply 与用户启动游戏存在毫秒级竞态 → PermissionError → 提示关闭游戏后重试(diff 幂等,安全)
- modid 匹配大小写不敏感(按小写归一,Forge 规范本身小写,防御违规 mod)

## 10. 风险
- PySide6 对 Python 3.14 的 wheel 支持(装不上 → 建 3.12 venv 兜底)
- watchdog 事件时序(NTFS 正常;网络盘/符号链接可能异常)
- 杀软干扰批量重命名 → 失败重试,幂等

## 11. 测试计划(实际交付形态)
- tests/test_core.py: 纯模块全链(scanner/depgraph/engine/executor/session
  + v0.2 sortkey/snapshots/repair/missing, 95 断言)
- tests/smoke.py: offscreen 无头冒烟, 真实事件链含 v0.2 全新功能(级联/快照/
  判决非模态/调试直通, 53 断言)
- 真机: Max 用真实整合包跑一轮

## 12. 里程碑
- [x] M0 骨架 + 本文档
- [x] M1 model/config/scanner/depgraph
- [x] M2 engine
- [x] M3 executor/watcher/processmon/session
- [x] M4 ui + main
- [x] M5 依赖安装 + 测试 + git + LICENSE
- [x] M6 v0.2 交付: 排序/级联启停/依赖画框/快照/修补/判决非模态/调试直通/
  黑金主题/UI 偏好持久化

## 13. 后续迭代(优先级序)
1. 自由开关+测试记录模式(交互问题逃生门)— v0.2 已落地自由开关半边
   (双击级联启停 + 快照系统); 测试记录模式(记录用户手动开关反推嫌疑)仍未做
2. 组合搜索(最小复现集上 leave-one-out)
3. Fabric 支持(fabric.mod.json)
4. 更聪明的切分启发式(避开枢纽依赖,按闭包影响最小侧切)
5. 启动器实例自动发现