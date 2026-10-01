# 字幕遮挡条 —— 重构任务书

> **本文件刻意只写「经过证据确认的事实」+「目标」+「验收标准」，不写具体设计。**
> 上一版任务书曾把模块划分、状态字段、函数拆法都写成硬性规格，那是编写者
> 在上下文不健康时的**臆断**，可能本身就是错的。**结构怎么设计由执行者决定**，
> 你只需要对「问题真实存在」和「验收标准」负责。凡本文件标注为
> **【推断】** 的内容，都只是待验证的假设，**可以推翻**；推翻时请在报告里说明理由。

---

## 1. 任务

把 `subtitle-cover`（约 3200 行 Python/X11 小工具）
做一次**结构性重构**，目标是「每个概念只有一个归属、静态分析能看懂」；
**对外行为完全不变**；用已有的 26 项自动化测试（`tools/test_all.py`）保证全绿；
并修掉真实存在的 bug。

**不要 git commit**（人类要审核后自己提交）。

---

## 2. 这个软件要做什么

看双语字幕视频时，用一条**磨砂玻璃（实时高斯模糊）横条**盖住中文字幕行，只留英文字幕。
需求共 12 条：

1. 一条横条，**永远置顶**（含全屏视频之上）—— X11 override-redirect 窗口
2. 横条显示**其正下方画面的实时高斯模糊**（要能看出下面有东西在动，不是纯色遮挡）
3. 左键拖动移动
4. 边缘拖拽缩放
5. 滚轮改高度；Shift+滚轮改宽度；Ctrl+滚轮改模糊强度
6. 右键 → 控制面板（尺寸/模糊/色调/噪点/提示时长/帧率/笔记目录/预设）
7. 全局热键（见 `core/config.py` 的 `DEFAULT_HOTKEYS`）
8. 快速笔记弹窗（Enter 换行、Ctrl+S 保存、Ctrl+Enter 保存并清空、Esc 关闭）
9. 位置预设（保存/应用/删除/循环切换）
10. 鼠标穿透锁定（锁定后点击穿透到下层，仍可用热键呼出面板解锁）
11. 中键透视（按住 → 横条消失看真实画面；松开 → 回来）
12. 配置持久化

**非功能要求**：目标机器是多年前的老轻薄本 —— 空闲 CPU < 1%、常驻内存 < 150MB；
拖动/缩放/滚轮等几何变化期间不能出现"模糊内容延迟跟随"的廉价感。

---

## 3. 环境与操作

- Ubuntu 24.04 X11 + mutter，`DISPLAY=:1`，2560x1600 @200% 缩放
  （**GTK 逻辑坐标 = X 物理坐标 / 2**）
- 无 sudo。python-xlib / PyGObject 在 `subtitle_cover/.deps/extracted/usr/lib/python3/dist-packages`
  （`subtitle_cover/__init__.py` 已注入 `sys.path`；直接 `python3 run.py` 即可）
- 配置：`~/.config/subtitle-cover/config.json`；横条窗口 XID：`~/.config/subtitle-cover/bar_xid`
- 杀进程：进程 cmdline 可能是 `python3 run.py` **或** `/usr/bin/python3 run.py`（测试拉起时带全路径），
  `^python3 run\.py` 锚定会漏检后者。用 `pgrep -af 'run\.py$'` 检查、`pkill -f 'run\.py$'` 杀（先确认匹配到的只有目标）
- **测试会抢用户的鼠标键盘**：XTest 合成真实输入事件。**少跑、批量跑**。
- 验收工具：`DISPLAY=:1 python3 tools/test_all.py`（约 2.5 分钟；
  它会自己重启程序、临时改配置、结束时恢复原配置并把程序重新拉起）

---

## 4. 已确认的事实（每条都附了产生它的命令，**请自行复验**）

### 4.1 【重要】单实例锁曾经失效，修复已在工作区（勿丢）

原 `subtitle_cover/__main__.py` 里 `lock = acquire_single_instance_lock()` 之后紧跟 `del lock`。
`flock` 绑在 fd 上，对象被回收即释放锁。

复验方式与结果：

```bash
# 有 bug 版本: 连续启动两次
(DISPLAY=:1 python3 run.py > /tmp/a.log 2>&1 &); sleep 3.5
(DISPLAY=:1 python3 run.py > /tmp/b.log 2>&1 &); sleep 3.5
pgrep -c -f '^python3 run\.py'      # 实测 = 2
head -3 /tmp/b.log                  # 实测 = X protocol error: BadAccess, major_opcode = 33
```

- `major_opcode = 33` 即 **XGrabKey** → 第二个实例的**全局热键全部注册失败**（异常被静默吞掉）
- 修复后同样操作：进程数 = 1，第二个实例打印「字幕遮挡条已在运行」后退出

**【推断】** 两个实例同时存在会造成：屏幕上两条重叠横条；热键只控制先启动的那个；
鼠标点击落在上层的那个；两实例各自周期性写配置导致数值互相覆盖；`bar_xid` 被后启动者覆盖。
这可以解释历史上反复出现的「无法交互 / 热键无反应 / 右键打不开 / 拖动无效 / 配置与实际不一致」。
**请自行判断这个推断是否成立**，并检查是否还有其他「多实例/多份状态」的隐患。

### 4.2 测试基线（注意：这是在**双实例环境**下测的，需重测）

最近一次 `tools/test_all.py` 结果：**24/26 通过**，失败项：

| 失败项 | 观测到的现象 |
| --- | --- |
| `10.2 松开恢复显示` | 中键按住（横条消失）后松开，2.5s 内横条未恢复 |
| `11.2 Esc 关闭便签` | 笔记弹窗打开后发 Esc，弹窗未关闭 |
| `1.4 渲染下方纯色窗口(绿)` | 该项是后来新加的，尚未在稳定环境下测过 |

**请以重新测量的结果为准**——单实例锁修复后，与焦点/事件投递有关的失败
（尤其 `11.2`）可能是双实例互斗造成的**幻影失败**。

### 4.3 客观代码测量（可复验）

```bash
find . -name "*.py" -not -path "./.deps/*" | xargs wc -l | sort -n
grep -c "save_config" subtitle_cover/app.py subtitle_cover/x11/cover.py
grep -rn "cfg\['x'\]\|cfg\['y'\]\|cfg\['w'\]\|cfg\['h'\]" subtitle_cover/ | wc -l
grep -n "_mid_press_time\|peek_factor" subtitle_cover/*.py subtitle_cover/*/*.py
```

实测结果：
- 总计 3191 行；`app.py` 743 行、`x11/cover.py` 626 行、`ui/panel.py` 353 行
- `x11/cover.py` 的 `render()` 单函数 **197 行**
- `save_config` 调用点 **23 处**（`app.py` 18 + `x11/cover.py` 5）——
  即**渲染线程里也在写配置文件**
- 直接读写 `cfg['x'/'y'/'w'/'h']` 的地方 **35 处**
- `_mid_press_time`（4 处）、`peek_factor`（1 处）：**疑似死代码**

### 4.4 【推断】结构上的可能问题（待你独立验证）

- 状态可能没有单一归属：`visible/locked/peeking/drag/resize` 在 `App`，
  `interactive/solid_until/_hint_start/pending_geo/cfg_pos/_wh/peeking` 在 `CoverBar`，
  两者互相直接读写对方字段（`peeking` 甚至两边各有一份）
- 几何可能有四份表示：`cfg['x','y','w','h']`、`bar._wh`、`bar.pending_geo`、`bar.cfg_pos`
- 命名可能有陷阱：`locked`（鼠标穿透锁定）vs `locked_now`（屏幕锁屏）

**请自己读代码判断这些问题是否真实、影响面多大**，再决定怎么重构。

### 4.5 前一次审计的两处半成品（已回退，仅供参考）

前一次审计在 `cover.py` 里加了 `_solid_active` 与 `_last_root` 两个字段但**未接线**
（只有赋值、没有读取），已被回退。说明它当时认为存在这两个问题：
纯色模式的退出检测、静止画面跳过渲染的比较基准。**是否需要处理，请自行判断。**

---

## 5. 经验性约束（这些是真实失败换来的，可靠性高，请遵守）

1. `XPutImage` 在 libX11 里**永远返回 0**（即使成功）—— 不能拿返回值判断成败
2. **渲染线程只能用它自己的 FastX 连接**；python-xlib 的 Display 绝不跨线程共享
   （曾导致全黑冻结）
3. 几何变更必须由渲染线程在**同一 FastX 连接**上、紧接 `XPutImage` 之前执行
   （`XMoveResizeWindow` → `XPutImage`，顺序由 X 协议保证），否则 resize 闪黑边
4. 窗口 `background_pixel` 必须跟随 tint 颜色（否则 resize 新暴露区域闪黑）
5. `_NET_WM_WINDOW_OPACITY` 的类型必须用 `Xlib.Xatom.CARDINAL`
   （`Xlib.X` 里**没有** `XA_CARDINAL`）
6. ctypes `XQueryPointer` 的 argtypes 里 `win_y` 是 `int*`（不是 `unsigned int*`）
7. **X 事件用主动轮询**（GLib `timeout_add` 调 `dpy.pending_events()`），
   **不要改回 fd 监听**：主线程的同步 roundtrip 会把同时到达的事件读进 python-xlib
   内部缓冲，之后 fd 无新数据、监听不再触发，全部交互假死（历史上真实发生过）
8. XTest 合成事件偶发丢失（尤其热键）—— `tools/test_all.py` 里已有 `ensure()` 重试，
   **不要把 flaky 当 bug**，但也不要放过真 bug
9. 横条下方若是**纯透明覆盖层**（如 Desktop Icons，depth=32、alpha≈0），
   会被 alpha 过滤跳过；此时需要回退 root 快照，否则可能画面不更新
10. **mutter 焦点防抢占**：全局热键唤起的 GTK 弹窗没有用户交互时间戳，
    `present()` 会被拒绝激活（仅标 DEMANDS_ATTENTION）→ 弹窗不聚焦、
    Esc 落在别的窗口。必须把热键 X 事件的 `ev.time` 作为 user_time 经
    `present_with_time()` 下发（B2 实施中实证；另注意 XRaiseWindow 对
    managed 窗口会被 mutter 无视）
11. **探针压栈陷阱**：合成器会抢栈序，ctypes `XRestackWindows` 会**静默
    失败**（返回成功但栈序未变）→ 测试装置从未真正进入条下方，帧差假 0。
    压栈一律用 Xlib `configure(stack_mode=Below, sibling=...)` 并**自检
    装置像素**确认压栈成功，再采数据（交互修复阶段实证）
12. **mutter 场景栈序对 OR 窗口只认映射顺序**（残影阶段实证）：对
    override-redirect 窗口，`configure(stack_mode=Below, sibling=...)`
    只改变 X 服务器栈序（query_tree 可查证），mutter 合成画面的视觉
    栈序**仍按"最后映射者在上"**，restack 请求被无视 → 装置会盖在条上
    （X 栈序自检通过、视觉却是装置在上，两种自检结果背离）。正确做法：
    装置压栈后把被测条 unmap+map 一次使其成为最后映射的 OR 窗口；
    自检必须含像素级"条区=root 内容 vs 条自身内容"比对，仅查 query_tree
    不可信。【勘误待重测】截图普查阶段发现当时探针装置的 OR 属性因
    `tools/_xprobe.py` 的 CW 掩码/字段错位 bug 实为**受管窗口**，本条
    的装置侧观测可能被污染（条侧行为不受影响）；_xprobe 已修复，
    待以真 OR 装置复测后定稿
13. **mutter 46 忽略 `_NET_WM_BYPASS_COMPOSITOR=1` 且 OR 窗口在顶时
    从不 unredirect 全屏窗口**（残影阶段实证）：受管全屏窗口设 BYPASS=1
    （映射前/映射后/重绘后三种时机）均保持重定向（NameWindowPixmap 可
    命名）。含免合成假设的性能优化/陈旧帧缓冲类诊断在本机无对象
14. **NameWindowPixmap/FreePixmap 周期调用会打断 mutter 的纹理绑定**：
    对被合成窗口交替命名/释放窗口 pixmap（如"抓窗口自身内容"的诊断
    采样），会使条冻结在陈旧合成帧 ≥1s（肉眼可见的假残影/假失配）。
    诊断探针严禁在测量窗内做该调用；确需窗口自身内容时，一次命名后
    缓存复用并放在测量窗外（残影阶段实证，探针自我污染教训）
15. **截「含遮挡条的真实画面」只有合成器路径，且 GNOME 截图 D-Bus
    对外部进程关门**（截图普查阶段实证）：条是 OR 窗口，内容只存在于
    其自身重定向像素图 + mutter 合成输出里；root 快照写回极少（实测
    root 快照里条区滞后数秒的旧磨砂帧），scrot/import/xwd/QQ 一类
    `XGetImage(root)` 截不到当前条。`org.gnome.Shell.Screenshot` D-Bus
    （菜单 PrtSc 的实现）对外部调用者返回
    `AccessDenied: Screenshot is not allowed`（GNOME 41+ 白名单）；
    外部唯一可达的合成器路径是
    `org.freedesktop.portal.Screenshot` v2 + `interactive=false`
    （免弹窗，异步落盘 `~/图片/Screenshot-N.png`，Response 信号先于
    文件出现）。复验命令：
    ```bash
    gdbus call --session --dest org.gnome.Shell.Screenshot \
      --object-path /org/gnome/Shell/Screenshot \
      --method org.gnome.Shell.Screenshot.Screenshot false false /tmp/x.png
    # -> GDBus.Error:AccessDenied: Screenshot is not allowed
    ```
    含条出图用 `tools/probe_shot.py`（portal 路径 + 条渲染自检）

---

## 6. 验收标准

1. **行为不变**：`tools/test_all.py` **0 失败**（先重新测量基线，再逐步改、每步复测）
2. **静态可分析**：新建 `tools/check_static.py`，用 `ast` 检查至少这几类问题，
   发现问题时退出码非 0：
   - `self.xxx(...)` 调用点都有定义（GTK 父类方法用显式白名单）
   - 元组解包的元数与被调函数返回一致
   - 每个 `lib.NAME(...)`（ctypes）调用都有对应的 `argtypes` 声明
   - 不存在指向「已删除功能」的残留引用（自己维护清单）
   - 无未使用的导入/变量
3. **性能不回退**：空闲 CPU < 1%（可用 `top -b -n1 -p <pid>` 粗测）
4. **程序可运行**：结束时 `pgrep -f '^python3 run\.py'` 有且仅有 1 个进程，日志无异常

---

## 7. 交付要求

- **不要 git commit**；让工作区保持 dirty
- 保持风格一致：**中文注释**、现有目录结构习惯、现有命名风格
- 不引入第三方依赖；**目标是减少概念而不是增加抽象层**——如果你觉得
  某处不需要拆、保持现状更好，请直接说明，不要为了"看起来整洁"而拆
- 测试会抢鼠标，**批量跑、少跑**；测试中途若被中断，
  记得把配置里的 `tint_color` 改回 `[64,64,64]`（测试会临时改成红色）

## 8. 报告格式

1. **你的重构方案与理由**（模块怎么分、状态归属怎么定、为什么这么选；
   若推翻本文件的任何推断，说明依据）
2. **复测的基线**：重构前 `test_all.py` 的真实结果（哪些失败项消失了/仍在）
3. **修复的 bug**：现象 / 根因 / 修复方式 / 涉及文件:行
4. **静态检查器**：检查项 + 最终输出
5. **最终测试结果**：粘贴 `test_all.py` 完整汇总
6. **未解决的问题**（如有）：现象 + 判断 + 建议
7. **改动文件列表**：`git status --short`
