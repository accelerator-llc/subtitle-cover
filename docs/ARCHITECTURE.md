# 架构契约（ARCHITECTURE.md）

> 本文是本次重构的**设计权威**，由架构 agent 撰写，文中所有耦合数字均经实测复验。
> `REFACTOR_PLAN.md` 提供需求清单、环境事实与验收工具；两者冲突时：
> **事实以 plan 为准（可复验），结构以本文为准**。
> 函数怎么拆、变量叫什么、文件叫什么，由实施者决定；
> **模块边界、依赖方向、线程契约、状态归属不得违反本文**——若实施中发现本文某条
> 确实不合理，停下来在报告里说明理由，由架构 agent 修订，不要私自绕过。

---

## 1. 实测现状（可自行复核，命令附后）

| 现象 | 实测 |
| --- | --- |
| import 图已是干净 DAG，**无循环依赖** | app→{ui,x11,render,core}；x11→core；ui→core；render→无内部 import |
| App 单向伸手进 Bar | app.py 中 `self.bar.` **46 处**（含直接读写属性）；cover.py 中 `self.app.` **0 处**（plan §4.4「互相读写」只有一半成立） |
| 共享可变配置 | `CoverBar(self.dpy, self.cfg)` 与渲染线程持**同一个活 dict**；渲染线程直接读 `app.cfg / app.bar / app.locked_now`，**无任何同步** |
| 配置写跨线程 | `save_config(` 调用点：app.py **17** + cover.py **2**（渲染线程里写配置文件） |
| 几何下标散布 | `cfg['x'/'y'/'w'/'h']` 直接下标 **35 处** |
| 状态无单一归属 | 旗标命中：app.py 12、cover.py 20、render.py 3；`locked`(鼠标穿透) vs `locked_now`(锁屏) 命名陷阱 |
| 死代码 | `_mid_press_time`×4（恒为 0.0）、`peek_factor`×1（只赋值不读） |
| render.py | 60 行，`RenderThread(app)` 每帧读 app 三个内部字段后调 `bar.render()`（197 行大函数） |

复核命令：`grep -c "self\.bar\." subtitle_cover/app.py`、`grep -c "self\.app\." subtitle_cover/x11/cover.py`、
`grep -c "save_config(" subtitle_cover/app.py subtitle_cover/x11/cover.py`、
`grep -rn "cfg\['x'\]" subtitle_cover/ | wc -l`。

**结论**：问题不是「缺分层」，而是①一个活 dict 被两个线程共享、②App 把 Bar 当数据结构摸、
③渲染线程反向读 App。重构 = 收敛这三件事，**不是加抽象层**。

---

## 2. 架构决策（不可违反）

### D0 目标层数：4 层，不新增

```
app（组装根：GLib 主循环、X 事件轮询、状态机、配置写）
 ├── ui      （GTK 面板/便签/托盘）
 ├── x11     （X 窗口、抓屏、渲染线程——含原 render.py 的职责）
 └── core    （config / blur 纯像素管线）
```

- **顶层 `render.py` 撤销**：渲染线程收归 x11 层（bar 自持线程），消灭
  `RenderThread(app)` 反向读 App 的结构。删除或留空壳均可，不再作为一层。
- plan §5 的 9 条 X11 经验约束（PutImage 返回值、FastX 线程独占、
  MoveResize→PutImage 同连接顺序、bg_pixel 跟随 tint、30ms 轮询……）**原样遵守**。

### D1 状态两分：意图归 App，机制归 Bar 私有

- **用户意图/会话状态**（visible、input_locked 鼠标穿透、screen_locked 锁屏、
  panel_open、peek_active、presets）→ **唯一归属 App**。
- **渲染机制状态**（solid_until、pending_geo、当前物理尺寸、遮罩缓存）→
  **CoverBar 私有**，App 一律不读写，只能经方法影响。
- 消灭命名陷阱：代码内改名 `input_locked`（穿透）/ `screen_locked`（锁屏）；
  **config.json 的键名保持不变**（兼容用户现有配置文件）。

### D2 边界即命令：App 只能调用 Bar 的方法，禁止摸 Bar 的属性

- app.py 中 `self.bar.<x>` 的 `<x>` 必须是 CoverBar 的公开方法；
  数据进出全部走方法参数/返回值（46 处里的属性读写要方法化）。
- 反向保持现状：CoverBar 不持有 app、不回调 App 字段（现在是 0 处，保持 0）。

### D3 配置三权分立：读有快照、写有单一入口、持久化只在主线程

- **写**：`save_config` 调用只允许出现在 app 层，收敛为单一入口
  （如 `App.persist()`）；cover.py / 渲染线程中为 **0 处**。
- **读（渲染侧）**：渲染线程不再读活的共享 dict。App 在配置/状态变更时构造
  **不可变快照**（NamedTuple/dataclass/frozen 均可），**整体原子替换一个引用**；
  渲染线程每帧开头读一次该引用。单一写入点 = App。
- CoverBar 不再持有共享 cfg，配置变更走显式 setter 推送。
- 运行期几何经方法参数传递；`cfg['x'/'y'/'w'/'h']` 下标收敛到**个位数**，
  且仅存在于配置读写边界（加载/持久化/启动）。

### D4 渲染线程数据流（单向、无反向引用）

```
主线程(App): 状态机命令 → 构造快照 → 原子替换引用；bar.start()/stop() 控线程
渲染线程(bar): 读快照 → (FastX)抓层 → 像素管线(blur) → 同连接 MoveResize→PutImage
```

- 线程循环节奏/跳帧/空闲降速逻辑留在 bar 内；可见性判断改读快照
  （visible / screen_locked / peek_active 都在快照里，App 变更即推送）。
- `render()` 大函数输入**只有快照**，不读 cfg/app/GTK；内部怎么拆由实施者定。

### D5 交互修复的结构性要求

- **peek（中键透视，测试 10.2）**：按下→unmap；**松开事件的接收不得依赖横条
  处于映射状态**（提示：主线程在 ButtonPress 时对该连接 XGrabPointer，
  松开无论落在哪个窗口都能收到 XButtonRelease；或等效方案，实施者定）。
  peek 状态唯一归属 App（D1），经快照下发。
- **peek 语义（用户拍板，交互修复阶段）**：中键=按住式（grab+看门狗）；
  热键 Ctrl+Alt+O=**切换式**，无任何松开依赖（KeyRelease 丢失曾导致
  `_peek_active` 永久卡死→渲染被门控→冻结帧，此类结构已根除）。
  `_set_peek` 是透视唯一变更入口；中键 release 结束任何来源的 peek。
- **solid 模式**：几何变化 → solid + 0.8s 单调钟 → 自动回 frosted（机制保留，
  计时状态归 Bar 私有，D1）。
- **notes Esc（测试 11.2）**：若基线复测仍失败，按「弹窗必须先取焦再响应 Esc」
  方向修；若是双实例幻影（基线已通过）则不动。

### D6 保留区（不要动）

- `core/blur.py`、`core/config.py` 对外行为；`x11/fastx.py` 内部实现；
- `ui/panel.py / notes.py / tray.py` 业务逻辑（近期刚修复，只允许
  删死代码/跟随改名级别的接触）；
- 12 条产品需求（plan §2）逐条行为不变；
- 不新增第三方依赖；**总行数不得显著增长**（重构≠加代码）。

---

## 3. 依赖方向（check_static.py 强制执行）

按 **import 语句**判定；运行期把 app 实例作为回调对象传给 ui 构造函数是允许的
（GTK 应用的正常接线），但 ui 不得 import app 模块。

```
app   → ui, x11, core
ui    → core                       （不得 import app/x11）
x11   → core                       （不得 import app/ui）
core  → 仅标准库/PIL               （不得 import 其余层）
```

## 4. 验收（在 plan §6 之上追加）

5. app.py 中 `self.bar.<非方法属性>` = 0（check_static 可验证）；
   cover.py 中 `self.app.` = 0、`save_config` = 0
6. 渲染线程读快照（不可变对象、引用整体替换、唯一写入点在 App）
7. `cfg['x'/'y'/'w'/'h']` 运行期下标访问 ≤ 个位数，仅限配置边界
8. 死代码清单为零（`_mid_press_time`、`peek_factor` 及实施中新发现的）
9. 顶层不再存在 `RenderThread(app)` 结构（render.py 并入 x11 或删除）

## 5. 实施流程（防屎山机制）

1. **增量式**：每个决策一次小步；每步 `python3 -m py_compile` 全绿才算完成；
2. 每 2~3 步批量跑 `DISPLAY=:1 python3 tools/test_all.py`（抢鼠标 2.5 分钟），
   并在 `docs/REFACTOR_LOG.md` 记一行：动机 / 动作 / 验证结果；
3. 某步测试大崩 → 回退该步、记录原因，**不带病前进**；
4. 全程 **git 不 commit**；中文注释；结束时报 `git status --short` + `git diff --stat`。
