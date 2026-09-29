# ModBisect

MC 问题 mod 二分排查工具 — 用二分法自动定位"不报错、不崩溃"的问题 mod。

渲染异常 / 行为异常 / 性能异常这类加载器零提示的问题, 传统排查只能人肉开关 mod。
本工具把 "开关 mod → 启动游戏 → 观察结果" 循环自动化为状态机,
以游戏进程生命周期为轮次边界, log2(n) 轮收敛到罪魁 mod。

## 技术栈

Python 3.12+ / PySide6 / psutil / watchdog / Windows

## 架构(五层, 计算与调度解耦, 全链路事件驱动)

```
scanner ──枚举/解析──▶ model + depgraph ──图/闭包──▶ engine(纯函数, 不碰文件系统)
                            │                            │ RoundPlan(目标启停集)
                            ▼                            ▼
watcher + processmon ◀──事件── executor(diff 重命名, 幂等, 回读验证)
                            ▼
                     ui/app.py(PySide6 信号槽, 全程序唯一有"时间"概念的层)
```

- 引擎只做纯状态转移(报告 → 归算 → 下一计划), 不碰文件系统
- 执行层以 diff 重命名实现幂等启停, apply 后回读磁盘,
  引擎以"实际生效集"归算答案(不信计划自封)
- 观察层: watchdog 监听 logs/latest.log(启动信号)与 crash-reports/(崩溃信号);
  psutil 绑定游戏进程做内核级等待(零 CPU), 绑定失败才降级为进程快照轮询
- UI 层是唯一调度者, 耗时操作全走后台线程, 界面零阻塞

## 用法

```
python -m pip install -r requirements.txt
python main.py
```

1. 选择 .minecraft 实例的 mods 目录 → 扫描(解析全部顶层 jar 元数据)
2. 点「开始排查」→ 基准轮: 按当前状态启动游戏确认 bug 存在(确定存在可跳过)
3. 之后每轮: 工具禁用约一半嫌疑 mod → 提示你启动游戏 → 测完正常关闭游戏
   → 弹窗回答 "问题还在 / 消失了"
4. 收敛后自动进入验证轮(只启用嫌疑 mod 及其必需依赖)→ 终局报告
5. 一键还原所有 mod 到初始状态

会话全程自动存档(sessions/), 中断后可恢复继续。

## 注意事项

- 仅处理 mods 目录**顶层**的 jar(不递归)
- 假设同一时刻只运行一个游戏实例
- 游戏崩溃的那一轮观察可能无效, 弹窗会警告并建议重测
- 验证轮不复现 = 疑似多 mod 交互问题, 报告后结案(组合搜索见后续迭代)
- 禁用方式 = 文件名追加后缀(xxx.jar.disabled, HMCL/PCL 通用约定),
  config.json 可调 disabled_suffix 等参数(首次运行自动生成)

## 测试

```
python tests/test_core.py   # 核心模块: 引擎/执行器/会话(无 GUI, 66 断言)
python tests/smoke.py       # UI 冒烟: offscreen 无头跑完整半轮(24 断言)
```

## 许可证

MPL-2.0, 见 LICENSE。
