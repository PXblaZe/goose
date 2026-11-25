"""`extern fn ... calls "sym"`: C calling an export back before the extern
returns. Callbacks that allocate leave their callers' locals alone, the
checker judges what a callback does to globals as a call the wrapper makes,
recursion through C follows §7.8, and an export C calls back without a
clause naming it is still refused (§7.11)."""
from pathlib import Path

import toolchain as tc

HEADER = r'''
__attribute__((weak)) int64_t on_callback(int64_t n);
__attribute__((weak)) int64_t on_outer(int64_t n);
__attribute__((weak)) int64_t on_inner(int64_t n);
/* C calling the program back before it returns, as a toolkit or qsort does. */
static int64_t call_back(int64_t n) { return on_callback ? on_callback(n) : -1; }
static int64_t call_back_unsaid(int64_t n) { return on_callback ? on_callback(n) : -1; }
static int64_t outer(int64_t n) { return on_outer ? on_outer(n) : -1; }
static int64_t inner(int64_t n) { return on_inner ? on_inner(n) : -1; }
'''

# Programs that run, and what they print.
RUNS = {
    "locals": ('''
extern fn call_back(n: i64) -> i64 calls "on_callback";
export "on_callback" fn on_callback(n: i64) -> i64 {
    let s = str("callback temporary number ", n, " with some padding to fill the stack");
    return str(s, s, s).len;
}
fn main() {
    let keys: const u8[:][] = ["alpha", "beta", "gamma", "delta"];
    let local = str("main local ", 42);
    var total = 0;
    for k in keys {
        total += call_back(k.len);
        print(k, " ", local);
    }
    print("total ", total);
}
''', "alpha main local 42\nbeta main local 42\ngamma main local 42\ndelta main local 42\n"
     "total 756\n"),
    "deep": ('''
extern fn outer(n: i64) -> i64 calls "on_outer";
extern fn inner(n: i64) -> i64 calls "on_inner";
var log: i64[>..] = [];
export "on_inner" fn on_inner(n: i64) -> i64 {
    let s = str("inner temporary ", n, " with padding to fill the stack");
    log.push(n);
    return s.len;
}
export "on_outer" fn on_outer(n: i64) -> i64 {
    let s = str("outer temporary ", n, " with padding to fill the stack");
    log.push(n);
    return s.len + inner(n + 1);
}
fn middle(k: const u8[:]) -> i64 {
    let mine = str("middle local ", k);
    var parts: u8[>..] = [];
    for c in k { parts.push(c); }
    let r = outer(k.len * 10);
    assert(mine == str("middle local ", k) && parts == k);
    return r;
}
fn main() {
    let keys: const u8[:][] = ["alpha", "beta", "gamma"];
    let local = str("main local ", 42);
    var total = 0;
    for k in keys { total += middle(k); }
    print("total ", total, ", logged ", log, ", ", local);
}
''', "total 294, logged [50, 51, 40, 41, 50, 51], main local 42\n"),
    "recursion": ('''
extern recursive fn outer(n: i64) -> i64 calls "on_outer";
export "on_outer" fn on_outer(n: i64) -> i64 {
    if n <= 0 { return 0; }
    return 1 + outer(n - 1);
}
fn main() {
    let local = str("main local ", 42);
    print(outer(5), " ", local);
}
''', "5 main local 42\n"),
    "globals": ('''
extern fn call_back(n: i64) -> i64 calls "on_callback";
var items: i64[>..<] = [];
var calls = 0;
export "on_callback" fn on_callback(n: i64) -> i64 {
    calls++;
    items.clear();
    for i in 100 { items.push(i * 1000); }
    return n;
}
fn main() {
    for i in 10 { items.push(i); }
    let before = items.len;
    let r = call_back(1);
    let view = items[..];
    print(before, " ", view.len, " ", view[3], " ", r, " ", calls);
}
''', "10 100 3000 1 1\n"),
}

# A callback through an extern without a clause naming it: stopped on entry.
GUARD = ('''
extern fn call_back_unsaid(n: i64) -> i64;
export "on_callback" fn on_callback(n: i64) -> i64 { return str("temporary ", n).len; }
fn main() {
    let local = str("main local ", 42);
    print(call_back_unsaid(5), " ", local);
}
''', 'export fn on_callback was called by C that Goose code called, before it returned')

# Programs the checker refuses, and the error each must report.
ERRORS = {
    "view_into_shrunk_global": ('''
extern fn call_back(n: i64) -> i64 calls "on_callback";
var items: i64[>..<] = [];
export "on_callback" fn on_callback(n: i64) -> i64 { items.clear(); return n; }
fn main() {
    for i in 10 { items.push(i); }
    let view = items[..];
    let r = call_back(1);
    print(view[3], " ", r);
}
''', "cannot call call_back, which shrinks items while view"),
    "argument_into_shrunk_global": ('''
extern fn sum_back(xs: const i64[:]) -> i64 calls "on_change";
var items: i64[>..<] = [];
export "on_change" fn on_change(n: i64) -> i64 { items.clear(); return n; }
fn main() {
    for i in 10 { items.push(i); }
    print(sum_back(items[..]));
}
''', "cannot call sum_back: it shrinks items while xs is still used"),
    "undeclared_recursion": ('''
extern fn outer(n: i64) -> i64 calls "on_outer";
export "on_outer" fn on_outer(n: i64) -> i64 { return if n <= 0 { 0 } else { outer(n - 1) }; }
fn main() { print(outer(3)); }
''', "which is not declared `recursive fn`"),
    "local_across_recursion": ('''
extern recursive fn outer(n: i64) -> i64 calls "on_outer";
export "on_outer" fn on_outer(n: i64) -> i64 {
    if n <= 0 { return 0; }
    let s = str("held across the call ", n);
    return s.len + outer(n - 1);
}
fn main() { print(outer(3)); }
''', "while non-fixed-size local s"),
}


def check_extern_calls(exe, cc, base, *, jit, runtime, extra=()):
    base = (Path(base) / "extern-calls").resolve()
    base.mkdir(parents=True, exist_ok=True)
    header = base / "callbacks.h"
    tc.write_text(header, HEADER)

    def build_and_run(name, source, level, use_jit):
        flags = [f"-O{level}", "--include", header]
        if use_jit:
            return tc.run_capture([exe, *flags, "--jit", source])
        cfile = base / f"{name}-{level}.c"
        code, out, err = tc.run_capture([exe, *flags, "-o", cfile, source])
        if code:
            raise RuntimeError(f"{name} -O{level}: compiling failed:\n{out}{err}")
        binary = cfile.with_suffix(tc.EXE_SUFFIX)
        ok, log = cc.compile(cfile, binary, opt=level if not extra else 1, extra=extra,
                             runtime=runtime)
        if not ok:
            raise RuntimeError(f"{name} -O{level}:\n{log}")
        return tc.run_capture([binary])

    backends = ([False] if cc else []) + ([True] if jit else [])
    for name, (text, want) in RUNS.items():
        source = base / f"{name}.goose"
        tc.write_text(source, text)
        for use_jit in backends:
            for level in (0, 2):
                code, out, err = build_and_run(name, source, level, use_jit)
                if code or out != want:
                    raise RuntimeError(f"{name} -O{level}{' jit' if use_jit else ''}: exit {code}, "
                                       f"printed:\n{out}{err}\nwanted:\n{want}")
    text, want = GUARD
    source = base / "guard.goose"
    tc.write_text(source, text)
    for use_jit in backends:
        code, out, err = build_and_run("guard", source, 0, use_jit)
        if code == 0 or want not in err or "main local" in out:
            raise RuntimeError(f"guard{' jit' if use_jit else ''}: exit {code}, printed:\n{out}{err}")
    for name, (text, want) in ERRORS.items():
        source = base / f"{name}.goose"
        tc.write_text(source, text)
        code, out, err = tc.run_capture([exe, "--include", header, "--check", source])
        if code == 0 or want not in out + err:
            raise RuntimeError(f"{name}: wanted an error containing {want!r}, got exit {code}:\n"
                               f"{out}{err}")
