#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_static: 静态检查器(纯标准库 ast, 无第三方依赖). 依据 REFACTOR_PLAN §6.2 与
ARCHITECTURE §2/§3/§4. 每文件一趟扫描(_scan: 立即上报 A4/B6, 其余收集事实), 全部扫
完后统一评估(_evaluate, 借跨文件事实压低误报). A 类(通用防线, 应恒为 0): A1 self.xxx(...)
调用点都有定义(GI/Xlib 父类方法走白名单); A2 元组解包元数==同文件模块级函数 return
元数(仅静态可判定, 方法解包不检查); A3 ctypes lib.NAME(...) 必须有 argtypes 声明;
A4 「已删除功能」残留引用(DELETED_NAMES, 各阶段追加); A5 未使用的 import 与明显未
使用的模块级变量(保守: 确定才算). B 类(边界项, 重构中应逐步降到 0): B6 import 方向
(§3 含相对导入; core 层仅标准库/PIL); B7 app.py 中 self.bar.<attr> 必须是 CoverBar
定义的方法(D2, 属性读写都报); B8 x11/ 与顶层 render.py 内不得出现 self.app. 与
save_config(; B9 赋值后从未被读取的实例属性. 用法: python3 tools/check_static.py
[--report-only]  # 有违规退出 1; --report-only 只打印恒 0. 解析失败按违规上报不崩溃;
检查器内部异常降级为警告; ast 拿不准的场景(动态 getattr 等)一律保守放行并注释说明.
"""
import ast
import os
import sys

PKG = 'subtitle_cover'
# A4: 「已删除功能」名单 —— 任何引用(读/写/实参/形参)都算违规; 重构删除某功能后把名字追加到这里.
DELETED_NAMES = {'_mid_press_time', 'peek_factor'}   # 中键单击锁定判别 / 透视渐变系数

# A1: self.<名>(...) 白名单 —— GI 父类方法(gi 动态绑定, ast 看不到继承树)与 threading.Thread 父类.
SELF_CALL_WHITELIST = {
    'show', 'show_all', 'hide', 'present', 'destroy', 'close', 'connect', 'add',
    'set_title', 'set_keep_above', 'set_resizable', 'set_default_size', 'grab_focus',
    'queue_draw', 'move', 'resize', 'get_visible', 'get_window', 'get_style_context',
}
# §3 依赖方向(同层互引总是允许; unknown 层任何方向都越界)
_ALLOWED = {'app': {'app', 'ui', 'x11', 'core'}, 'ui': {'ui', 'core'}, 'x11': {'x11', 'core'},
            'core': {'core'}, 'unknown': set()}
_STDLIB = frozenset(getattr(sys, 'stdlib_module_names', ()))

def _layer(parts):
    """包内路径分量(去掉 PKG) -> 层名; D0 之外的新子包/顶层模块视为 unknown."""
    if not parts or parts[0] in ('__init__', '__main__', 'app'):
        return 'app'                  # 组装根/入口与 app 同层
    if parts[0] == 'render':
        return 'x11'                  # D0: 顶层 render.py 职责并入 x11 层
    return parts[0] if parts[0] in ('ui', 'x11', 'core') else 'unknown'


def _base(node):
    """ctypes 接收者归一化: Name / self.attr -> 名字, 其余 None(self.lib 与 lib 同名)."""
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'self' else None


def _cls_names(cls):
    """类体内可经 self.<名>(...) 调用的名字(方法/类属性/嵌套类/CFUNCTYPE 可调用对象)."""
    names, stack = set(), list(cls.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            names |= {t.id for t in tg if isinstance(t, ast.Name)}
    return names


def _arity(fn):
    """函数 return 元数集合; 出现非元组/裸 return/生成器/嵌套定义则返回空(保守)."""
    out, pure = set(), True
    for ch in ast.walk(fn):
        if isinstance(ch, (ast.Yield, ast.YieldFrom)) or (ch is not fn and isinstance(ch, (
                ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))):
            pure = False    # 生成器/嵌套定义: return 元数不可静态判定
        elif isinstance(ch, ast.Return):
            elts = ch.value.elts if isinstance(ch.value, ast.Tuple) else None
            if elts is None or any(isinstance(e, ast.Starred) for e in elts):
                pure = False
            else:
                out.add(len(elts))
    return out if pure and out else set()


def _scan(tree, rel, pkg_parts, shared, rep):
    """一趟扫描: 立即上报 A4/B6(无跨文件依赖), 其余事实收进结果对象待评估."""
    r = {'funcs': {}, 'unpacks': [], 'assigns': [], 'imports': [], 'used': set(),
         'loads': set(), 'gdecl': set(), 'selfcalls': [], 'classes': {}, 'libs': set(),
         'argdecls': [], 'libcalls': [], 'bar': [], 'cf': set(), 'b8calls': [],
         'b8self': [], 'stores': []}
    src = _layer(pkg_parts[1:])
    stack = [(tree, None, True)]       # (节点, 所属类, 是否模块级)
    while stack:
        node, cls, top = stack.pop()
        t = type(node)
        if t is ast.Name:
            r['used'].add(node.id)
            if isinstance(node.ctx, ast.Load):
                r['loads'].add(node.id)
            if node.id in DELETED_NAMES:
                rep(rel, node.lineno, 'A4', f'已删除功能残留引用: {node.id}')
        elif t is ast.Attribute:
            if node.attr in DELETED_NAMES:
                rep(rel, node.lineno, 'A4', f'已删除功能残留引用: {node.attr}')
            if isinstance(node.ctx, ast.Load):
                shared['attrs'].add(node.attr)
                v = node.value
                if isinstance(v, ast.Attribute) and v.attr == 'bar' and isinstance(v.value, ast.Name) and v.value.id == 'self':
                    r['bar'].append(node)            # B7: self.bar.<x> 访问
            elif isinstance(node.value, ast.Name) and node.value.id == 'self':
                r['stores'].append((node.lineno, node.attr))   # B9: 实例属性写
            if node.attr == 'app' and isinstance(node.value, ast.Name) and node.value.id == 'self':
                r['b8self'].append(node.lineno)      # B8: self.app. 出现行
            if node.attr == 'argtypes' and isinstance(node.value, ast.Attribute):
                r['argdecls'].append((_base(node.value.value), node.value.attr))
        elif t is ast.Call:
            r['cf'].add(id(node.func))
            r['b8calls'].append((node.lineno, node.func))
            f = node.func
            if isinstance(f, ast.Attribute):
                r['libcalls'].append((node.lineno, _base(f.value), f.attr))
                if isinstance(f.value, ast.Name) and f.value.id == 'self' and cls is not None:
                    r['selfcalls'].append((node.lineno, f.attr, cls))   # A1
            if isinstance(f, ast.Name) and f.id in ('getattr', 'hasattr', 'delattr', 'setattr') \
                    and len(node.args) > 1 and isinstance(getattr(node.args[1], 'value', None), str):
                shared['gstrs'].add(node.args[1].value)   # 动态 getattr(self,'x') 视为读取
        elif t in (ast.Import, ast.ImportFrom):
            r['imports'].append(node)
            for a in node.names:
                shared['imported'].add(a.asname or a.name.split('.')[0])
            if t is ast.ImportFrom and node.level and node.level - 1 > len(pkg_parts):
                rep(rel, node.lineno, 'B6', '相对导入越出包顶, 无法静态解析')
                continue   # 导入节点无待下钻子节点, 跳过入栈无碍
            if t is ast.ImportFrom:
                base = list(pkg_parts[:len(pkg_parts) - (node.level - 1)]) if node.level else []
                mods = [base + (node.module.split('.') if node.module else [])]
            else:
                mods = [a.name.split('.') for a in node.names]
            for parts in mods:
                if not parts:
                    continue
                if parts[0] != PKG:      # 外部库: 仅 core 层受限(§3)
                    if src == 'core' and parts[0] not in ('__future__', 'PIL') and parts[0] not in _STDLIB:
                        rep(rel, node.lineno, 'B6', f'core 层仅允许标准库/PIL, 却 import 了 {parts[0]}')
                    continue
                if _layer(parts[1:]) not in _ALLOWED[src]:
                    rep(rel, node.lineno, 'B6',
                        f'import 方向越界: {src} 层 → {_layer(parts[1:])} 层 ({".".join(parts)})')
        elif t is ast.AugAssign and isinstance(node.target, ast.Attribute):
            shared['aug'].add(node.target.attr)   # self.x += 1 属读改写, 非死状态
        elif t is ast.Global:
            r['gdecl'].update(node.names)
        elif t in (ast.FunctionDef, ast.AsyncFunctionDef) and top:
            r['funcs'][node.name] = node          # 模块级函数(A2)
        elif t is ast.ClassDef:
            r['classes'][node.name] = node
            if node.name == 'CoverBar':
                shared['coverbar'].append(node)
        elif t in (ast.Assign, ast.AnnAssign):
            r['assigns'].append((node, top))
            v = node.value
            if isinstance(v, ast.Call):           # A2: 元组解包 <- 函数调用
                tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
                r['unpacks'] += [(tg, v) for tg in tgts if isinstance(tg, ast.Tuple)
                                 and not any(isinstance(e, ast.Starred) for e in tg.elts)]
        for ch in ast.iter_child_nodes(node):     # 子节点上下文: 入类记类, 入函数离模块级
            stack.append((ch, node if t is ast.ClassDef else cls,
                          top and t not in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    return r
def _evaluate(r, rel, shared, rep, lines, do_b7, do_b8, bar_methods):
    """评估阶段: 全部文件扫描完后执行, 此时跨文件事实(shared)已齐, 可压低误报."""
    # ---- A1: self.<m>(...) 是否有定义(本类可调用名 + 同文件基类 + 白名单) ----
    defs = {}
    for c in r['classes'].values():
        defs[id(c)] = set(SELF_CALL_WHITELIST) | _cls_names(c)
        for b in c.bases:   # 外部基类(Gtk.Window 等)无法解析, 走白名单
            if isinstance(b, ast.Name) and b.id in r['classes']:
                defs[id(c)] |= _cls_names(r['classes'][b.id])
    for no, m, cls in r['selfcalls']:
        if m not in defs.get(id(cls), set()): rep(rel, no, 'A1', f'self.{m}(...) 无定义且不在白名单')
    # ---- A2: 解包元数 vs 同文件模块级函数 return 元数 ----
    for tgt, v in r['unpacks']:
        f = v.func
        ar = _arity(fn) if (fn := r['funcs'].get(f.id) if isinstance(f, ast.Name) else None) \
            is not None else set()
        if ar and len(tgt.elts) not in ar:
            rep(rel, tgt.lineno, 'A2', f'解包 {len(tgt.elts)} 元, 但 {f.id}() 返回 {sorted(ar)} 元')
    # ---- A3: 先归集 ctypes 库接收者, 再比对 argtypes 声明与调用 ----
    for node, top in r['assigns']:
        v = node.value
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and \
                v.func.attr in ('CDLL', 'WinDLL', 'PyDLL', 'OleDLL') and _base(v.func.value) == 'ctypes':
            tg = node.targets if isinstance(node, ast.Assign) else [node.target]
            r['libs'] |= {b for b in map(_base, tg) if b}   # lib = ctypes.CDLL(...)
    declared = {fn for recv, fn in r['argdecls'] if recv in r['libs']}
    for no, recv, fname in r['libcalls']:
        if recv in r['libs'] and fname not in declared:
            rep(rel, no, 'A3', f'ctypes 调用 {recv}.{fname}(...) 缺少 argtypes 声明')
    # ---- A5: 未使用 import(尊重 # noqa: F401) + 明显未用的模块级变量 ----
    for node in sorted(r['imports'], key=lambda n: n.lineno):
        line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ''
        if 'noqa' in line and 'F401' in line: continue    # 作者显式保留(# noqa: F401)
        for a in node.names:
            if a.name != '*' and (a.asname or a.name.split('.')[0]) not in r['used']:
                rep(rel, getattr(a, 'lineno', node.lineno), 'A5',
                    f'未使用的 import: {a.asname or a.name.split(".")[0]}')
    # 模块级变量: 「从未读取 + 非 dunder + 无 global 声明 + 未被他文件 import」全满足才报
    keep, seen = r['loads'] | r['gdecl'] | shared['imported'], set()
    for node, top in sorted((a for a in r['assigns'] if a[1]), key=lambda x: x[0].lineno):
        for tg in (node.targets if isinstance(node, ast.Assign) else [node.target]):
            if isinstance(tg, ast.Name) and tg.id not in keep and not tg.id.startswith('__') \
                    and tg.id not in seen:
                seen.add(tg.id)   # 同名多处赋值只报一次(try/except 分支)
                rep(rel, tg.lineno, 'A5', f'未使用的模块级变量: {tg.id} (赋值后从未读取)')
    # ---- B7: app.py 里 self.bar.<x> 只能是调用 CoverBar 方法(D2), 读写属性都报 ----
    if do_b7:
        for n in r['bar']:
            if id(n) in r['cf']:
                if n.attr not in bar_methods:
                    rep(rel, n.lineno, 'B7', f'调用了 CoverBar 未定义的方法: bar.{n.attr}(...)')
            else:
                act = {ast.Store: '写入', ast.Load: '读取', ast.Del: '删除'}.get(type(n.ctx), '访问')
                rep(rel, n.lineno, 'B7', f'直接{act} CoverBar 属性(D2: 应改为方法): bar.{n.attr}')
    # ---- B8: x11/与顶层 render.py 禁 self.app.(D4)与 save_config(D3, 识别 as 别名) ----
    if do_b8:
        sc, seen = {'save_config'}, set()
        for node in r['imports']:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith('config'):
                sc |= {a.asname or a.name for a in node.names if a.name == 'save_config'}
        hits = [(no, '渲染线程/x11 层出现 self.app.(D4: 数据流必须单向)') for no in r['b8self']]
        hits += [(no, 'save_config 调用(D3: 配置持久化只允许在 app 层)') for no, f in r['b8calls']
                 if isinstance(f, ast.Name) and f.id in sc or isinstance(f, ast.Attribute) and f.attr == 'save_config']
        for no, msg in sorted(hits):   # 一行多处只报一次
            if no not in seen:
                seen.add(no)
                rep(rel, no, 'B8', msg)
    # ---- B9: 实例属性赋值后从未被读取(全工程无任何读取才报, 保守) ----
    done = set()
    for no, attr in sorted(r['stores']):
        if attr not in shared['read'] and attr not in done:
            done.add(attr)
            rep(rel, no, 'B9', f'实例属性 self.{attr} 赋值后从未被读取(疑似死状态)')


def main():
    report_only = '--report-only' in sys.argv[1:]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg, aux, trees, lines = [], [], {}, {}
    for dirpath, dirnames, filenames in os.walk(root):   # 收集: 包内 + 辅助文件(run.py/tools)
        dirnames[:] = sorted(d for d in dirnames if d not in ('.git', '.deps', '__pycache__'))
        for fn in sorted(filenames):
            if fn.endswith('.py'):
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                (pkg if rel.split(os.sep)[0] == PKG else aux).append(rel)
    violations, warnings = [], []

    def rep(rel, no, code, msg):
        violations.append((rel, no, code, msg))

    for rel in sorted(pkg) + sorted(aux):   # 解析; 被检文件失败按违规上报, 辅助文件仅提示
        try:
            with open(os.path.join(root, rel), 'r', encoding='utf-8') as f:
                src = f.read()
            lines[rel], trees[rel] = src.splitlines(), ast.parse(src, filename=rel)
        except (OSError, SyntaxError, ValueError, RecursionError) as e:
            if rel.split(os.sep)[0] == PKG:
                rep(rel, getattr(e, 'lineno', 1) or 1, 'PARSE', f'解析失败, 无法静态检查: {e}')
            else:
                warnings.append(f'{rel}: 解析失败(辅助文件, 仅提示): {e}')

    shared = {'attrs': set(), 'gstrs': set(), 'aug': set(), 'imported': set(), 'coverbar': []}
    scans = []
    for rel in (x for x in sorted(pkg) if x in trees):   # 一趟扫描: 上报 A4/B6, 收集事实
        parts = tuple(rel.split(os.sep)[:-1])   # 含 PKG 的包路径分量
        try:
            scans.append((rel, parts, _scan(trees[rel], rel, parts, shared, rep)))
        except Exception as e:   # 单文件扫描故障不许让工具崩溃
            warnings.append(f'{rel}: 检查器扫描异常(已降级): {e!r}')
    shared['read'] = shared['attrs'] | shared['gstrs'] | shared['aug']
    bar_methods = set().union(*({f.name for f in c.body if isinstance(f, ast.FunctionDef)}
                                for c in shared['coverbar'])) if shared['coverbar'] else set()
    if not shared['coverbar']: warnings.append('未找到 CoverBar 类, B7 跳过(类改名后需同步本工具)')
    for rel, parts, r in scans:  # 统一评估: 此时跨文件事实已齐
        try:
            _evaluate(r, rel, shared, rep, lines.get(rel, []),
                      rel == os.path.join(PKG, 'app.py'), len(parts) > 1 and parts[1] == 'x11'
                      or rel == os.path.join(PKG, 'render.py'), bar_methods)
        except Exception as e:
            warnings.append(f'{rel}: 检查器评估异常(已降级): {e!r}')

    for rel, no, code, msg in sorted(violations):
        print(f'{rel}:{no} [{code}] {msg}')
    for w in warnings:
        print(f'[警告] {w}')
    cnt = {}
    for _, _, c, _ in violations:
        cnt[c] = cnt.get(c, 0) + 1
    a_n = sum(v for k, v in cnt.items() if k[0] == 'A'); b_n = sum(v for k, v in cnt.items() if k[0] == 'B')
    detail = ' '.join(f'{k}={cnt[k]}' for k in sorted(cnt))
    tail = '--report-only, 退出码 0' if report_only else f'退出码 {1 if violations else 0}'
    print(f'汇总: A 类 {a_n} / B 类 {b_n} / 警告 {len(warnings)} ({detail}) [{tail}]')
    return 0 if report_only else (1 if violations else 0)


if __name__ == '__main__': sys.exit(main())
