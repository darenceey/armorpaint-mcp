# minic dialect & ArmorPaint plugin API — builder's reference

**Provenance.** Everything below was read out of the local clone at `E:\Apps\ArmorPaint\src`
(ArmorPaint 1.0, `906418acc600132fa927876d208eb452dc5a0967`) on 2026-09-08. No web documentation was
consulted; public docs describe the pre-2025 Haxe/Kha build and are wrong for this tree.

Primary sources, in order of authority:

| File | What it settles |
|---|---|
| `base/sources/libs/minic.c` (2431 ln) | the grammar, every cap, every silent failure |
| `base/sources/libs/minic.h` (214 ln) | the caps as constants, the value model |
| `paint/sources/minic_api_list.h` (595 ln, **529 active `X` entries**) | the complete callable surface |
| `paint/sources/minic_api.c` | builtins, struct/enum registration — i.e. what is *readable* |
| `paint/sources/plugin.c`, `ui/ui_base.c`, `ui/tab_plugins.c`, `base/sources/iron.h` | lifecycle & gating |
| `paint/assets/plugins/*.c` (8) + `plugins/dev/{test,blocks}.c` (2) | the dialect **by example** |

Rule of thumb used throughout: **if it is not in `minic_api_list.h` and not a struct field registered
in `minic_api.c`, it does not exist for a plugin.** There is no escape hatch.

---

# Part 0 — How a plugin actually runs

You need this before Part 1, because three of the dialect's sharpest edges are lifecycle edges.

## 0.1 Load

`plugin_start(name)` (`paint/sources/plugin.c:13`):

```c
char     *file = string("plugins/%s", plugin);
buffer_t *blob = data_get_blob(file);
minic_ctx_t *ctx = minic_eval_named(sys_buffer_to_string(blob), plugin);
data_delete_blob(file);
plugin_t *p = any_map_get(g_plugins, plugin);
if (p == NULL) { p = plugin_create(); }   // script did not call plugin_create()
p->ctx = ctx;
```

`minic_eval_named` runs three passes (`minic.c:2073`):

1. **struct/enum scan** — a raw token sweep of the whole file registering `struct`/`typedef struct`/
   `enum`/`typedef enum` definitions (`minic_register_structs`, `minic.c:1808`).
2. **function & global registration** — walks top level, registering functions and *evaluating global
   initialisers in source order*, and **stops dead at `main`** (`minic_register_funcs`, `minic.c:1947`,
   `break` at `:2051`).
3. **execution** — `minic_parse_block` runs `main`'s body as the top-level block (`minic.c:2122`).

Consequences you must design around:

- **`main` must be the last function in the file.** Anything after it is never registered and calling
  it errors `unknown function`. All 8 bundled plugins put `main` last.
- **Global initialisers run during pass 2**, before `main`. They may only reference things already
  defined above them.
- `plugin_create()` returns the `plugin_t*` you pass to the three `plugin_notify_on_*` binders.

## 0.2 The three callbacks

| Callback | Bound by | Invoked from | Frequency |
|---|---|---|---|
| `on_ui` | `plugin_notify_on_ui(plugin, fn)` | `ui/tab_plugins.c:27` | **only while the Plugins tab is being drawn** |
| `on_update` | `plugin_notify_on_update(plugin, fn)` | `ui/ui_base.c:225` | every frame the app is not gated |
| `on_delete` | `plugin_notify_on_delete(plugin, fn)` | `plugin.c:36` on `plugin_stop` | once |

`on_ui` firing only inside the Plugins tab matters: a bridge enable/disable toggle drawn in `on_ui`
is invisible unless the user is on that tab. Do not put state the server depends on behind `on_ui`.

`on_update` is dispatched from `ui_base_update` **outside** its `if (base_ui_enabled)` block, so it
still runs when UI input is disabled.

There is also `script_notify_on_update(fn)` / `script_notify_on_next_frame(fn)`. Both are **process-wide
singletons** (`minic_impl.c:117-135`) — a second call replaces the first, and the Scripts tab uses the
same slot. Use `plugin_notify_on_update` for a bridge. `script_notify_on_next_frame` is the right tool
for a one-shot "do this next frame" continuation (`dev/test.c` drives its whole 18-step suite with it).

## 0.3 The idle / background gate — resolved

`base/sources/iron.h:215`:

```c
void _update() {
#ifdef IRON_WINDOWS
    if (in_background && ++paused_frames > 3) { Sleep(1); return; }
#endif
#ifdef IDLE_SLEEP
    ...
    if (++paused_frames > start_sleep && !input_down) { Sleep(1); return; }   // start_sleep = 120
#endif
    string_tmp_reset();
    iron_net_update();
    iron_update();            // <- dispatches ui_base_update -> your on_update
    ...
}
```

`iron_delay_idle_sleep()` is one line (`iron.h:1011`): `paused_frames = 0;`.

**Both gates share the single `paused_frames` counter.** Trace one frame with the reset called inside
`on_update`: enter at 0 → background gate `++` → 1 (not > 3) → idle gate `++` → 2 (not > 120) →
`iron_update()` → `on_update` → `paused_frames = 0`. Steady state is 0→2→0. Neither gate ever trips,
**including the Windows background gate.**

This answers the open question flagged in `PROTOCOL.md` §"The idle gate": *yes*, calling
`iron_delay_idle_sleep()` every frame also defeats the background gate, because it is not a separate
counter. Live control of a backgrounded ArmorPaint is possible.

The precondition is exact: the background gate tolerates **at most 3 consecutive missed frames**. So
the call must be reached unconditionally on every frame you want to stay alive — before any
poll-interval early return. (`make_tilesheet.c:26` puts it after its own `if (!baking) return;`
precisely because it is *fine* with going idle when not baking. A bridge that wants to stay awake is
not, so put the reset above the throttle but below the enable check.)

`IDLE_SLEEP` is on for ArmorPaint (`paint/project.js:18`). The background gate is `#ifdef IRON_WINDOWS`
only.

---

# Part 1 — The minic dialect

minic is a **tree-walking re-lexing interpreter**, not a compiler. It re-reads source text on every
loop iteration and every function call. This explains most of its cost model and several of its bugs.

## 1.1 Program shape

```c
#include "global.h"          // a NO-OP at runtime (see 1.2), keep it for editor tooling

void *plugin;                // globals: any top-level decl that is not a function
ui_handle_t *h0;
int counter = 0;             // initialiser evaluated during pass 2

void helper(int x) { ... }   // functions, in any order, ALL BEFORE main

void main() {                // must be LAST; its body is the top-level script block
    plugin = plugin_create();
    plugin_notify_on_update(plugin, on_update);
}
```

**32 functions, hard.** `minic.c:2099` sets `e->func_cap = 32`; `minic.c:2054`:

```c
if (e->func_count < e->func_cap) { e->funcs[e->func_count++] = fn; }
```

No `else`, no diagnostic. **The 33rd function is silently dropped** and any call to it fails at
runtime with `unknown function 'x'`. `main` does not count against the 32 (it `break`s out of the
registration loop before being added, `minic.c:2051`), so the real budget is **32 non-`main`
functions**.

⇒ **One dispatcher with an `if / else if` chain, never one function per operation.** `dev/test.c:325`
is the upstream proof of the pattern:

```c
void run_step(int s) {
    if (s == 0) step_tools();
    else if (s == 1) step_paint_brush();
    else if (s == 2) step_paint_eraser();
    ...
}
```

## 1.2 Preprocessor — there is none

`minic.c:131`:

```c
if ((l->src[l->pos] == '/' && l->src[l->pos + 1] == '/') || l->src[l->pos] == '#') {
    while (... != '\n') l->pos++;   // skip to end of line
    continue;
}
```

**Every line beginning with `#` is treated as a line comment.** So:

- `#include "global.h"` does nothing at runtime. It exists so an IDE can resolve types; the file itself
  (`paint/sources/global.h`, 9 lines) just pulls in `iron.h`, `enums.h`, `functions.h`, `globals.h`,
  `types.h`. Every bundled plugin except `dev/blocks.c` starts with it. Keep it — it costs nothing and
  buys you completion. It brings in **nothing at runtime**; the real API surface is registered natively.
- **`#define` does not work.** A `#define TILE 256` is skipped, and `TILE` then becomes an *undefined
  identifier*, which silently evaluates to `0` (see 1.12). This is the single most dangerous
  interaction in the language. Use `int TILE = 256;` at global scope, or an `enum`.
- `#ifdef` / `#if` are likewise ignored — **both branches of a conditional block are compiled**.

Comments: `//` and `/* ... */` both work (`minic.c:131,137`).

## 1.3 Types

The value model is `minic_val_t` (`minic.h:32`): a tag plus a `union { int i; float f; void *p; }`.
Everything is one of **three storage classes**:

| Storage | Written as | Notes |
|---|---|---|
| `MINIC_T_INT` | `int`, `char`, `bool` | `bool` and `char` are ints; `true`/`false` lex as `1`/`0` (`minic.c:252`) |
| `MINIC_T_FLOAT` | `float`, `double` | `double` is an alias for `float` (`minic.c:406`) |
| `MINIC_T_PTR` | `void *`, `char *`, `T *` | carries a `deref_type` stamped at declaration |

- **No `unsigned`, `long`, `short`, `const`, `static`, `extern`, `volatile`, `size_t`, `int64_t`.**
  None are keywords (`minic.c:74`); each becomes an identifier and derails the parse.
- `char *` is a real C string (NUL-terminated, native memory). Indexing `s[i]` yields the byte as an
  int (`minic_ptr_index_get`, `minic.c:565`).
- Pointer element type is decided **at declaration**, not by the value. `char *p = malloc(16);` makes
  `p[i]` a byte read; `float *p = ...` makes it a float read. Get the declaration wrong and you read
  the right address with the wrong stride, silently.
- **A pointer is only "a struct" if you declare it with a registered struct type name.**
  `void *c = script_get_context(); c->tool` errors with `'c' is not a struct` (`minic.c:1039`).
  `context_t *c = script_get_context(); c->tool` works. Same for parameters: `void set_block(object_t *o, ...)`
  in `dev/blocks.c:95` is what makes `o->transform` legal inside it.

Number literals: decimal, `0x` hex (`minic.c:155`), a trailing `.` fraction or `f` suffix makes it a
float. `1 / 2` is integer division (`0`); `1.0 / 2.0` is `0.5` — `hello_world.c:22` writes `1.0 / 2.0`
for exactly this reason. **Division and modulo by zero return `0`, they do not trap** (`minic.c:913`).

## 1.4 Declarations

```c
int   i = 0;                 // scalar, initialiser optional (defaults 0 / NULL)
float f;
char *s = "hello";
void *p = NULL;              // NULL is not defined anywhere; it resolves to integer 0 (see 1.12)
                             // and coerces correctly on assignment to a pointer variable
ui_handle_t *h = ui_handle_create();      // registered struct -> '->' enabled
gpu_texture_t *t = NULL;                  // NOT registered -> opaque handle, '->' illegal
any_array_t *a = any_array_create(0);
int board[64];               // fixed array — SEE THE WARNING BELOW
```

Multiple declarators are **not** supported: `int a, b;` declares `a` and then mis-parses. One name per
statement.

### ⚠ Fixed-size C arrays leak permanently

`minic_arr_decl` (`minic.c:548`) allocates out of a **512-element pool shared by the whole context**
(`minic.c:2096`), via a counter that is **never reset** — not on function return, not on frame
boundaries:

```c
a->offset = *e->arr_data_used;
*e->arr_data_used += count;      // monotonic, forever
```

`minic_call_in_ctx` restores the arena watermark after each host→script entry but does **not** restore
`arr_data_used`. And there is no bounds check against 512 — overflow writes past the pool into the rest
of the arena.

⇒ **Never declare `T name[N];` inside a function that runs more than a handful of times.** A 10-element
array in `on_update` exhausts the pool in ~51 frames and then corrupts memory. Use module-level globals,
or the heap array constructors (`i32_array_create`, `f32_array_create`, `any_array_create`, …) which are
real allocations you control. `dev/blocks.c` is careful about this: every array is a global built once
with `i32_array_create` / `any_array_create` in `main`.

Fixed arrays are also capped at **32 per scope** and are subscript-only — you cannot take their length.

## 1.5 Control flow

| Construct | Supported | Evidence / caveat |
|---|---|---|
| `if` / `else if` / `else` | ✅ | `minic.c:1581`; `dev/test.c:325` chains 18 of them |
| `while` | ✅ | `minic.c:1721`; `dev/blocks.c:64` |
| `for (init; cond; incr)` | ✅ **restricted** | `minic.c:1612`, see below |
| `break`, `continue` | ✅ | `minic.c:1707,1714`; `dev/blocks.c:41` uses `continue` |
| `return expr;` / bare `return;` | ✅ | bare return yields 0; `autosave.c:17` |
| braceless single-statement bodies | ✅ | `dev/blocks.c:157` `if (keyboard_started("left")) try_move(-1);` |
| `switch` / `case` / `default` | ❌ | not keywords (`minic.c:74`); parses as an identifier then errors |
| `do { } while` | ❌ | no `do` keyword |
| `goto`, labels | ❌ | |
| ternary `a ? b : c` | ❌ **and silent** | `?` and `:` are not in the operator table; `minic.c:276` *skips unknown characters*, so `a ? b : c` lexes as `a b c` and mis-parses without a diagnostic |

### `for` is narrower than C

`minic.c:1612-1685`. The init clause must be exactly `[type] IDENT = expr` — so:

- `for (;;)` ❌, `for (int i = 0, j = 0; …)` ❌, `for (i += 1; …)` ❌.
- The increment clause accepts only `++i`, `--i`, `i++`, `i--`, `i op= expr`, `i = expr`
  (`minic_parse_for_incr`, `minic.c:1322`) — **one variable, no comma**.
- The loop variable is created with `minic_var_set`, not a fresh declaration, so it **leaks into the
  enclosing scope** and inherits the type of an existing same-named variable if one exists.
- The condition and body are **re-lexed from source on every iteration**. Correct, but it means string
  literals inside a loop body allocate arena per iteration (see 1.11).

Every bundled example uses the same safe shape: `for (int i = 0; i < n; ++i)`.

## 1.6 Expressions

### Precedence (loosest → tightest), from the parser chain

```
cond    : bitor  (('&&' | '||') bitor)*        minic.c:1210
bitor   : bitxor ('|' bitxor)*                 minic.c:1198
bitxor  : bitand ('^' bitand)*                 minic.c:1186
bitand  : cmp    ('&' cmp)*                    minic.c:1174
cmp     : shift  (('=='|'!='|'<'|'>'|'<='|'>=') shift)?     minic.c:1139  -- AT MOST ONE
shift   : expr   (('<<' | '>>') expr)*         minic.c:1126
expr    : term   (('+' | '-') term)*           minic.c:1115
term    : primary (('*' | '/' | '%') primary)* minic.c:1104
primary : '&'x | '*'x | '-'x | '!'x | '~'x | '++'x | '--'x | literal | ident[...] | '(' expr ')'
```

Two divergences from C that *will* bite:

1. **`&&` and `||` sit at the same precedence and are left-associative.** `a || b && c` parses as
   `(a || b) && c`, not C's `a || (b && c)`. **Always parenthesise mixed `&&`/`||`.**
2. **Comparison is non-associative and single.** `a < b < c` is not chainable (harmless), but it also
   means you cannot write `x == y == z`.

### ⚠ `&&` and `||` do NOT short-circuit

`minic.c:1215`:

```c
int vi = minic_val_is_true(v);
int ri = minic_val_is_true(minic_parse_bitor(e));   // RHS always evaluated
v = minic_val_int(op == TOK_AND ? (vi && ri) : (vi || ri));
```

Both sides are evaluated, always. Therefore:

```c
if (p != NULL && p->field > 0) { ... }     // ☠ DEREFERENCES p WHEN IT IS NULL
```

minic has no exceptions and no null guard on native struct reads. Use nested `if`s — which is exactly
why `dev/test.c:112-121` writes four nested `if (x != NULL)` blocks instead of one `&&` chain:

```c
if (noise != NULL) {
    if (rgb != NULL) {
        if (mix != NULL) {
            if (out != NULL) { ok = 1; }
        }
    }
}
```

### Not available in expressions

| | Status |
|---|---|
| casts `(int)x`, `(float)x` | ❌ **parse error** (`expected ')'`). `import_stl.c:48-53` has its cast lines commented out and replaced with `buffer_set_i16` calls — that is the workaround. To convert, assign through a typed variable: `int n = some_float;` |
| assignment as an expression | ❌ `if ((v = f()) != NULL)` and `a = b = c;` are illegal; assignment is a statement only |
| postfix `i++` inside an expression | ❌ **and silently corrupting.** `minic_parse_primary` handles **prefix only** (`minic.c:993`). `f(i++)` leaves the `++` in the token stream and the argument loop reparses it as a prefix increment of the *next* token. Use `++i`, or increment on its own line. All examples use `++i` in `for`. |
| comma operator | ❌ |
| `sizeof(expr)` | ❌ — only `sizeof(TypeName)` (`minic.c:1014`), and it returns `0` for any type that is not a **host-registered native struct** |
| adjacent string literal concatenation `"a" "b"` | ❌ — use `string("%s%s", a, b)` |
| compound assignment to an array or struct-array element | ❌ `a->buffer[i] += 1;` is a parse error (`minic.c:1512,1569` accept only `=`). Write `a->buffer[i] = a->buffer[i] + 1;` — `dev/blocks.c:75` does exactly that |

### Available

`+ - * / %` (with `%` = `fmod` on floats), `<< >> & ^ | ~`, `! && ||`, all six comparisons, unary `-`,
prefix `++`/`--`, `&x` (address-of), `*p` (deref), `p[i]`, `s.f`, `p->f` with arbitrary chaining
(`node->inputs->buffer[0]`, `hello_node.c:10`), function calls, parenthesised sub-expressions.

### Call arity is not checked

`minic_arg_i/f/p` (`minic.c:2413-2423`) return `0` / `NULL` for any index `>= argc`, and
`minic_parse_call` drops arguments past `MINIC_MAX_PARAMS` (20). So **too few arguments silently pass
zeros and too many are silently discarded** — for host bindings *and* for your own functions.

This is deliberate and used by upstream: `ui_panel` is declared with five parameters
(`minic_api_list.h:125`) but `autosave.c:9` calls `ui_panel(h1, "Auto Save")` with two, letting the
last three default to `0`, while `converter.c:24` passes all five. Convenient — and it means a
mistyped call site never reports anything.

**Pointer arithmetic is byte-wise and lossy.** `minic_arith` (`minic.c:888`) promotes any
pointer-involving expression to `MINIC_T_PTR` and computes through a `double`: `p + 1` advances **one
byte**, not one element, and the address round-trips through 53 bits of mantissa. Never do pointer
arithmetic; index with `p[i]`, which is element-typed and correct.

## 1.7 Statements

Legal statement forms (`minic_parse_stmt`, `minic.c:1354`):

```c
type name [= expr];          // declaration
type *name [= expr];         // pointer declaration
type name[N];                // array declaration
StructName *p [= expr];      // struct-pointer declaration (enables '->')
StructName v;                // struct value (boxed, arena-allocated)
name = expr;                 // assignment
name op= expr;               // += -= *= /= %= <<= >>= &= |= ^=
name++;  name--;  ++name;  --name;
name[i] = expr;              // '=' only
p->f = expr;   p->f op= expr;   p->f++;      // chained: a->b->c = expr works
p->f[i] = expr;              // '=' only
*p = expr;   *p op= expr;   *p++;
f(args);                     // call as a statement
if / while / for / break / continue / return
{ ... }                      // block; locals are popped on exit (minic.c:1777)
```

`typedef` inside a function body is skipped (`minic.c:1356`).

## 1.8 Structs, typedefs, enums in plugin code

**All three are allowed**, discovered by the pass-0 sweep (`minic.c:1808`).

```c
typedef struct {
    int   kind;
    char *name;
    float weight;
} my_entry_t;

typedef enum { MY_OP_PING, MY_OP_SAVE, MY_OP_EXPORT } my_op_t;

void f() {
    my_entry_t e;          // value: arena-allocated boxed fields
    e.kind = MY_OP_SAVE;   // works
    my_entry_t *p = &e;    // pointer to it
}
```

Precise semantics and traps:

- A script-defined struct is **script-layout (boxed)**: instances are an array of `minic_val_t`, one per
  field, and fields are effectively **dynamically typed** — whatever you store is what you read back
  (`minic.c:773`). Field *types* in your declaration are ignored; only the *names and order* matter.
- `sizeof(my_entry_t)` returns **0** (`minic_struct_begin` is only ever called by the host, so script
  structs have `size == 0`). Therefore `malloc(sizeof(my_entry_t))` allocates nothing. **Declare struct
  values (`my_entry_t e;`), never `malloc` them.** By contrast `calloc(1, sizeof(ui_node_t))` *is*
  correct and is what `hello_node.c:35` does, because `ui_node_t` is a host-registered native struct
  with a real size.
- A script struct **cannot overlay native memory.** `minic_struct_field_get_base` (`minic.c:748`) takes
  the boxed path for any non-native struct, so pointing your own struct at a `context_t*` reads garbage.
  There is no way to reach an unregistered host type.
- Caps: **32 fields per struct**, **64 structs per context total** — and the ~40 host structs are seeded
  into every context first (`minic.c:2107`), leaving roughly **20 slots** for your own.
- **Enum constants are registered in a process-wide table** (`minic_enum_const_add`, `minic.c:2221`),
  512 max, shared across every plugin and script, **first definition wins**. Prefix your constants
  (`MYBRIDGE_OP_PING`) or you will collide with another plugin or with the host's `TOOL_TYPE_*`,
  `UI_ALIGN_*`, `GPU_TEXTURE_FORMAT_*`, `PHYSICS_SHAPE_*`, `UI_STATE_*`, `UI_LAYOUT_*`.
- `typedef enum {...} name_t;` also registers `name_t` as an int-typedef so you can declare variables
  of it. Plain `typedef int foo_t;` does **not** work (no body → skipped).

## 1.9 Function pointers and callbacks

Function pointers exist, and they are simple: **pass the bare function name**. No `&`, no cast, no
typedef, no declared signature.

`minic_parse_primary` (`minic.c:1078`): an identifier that is not a variable but *is* a registered
script function evaluates to a pointer to it.

```c
void on_ui() { ... }
void on_update() { ... }

void main() {
    plugin = plugin_create();
    plugin_notify_on_ui(plugin, on_ui);          // autosave.c:31
    plugin_notify_on_update(plugin, on_update);  // autosave.c:32
}
```

On the host side the parameter is typed `p` (raw pointer) in the binding table — e.g.
`X2(plugin_notify_on_ui, "v(p:plugin_t plugin,p f)", v, p, p)`. The host later invokes it with
`minic_call_fn(fn, args, argc)`.

**The host fixes the callback signature; you must match it.** The complete set of callback shapes in
this tree:

| Binding | Your function must be |
|---|---|
| `plugin_notify_on_ui` / `_on_update` / `_on_delete` | `void f()` |
| `script_notify_on_update` / `script_notify_on_next_frame` | `void f()` |
| `script_timer(delay, fn)` | `void f()` |
| `ui_files_show2(filters, is_save, multi, files_done)` | `void f(char *path)` — `converter.c:6` |
| `plugin_register_mesh(fmt, fn)` | `void *f(char *path)` returning a `raw_mesh_t*` — `import_stl.c:5` |
| `plugin_register_texture` / `plugin_register_text` | `void *f(char *path)` (same shape) |
| `plugin_material_custom_nodes_set(type, fn)` | `char *f(ui_node_t *node, char *socket_name)` — `hello_node.c:8` |
| `plugin_brush_custom_nodes_set(type, fn)` | `float f(logic_node_t *node, int from)` — `hello_node_brush.c:8` |
| `context_set_viewport_shader(fn)` | `void f(void *shader)` — `viewport_celshade.c:6` |
| `array_sort(ar, cmp)` / `i32_array_sort` | comparator (untested in any bundled example) |
| `trait_point_and_click_controller_walk_to(x, y, on_arrive)` | `void f()` |
| `file_download_to(url, dst, done, size)` | `void f()` |

`context_set_viewport_shader(NULL)` unbinds (`viewport_celshade.c:15`).

Note `logic_node_t` is **not a registered struct** — which is why `hello_node_brush.c:9` has its
`node->inputs->buffer[0]->get(0)` line commented out. Brush custom nodes cannot read their inputs.

## 1.10 Strings

There is no `+` for strings and no `strcmp`. The whole surface:

| Need | Use |
|---|---|
| format / concatenate | `string(fmt, ...)` → new `char *` |
| print to the ArmorPaint console | `printf(fmt, ...)` → int length |
| compare | `string_equals(a, b)` → bool |
| length | `string_length(s)` |
| substring | `substring(s, start, end)` |
| search | `string_index_of`, `string_index_of_pos`, `string_last_index_of`, `starts_with`, `ends_with` |
| split / join | `string_split(s, sep)` → array, `string_array_join(a, sep)` |
| replace | `string_replace_all(s, search, replace)` |
| case | `to_lower_case`, `to_upper_case`, `trim_end` |
| char access | `char_at(s, i)` → `char *`, `char_code_at(s, i)` → int, `string_from_char_code(c)` |
| number → string | `i32_to_string`, `i64_to_string`, `u64_to_string`, `i32_to_string_hex`, `f32_to_string`, `f32_to_string_with_zeros` |
| allocate | `string_alloc(size)`, `string_copy(s)` |

### `string()` and `printf()` in detail

Both are registered as **variadic natives** (`minic_api.c:487-488`), which is why they carry no `sig`
and accept any argument count. They share one formatter, `minic_vformat` (`minic_api.c:39`).

Supported specifiers: `%d` `%i` `%u` `%f` `%g` `%e` `%s` `%p` `%c` `%%`.

**No width, no precision, no flags.** `%5.2f` is not parsed as a spec — `minic_vformat` reads exactly
one character after `%`, sees `5`, falls into the fallback branch, and emits the literal text `%5.2f`
minus a character. Format numbers with `f32_to_string` if you need control.

`printf` writes to the **ArmorPaint console via `console_log`, not stdout**, and returns the formatted
length. It appends nothing — `console_log` handles the line. `dev/test.c` uses it as its entire test
harness output.

`string()` returns memory from `string_alloc`, which is a plain `calloc` (`base/sources/iron_string.c:9`)
that **nothing ever frees**. See 1.11.

### String literals

Escapes handled (`minic.c:106`): `\n \t \r \\ \" \'`. **Any other escape yields `\0`**, which
terminates the string in place — `"C:\data"` silently becomes `"C:"`. Use forward slashes in paths, or
`\\`.

Backslash-newline line continuation is supported and is how `viewport_celshade.c:7-11` embeds a
multi-line shader:

```c
node_shader_write_frag(shader, " \
    var light_dir: float3 = float3(0.5, 0.5, -0.5);\
    output_color = basecol * step(0.5, dotnl) + basecol; \
");
```

Literals are written into the context arena at **lex time**, so a literal inside a loop body is
re-allocated on every iteration (the body is re-lexed each pass).

## 1.11 Memory model — the cost you must budget

Three separate allocators are in play. Getting them confused is the most likely way to crash the app.

### (a) The arena — 8 MB per plugin context, unchecked

`minic_alloc` (`minic.c:287`):

```c
int aligned = (*minic_active_mem_used + 7) & ~7;
*minic_active_mem_used = aligned + size;
return &minic_active_mem[aligned];
```

**There is no bounds check against `MINIC_MEM_SIZE`.** Overflowing the arena returns pointers past the
end of an 8 MB `calloc` and corrupts the heap.

The arena is charged by: every **script function call**, every **string literal lexed**, every **struct
value declaration**. It is rewound to the entry watermark **only at the outermost host→script boundary**
(`minic_call_in_ctx`, `minic.c:860`) — i.e. once per `on_update`/`on_ui` invocation. Script→script calls
do **not** rewind.

**Per-call cost** (`minic_call`, `minic.c:823`): a fresh `vars[128]`, `arrs[32]`, `vartypes[128]`:

```
128 * sizeof(minic_var_t)     = 128 * 80  = 10240
 32 * sizeof(minic_arr_t)     =  32 * 76  =  2432
128 * sizeof(minic_vartype_t) = 128 * 128 = 16384
                                  total   ≈ 29 KB per call frame
```

⇒ **≈ 280 script-function calls per `on_update` invocation before the 8 MB arena overflows.** This is a
per-frame budget, not a per-call-depth budget: 300 sequential calls to a one-line helper in a single
frame is enough. It also caps recursion at roughly the same depth.

Design rule for a bridge: **handle at most one request per frame** (which `PROTOCOL.md` already
specifies for latency reasons — it is also a memory-safety requirement), and keep per-request helper
call counts in the low tens.

### (b) `string()` / `string_alloc` — never freed

`string_alloc` is `calloc` and there is no collector (`string_tmp_reset` exists but is only called by
`_update` for a *different*, host-side temp buffer). Every `string()` call in your plugin leaks its
result for the life of the process. Small (tens of bytes) but unbounded — do not build strings in a
hot loop. Prefer fixed literals and `console_log` over per-frame `string()` formatting.

### (c) `malloc` / `calloc` / `realloc` / `free` — real, bound, yours

```
X1(malloc,  "p(i size)",       p, i)
X2(calloc,  "p(i n,i size)",   p, i, i)
X2(realloc, "p(p ptr,i size)", p, p, i)
X1(free,    "v(p ptr)",        v, p)
```

Use these for host structs you construct (`calloc(1, sizeof(ui_node_t))`) and for anything that must
outlive a callback. Nothing frees them for you.

The heap array constructors (`*_array_create`, `buffer_create`) are also real allocations;
`array_free(a)` releases the backing buffer (you still `free(a)` the header — see `ui_base.c:228-229`
for the host's own idiom).

### (d) `data_get_blob` — a permanent per-path cache

`base/sources/engine.c:1879`:

```c
buffer_t *cached = any_map_get(data_cached_blobs, file);
if (cached != NULL) return cached;
```

Keyed by path string, **never expires**. Read a file twice by the same path and you get the *first*
bytes forever. `data_delete_blob(path)` (`engine.c:1985`) is the only eviction. `plugin.c:21` and
`import_stl.c:64` both call it. This is already load-bearing in `PROTOCOL.md`; it is confirmed.

Note `data_get_blob` resolves relative paths against `./data/` (`data_resolve_path`, `engine.c:1798`);
absolute paths, `..`-relative and `./`-prefixed paths pass through untouched. Use absolute paths.

## 1.12 The silent-failure catalogue

These produce **no diagnostic**. This table is the reason this document exists.

| Mistake | What actually happens |
|---|---|
| 33rd function | dropped at registration; call errors later as `unknown function` |
| any function defined after `main` | never registered; same |
| `#define FOO 1` | line skipped; `FOO` becomes an undefined identifier |
| **misspelled / undefined variable** | `minic_var_get` returns **integer 0** with no error (`minic.c:490`). A typo is a silent zero. (Misspelled *functions* do error.) |
| `a ? b : c` | `?` and `:` are unknown characters and are **skipped** (`minic.c:276`); the expression mis-parses |
| `p != NULL && p->x` | RHS always evaluated → null deref → crash |
| `a || b && c` | parses as `(a || b) && c` |
| `f(i++)` | `++` reparsed as a prefix increment of the next argument |
| `"C:\data"` | unknown escape `\d` → `\0` → string truncated to `"C:"` |
| `int tmp[8];` inside a per-frame function | permanently consumes 8 of 512 shared slots; overflows into the arena after ~64 calls |
| >280 script calls in one frame | arena overflow, heap corruption |
| `malloc(sizeof(my_script_struct_t))` | `sizeof` is 0 → allocates nothing |
| enum constant name collision with another plugin | first definition wins, yours is ignored |
| `data_get_blob` without `data_delete_blob` | second read of the same path returns the first bytes forever |
| declaring an opaque handle as a struct pointer of the wrong registered type | reads at real offsets of the wrong layout |

Errors that *are* reported go to the console via `console_log` as
`"<plugin>.c:<line>: error: <msg> (got <token>)"` (`minic.c:378`) and **abort the rest of the script**
(`e->error` also sets `e->returning`). Watch the ArmorPaint console during development.

## 1.13 Hard caps (all from `minic.h` / `minic.c`)

| Cap | Value | Source | On exceed |
|---|---|---|---|
| functions per script | **32** (excl. `main`) | `minic.c:2099` | silently dropped |
| arena per context | **8 MB** | `MINIC_MEM_SIZE`, `minic.h:7` | **unchecked overflow** |
| locals per scope | 128 | `MINIC_MAX_VARS` | error: `too many local variables` |
| struct-typed locals per scope | 128 | `MINIC_MAX_VARTYPES` | error |
| fixed arrays per scope | 32 | `minic.c:2095` | silently ignored |
| total fixed-array elements per context | 512 | `minic.c:2096` | **unchecked overflow** |
| call arguments | 20 | `MINIC_MAX_PARAMS` | extra args dropped |
| struct fields | 32 | `MINIC_MAX_STRUCT_FIELDS` | dropped |
| structs per context | 64 (≈40 pre-seeded) | `MINIC_MAX_STRUCTS` | dropped |
| enum constants (process-wide) | 512 | `MINIC_MAX_ENUM_CONSTS` | dropped |
| host globals | 64 | `MINIC_MAX_GLOBALS` | dropped |
| identifier length | 63 chars | `MINIC_MAX_NAME` | truncated |
| registered host functions | 1024 (529 used) | `MINIC_MAX_EXTFUNS` | — |

Also absent by construction: **threads, sockets, exceptions, `setjmp`, dynamic loading, any stdio
beyond the bound `printf`**.

## 1.14 Canonical skeleton for a one-dispatcher bridge plugin

Everything here is checked against the rules above.

```c
#include "global.h"

void        *plugin;
ui_handle_t *h0;
char        *req_dir;
char        *res_dir;
float        accum   = 0.0;
int          enabled = 1;

// ---- helpers (budget: 32 functions total, main excluded) ----

int to_int(char *s) {                 // no atoi binding exists (see 2.9)
    if (s == NULL) return 0;
    int len = string_length(s);
    int i   = 0;
    int neg = 0;
    if (len > 0) {
        if (char_code_at(s, 0) == 45) {   // '-'
            neg = 1;
            i   = 1;
        }
    }
    int n = 0;
    for (int k = i; k < len; ++k) {
        int c = char_code_at(s, k);
        if (c < 48) break;
        if (c > 57) break;
        n = n * 10 + (c - 48);
    }
    if (neg) n = -n;
    return n;
}

void reply(char *id, char *body) {
    buffer_t *b = sys_string_to_buffer(body);
    iron_file_save_bytes(string("%s/%s.json", res_dir, id), b, 0);
    // commit marker LAST, as a separate call (PROTOCOL.md two-file commit)
    buffer_t *m = sys_string_to_buffer(i32_to_string(string_length(body)));
    iron_file_save_bytes(string("%s/%s.done", res_dir, id), m, 0);
}

char *dispatch(any_map_t *m) {
    char *op = any_map_get(m, "op");
    if (op == NULL) return "{\"v\":1,\"ok\":false,\"error\":{\"code\":\"bad_args\"}}";

    if (string_equals(op, "ping")) {
        return string("{\"v\":1,\"ok\":true,\"result\":{\"t\":%f}}", sys_time());
    }
    else if (string_equals(op, "project_save")) {
        if (string_equals(project_filepath_get(), "")) {
            return "{\"v\":1,\"ok\":false,\"error\":{\"code\":\"no_project\"}}";
        }
        project_save(false);
        return "{\"v\":1,\"ok\":true,\"result\":{}}";
    }
    else if (string_equals(op, "select_tool")) {
        char *v = any_map_get(m, "tool");
        if (v == NULL) return "{\"v\":1,\"ok\":false,\"error\":{\"code\":\"bad_args\"}}";
        int t = to_int(v);
        if (t < 0) return "{\"v\":1,\"ok\":false,\"error\":{\"code\":\"bad_args\"}}";
        if (t > 13) return "{\"v\":1,\"ok\":false,\"error\":{\"code\":\"bad_args\"}}";
        context_select_tool(t);
        return "{\"v\":1,\"ok\":true,\"result\":{}}";
    }
    // ... one else-if per op ...
    return "{\"v\":1,\"ok\":false,\"error\":{\"code\":\"unsupported\"}}";
}

void handle_one(char *name) {
    char     *path = string("%s/%s", req_dir, name);
    buffer_t *blob = data_get_blob(path);
    if (blob == NULL) return;
    char *text = sys_buffer_to_string(blob);
    data_delete_blob(path);            // MANDATORY: cache is keyed by path, forever
    iron_delete_file(path);            // consume before executing: a crash must not replay
    any_map_t *m = json_parse_to_map(text);
    char *id = any_map_get(m, "id");
    if (id == NULL) return;
    reply(id, dispatch(m));
}

void on_update() {
    if (!enabled) return;
    iron_delay_idle_sleep();           // FIRST real statement: keeps both gates open (0.3)
    accum += sys_real_delta();
    if (accum < 0.016) return;
    accum = 0.0;

    any_array_t *files = file_read_directory(req_dir);
    if (files == NULL) return;
    if (files->length < 1) return;
    char *name = files->buffer[0];     // ONE request per frame (arena budget, 1.11a)
    if (!ends_with(name, ".json")) return;
    handle_one(name);
}

void on_ui() {
    // only drawn while the Plugins tab is visible (0.2)
    if (ui_panel(h0, "MCP Bridge", false, false, false)) {
        if (ui_button("Toggle", UI_ALIGN_CENTER, "")) {
            enabled = !enabled;
        }
    }
}

void main() {
    plugin  = plugin_create();
    h0      = ui_handle_create();
    req_dir = string("%s/mcp_spool/req", project_basepath_get());
    res_dir = string("%s/mcp_spool/res", project_basepath_get());
    iron_create_directory(req_dir);
    iron_create_directory(res_dir);
    plugin_notify_on_ui(plugin, on_ui);
    plugin_notify_on_update(plugin, on_update);
}
```

---

# Part 2 — The tool surface

## 2.0 Reading the table

Each entry in `minic_api_list.h` is `X<argc>(name, "sig", ret_class, arg_class...)`.

The `sig` string is the **documentation-grade** signature; the trailing classes are the actual ABI:
`i` int · `f` float · `p` pointer · `b` bool (int) · `c` char (int) · `v` void.
In `sig`, `p:type_name` means "pointer to `type_name`", and the word after a space is the parameter
name. There are **529 active entries**; 9 more are present but commented out and do **not** exist
(`object_create`, `transform_create`, `camera_object_create`, `shader_data_create`, `mesh_data_create`,
`mesh_object_create`, `scene_create`, `scene_create_object`, `scene_create_mesh_object`).

All signatures quoted below are verbatim from the file, with the line number.

## 2.1 Introspection — what an agent can READ

Five entry points return live host state:

```
:474  X0(script_get_context,  "p:context_t()",  p)
:475  X0(script_get_config,   "p:config_t()",   p)
:476  X0(script_get_project,  "p:project_t()",  p)
:477  X1(script_get_object,   "p:object_t(p:char s)",         p, p)
:478  X1(script_get_material, "p:slot_material_t(p:char s)",  p, p)
:539  X0(context_main_object, "p:mesh_object_t()", p)
```

They are only useful to the extent the returned struct's fields are **registered**. Registration
happens in `minic_register_builtins` (`minic_api.c:484-834`) and is a **strict subset** of the real C
struct. Unlisted fields are unreachable; there is no offset arithmetic escape hatch.

### `context_t` — `script_get_context()` (`minic_api.c:797`)

| Field | Type | R/W | Note |
|---|---|---|---|
| `paint_object` | `mesh_object_t *` | R | the active paint mesh |
| `ddirty` | int | RW | force redraw; `make_tilesheet.c:59` sets `2` |
| `pdirty` | int | RW | force repaint |
| `material` | `slot_material_t *` | R | active material; `->canvas->name` gives its name |
| `layer` | **`void *`** | R | active layer — **opaque, cannot be dereferenced.** Only `!= NULL` is meaningful |
| `brush` | **`void *`** | R | opaque, same |
| `tool` | int | RW | `TOOL_TYPE_*`; `dev/test.c:42` reads it back to verify `context_select_tool` |
| `brush_radius` | float | RW | `dev/test.c:55` writes it directly |
| `brush_opacity` | float | RW | |
| `brush_hardness` | float | RW | |
| `brush_scale` | float | RW | |
| `brush_angle` | float | RW | |
| `brush_blending` | int | RW | |
| `viewport_mode` | int | RW | mirrors `context_set_viewport_mode` |
| `xray` | int | RW | |
| `capturing_screenshot` | bool | RW | `make_tilesheet.c:88` |

**18 fields. The real `context_t` has well over a hundred** — `format_type`, `layers_export`,
`layer_preview_dirty`, `rtdirty`, `texture_export_path`, `envmap_angle`, `show_envmap_blur` and the
rest are **not** exposed.

### `config_t` — `script_get_config()` (`minic_api.c:778`)

`window_w`, `window_h`, `window_scale`, `rp_supersample`, `recent_projects` (`string_array_t*`),
`plugins` (`string_array_t*`), `keymap`, `theme`, `undo_steps`, `camera_fov`, `layer_res`,
`brush_live`, `node_previews`, `material_live`, `workspace`, `workflow`. All read/write.

Not exposed: `version`, `sha`, `locale`, window position/mode flags, `rp_ssao`/`bloom`/`vignette`/
`grain`/`contrast`/`gamma`, `lut_path`, `bookmarks`, camera speeds, `zoom_direction`, `touch_ui`, …

### `project_t` — `script_get_project()` (`minic_api.c:816`)

| Field | Type | Liveness |
|---|---|---|
| `version` | `char *` | live |
| `assets` | `string_array_t *` | **live** — imported *texture* asset names |
| `is_bgra` | int | live |
| `envmap` | `char *` | live (asset name) |
| `envmap_strength`, `envmap_angle`, `camera_fov` | float | live |
| `camera_world`, `camera_origin` | `f32_array_t *` | **snapshot** — written by save (`export_arm.c:292`) |
| `font_assets`, `script_datas` | `string_array_t *` | snapshot |
| `swatches`, `brush_nodes`, `material_nodes`, `mesh_datas` | **`void *`** | snapshot, see below |
| `layer_datas` | **`void *`** | **always `NULL` in a live session** — `export_arm.c:415` nulls it right after save |

Not exposed at all: `packed_assets`, `material_groups`, `material_datas`, `mesh_assets`,
`mesh_transforms`, `mesh_materials/parents/physics_*`, `atlas_*`, `script_names`, `timeline_*`,
`stages`, and crucially the runtime pointer `_` (`project_runtime_t`) that holds the **live**
`layers`, `materials`, `paint_objects` arrays.

### The re-typing trick (and its limit)

Every `*_array_t` in this codebase is `{ void *buffer; int length; int capacity; }` — identical to the
registered `any_array_t` (`types.h:781,895`; `minic_api.c:331`). So an untyped `MINIC_P` array field can
be re-typed and read:

```c
project_t   *pr = script_get_project();
any_array_t *mn = pr->material_nodes;          // legal: layout matches
if (mn != NULL) {
    for (int i = 0; i < mn->length; ++i) {
        ui_node_canvas_t *c = mn->buffer[i];    // ui_node_canvas_t IS registered
        printf("material %s", c->name);
    }
}
```

**But**: `material_nodes` / `brush_nodes` / `mesh_datas` are only written at save (`export_arm.c:359`)
and load (`import_arm.c:842`). They are a **snapshot of the last save/load**, `NULL` in a fresh unsaved
project, and stale after `script_material_create`. Any tool built on them must say so.

The trick **cannot** reach layers: `layer_datas` is nulled after save, and `layer_data_t`/`slot_layer_t`
are not registered structs, so even a non-null array yields undereferenceable pointers.

### Registered struct catalogue (what `->` works on)

`i8/u8/i16/u16/i32/u32/f32/any/string_array_t`, `buffer_t`, `vec2_t`, `vec3_t`, `vec4_t`, `quat_t`,
`mat3_t`, `mat4_t`, `ui_handle_t`, `ui_node_socket_t`, `ui_node_button_t`, `ui_node_link_t`,
`ui_node_t`, `ui_node_canvas_t`, `slot_material_t`, `obj_t`, `vertex_array_t`, `mesh_data_t`,
`camera_data_t`, `world_data_t`, `vertex_element_t`, `shader_const_t`, `tex_unit_t`,
`shader_context_t`, `bind_tex_t`, `shader_data_t`, `render_target_t`, `object_t`, `mesh_object_t`,
`transform_t`, `camera_object_t`, `config_t`, `context_t`, `project_t`.

Notably **not** registered (opaque handles only): `gpu_texture_t`, `gpu_shader_t`, `gpu_pipeline_t`,
`gpu_buffer_t`, `slot_layer_t`, `slot_brush_t`, `slot_font_t`, `logic_node_t`, `ui_t`, `ui_nodes_t`,
`plugin_t`, `scene_t`, `raw_mesh_t`, `draw_font_t`, `video_t`, `sound_t`, `node_shader_t`,
`iron_file_reader_t`, `iron_file_writer_t`, `i32_map_t`/`f32_map_t`/`any_map_t`/`*_imap_t`.

### Registered enum constants (usable by name)

`UI_LAYOUT_VERTICAL|HORIZONTAL` · `UI_ALIGN_LEFT|CENTER|RIGHT` ·
`UI_STATE_IDLE|STARTED|DOWN|RELEASED|HOVERED` ·
`GPU_TEXTURE_FORMAT_RGBA32|RGBA64|RGBA128|R8|R16|R32|D32|RGBA32_BC7` ·
`PHYSICS_SHAPE_BOX|SPHERE|TERRAIN|MESH` ·
`TOOL_TYPE_BRUSH(0) ERASER(1) FILL(2) DECAL(3) TEXT(4) CLONE(5) BLUR(6) PARTICLE(7) COLORID(8) PICKER(9) MATERIAL(10) CURSOR(11) SELECT(12) BAKE(13)`
(`minic_api.c:535-543`).

Two host globals: `mouse_x`, `mouse_y` (floats, `minic_api.c:841`).

**`viewport_mode_t` is NOT registered** — pass integers (`enums.h:58`):
`-1` none, `0` lit, `1` base color, `2` normal map, `3` occlusion, `4` roughness, `5` metallic,
`6` opacity, `7` height, `8` emission, `9` subsurface, `10` texcoord, `11` object normal,
`12` material id, `13` object id, `14` mask, `15` path trace.

## 2.2 Project & session

```
:463  X1(project_save,          "v(i save_and_quit)", v, i)
:464  X0(script_project_new,    "v()", v)
:465  X1(script_project_open,   "v(p:char path)", v, p)
:471  X0(project_filepath_get,  "p:char()", p)
:472  X0(project_basepath_get,  "p:char()", p)
:473  X1(project_filepath_set,  "v(p:char s)", v, p)
:462  X0(script_quit,           "v()", v)
:545  X1(project_reskin_mesh,   "b(i frame)", b, i)
:546  X0(iron_delay_idle_sleep, "v()", v)
:455  X1(script_notify_on_update,     "v(p fn)", v, p)
:456  X1(script_notify_on_next_frame, "v(p fn)", v, p)
:494  X2(script_timer,          "v(f delay,p fn)", v, f, p)
:219  X0(sys_time,   "f()", f)      :220 sys_delta   :221 sys_real_delta
:222  X0(sys_w,      "i()", i)      :223 sys_h  :224 sys_x  :225 sys_y
:226  X0(sys_title,  "p:char()", p) :227 X1(sys_title_set, "v(p:char value)", v, p)
```

"Save as" is `project_filepath_set(path)` then `project_save(false)` — `dev/test.c:238` does exactly
that. `project_filepath_get()` returns `""` when no project has been saved (`autosave.c:15` tests it).

## 2.3 Assets, import & export

```
:466  X2(script_import_asset,   "v(p:char path,i hdr_as_envmap)", v, p, i)
:467  X1(script_append_mesh,    "v(p:char path)", v, p)
:468  X1(script_append_mesh_obj,"v(p:char data)", v, p)
:469  X1(script_export_mesh,    "v(p:char path)", v, p)
:470  X1(script_export_material,"v(p:char path)", v, p)
:540  X2(export_texture_run,    "v(p:char path,i bake_material)", v, p, i)
:522  X2(plugin_register_texture,  "v(p:char format,p fn)", v, p, p)
:523  X1(plugin_unregister_texture,"v(p:char format)", v, p)
:524  X2(plugin_register_mesh,     "v(p:char format,p fn)", v, p, p)
:525  X1(plugin_unregister_mesh,   "v(p:char format)", v, p)
:526  X2(plugin_register_text,     "v(p:char format,p fn)", v, p, p)
:527  X1(plugin_unregister_text,   "v(p:char format)", v, p)
:528  X5(plugin_make_raw_mesh, "p:raw_mesh_t(p:char name,p:i16_array_t posa,p:i16_array_t nora,p:u32_array_t inda,f scale_pos)", p, p,p,p,p,f)
```

**`export_texture_run` is the only binding that writes real image files to disk.** Verified in
`io/export_texture.c:497`. Important behaviour:

- `path` is a **directory**, not a filename (`dev/test.c:222` passes `dir_textures` and then counts
  files in it).
- Filenames are derived from `ui_files_filename` — the last name used in the export dialog — falling
  back to the translated `"untitled"` (`export_texture.c:160-164`), plus per-channel suffixes from the
  active export preset. On first use it auto-selects the `"generic"` preset (`export_texture.c:501`).
- **Format and bit depth are not controllable from a plugin.** They come from
  `g_context->format_type` and `base_bits_handle` — neither is registered. Default is 8-bit PNG.
- `bake_material = true` bakes the current material onto a plane instead of exporting layers.
- `script_export_mesh(path)` writes `<path>.obj` (`dev/test.c:230` checks for the `.obj` suffix);
  `script_export_material(path)` writes a `.arm`.
- `script_import_asset` handles textures, meshes and `.arm` materials by extension
  (`dev/test.c:286-312` round-trips all three).

## 2.4 Meshes, objects, scene, transforms

```
:539  X0(context_main_object,     "p:mesh_object_t()", p)
:477  X1(script_get_object,       "p:object_t(p:char s)", p, p)
:479  X0(script_shape_list,       "p:string_array_t()", p)
:480  X1(script_shape_add,        "p:object_t(p:char name)", p, p)
:481  X1(script_object_duplicate, "p:object_t(p:object_t o)", p, p)
:489  X0(script_pick_object,      "p:object_t()", p)
:504  X2(script_object_set_material, "v(p:object_t object,p:slot_material_t material)", v, p, p)
:14   X2(object_set_parent, "v(p:object_t raw,p:object_t parent_object)", v, p, p)
:15   X1(object_remove,     "v(p:object_t raw)", v, p)
:16   X2(object_get_child,  "p:object_t(p:object_t raw,p:char name)", p, p, p)
:93   X1(scene_get_child,   "p:object_t(p:char name)", p, p)
:94   X3(scene_add_mesh_object, "p:mesh_object_t(p:mesh_data_t data,p:shader_data_t material,p:object_t parent)", p, p,p,p)
:92   X1(scene_add_object,  "p:object_t(p:object_t parent)", p, p)
:97   X3(scene_spawn_object,"p:object_t(p:char name,p:object_t parent,i spawn_children)", p, p,p,i)
:20-26  transform_reset / _update / _build_matrix / _decompose / _world_x / _world_y / _world_z
```

`script_get_object(name)` matches against `paint_objects[i]->base->name` (`minic_impl.c:166`).
`script_shape_list()` returns the built-in primitive names accepted by `script_shape_add`
(`minic_impl.c:702`).

Transforms are written through the registered `transform_t` (`minic_api.c:760`), whose `loc`/`rot`/
`scale` are **embedded** `vec4_t`/`quat_t` — so `t->loc.x = 1.0;` writes in place. You must call
`transform_build_matrix(t)` afterwards. `dev/blocks.c:95-112` is the complete worked example.

Also available as math-API natives (registered by `MINIC_MATH_API`, `minic_api.c:278-290`, not in the
X-list): `transform_set_matrix`, `transform_rotate`, `transform_move`, `transform_look`,
`transform_right`, `transform_up`, `raycast_aabb_mouse`, `point_in_aabb`, `script_tween_to`.

**There is no binding that enumerates the paint objects.** `project_t` does not expose `mesh_assets`
or the runtime `paint_objects`; `mesh_datas` is a stale snapshot (2.1).

## 2.5 Materials

```
:502  X1(script_material_create,     "p:slot_material_t(p:char name)", p, p)
:503  X1(script_material_set,        "v(p:slot_material_t m)", v, p)
:505  X1(script_material_delete,     "v(p:slot_material_t m)", v, p)
:478  X1(script_get_material,        "p:slot_material_t(p:char s)", p, p)
:504  X2(script_object_set_material, "v(p:object_t object,p:slot_material_t material)", v, p, p)
:517  X0(script_material_update,     "v()", v)
:537  X0(plugin_material_kong_get,   "p()", p)
:538  X2(parser_material_parse_value_input, "p:char(p:ui_node_socket_t inp,i vector_as_grayscale)", p, p, i)
:521  X2(node_shader_write_frag,     "v(p:node_shader_t raw,p:char s)", v, p, p)
```

A material's **name is its canvas name**: `script_get_material` compares against
`materials[i]->canvas->name` (`minic_impl.c:177`). Read it back with
`script_get_context()->material->canvas->name`.

`slot_material_t` exposes: `canvas` (`ui_node_canvas_t*`), `id`, and nine paint-channel flags
`paint_base|opac|occ|rough|met|nor|height|emis|subs` (`minic_api.c:614`) — all read/write.

## 2.6 Material nodes

```
:506  X1(script_material_create_node,    "p:ui_node_t(p:char type)", p, p)
:507  X3(script_material_create_node_at, "p:ui_node_t(p:char type,f x,f y)", p, p, f, f)
:508  X1(script_material_get_node,       "p:ui_node_t(p:char type)", p, p)
:509  X1(script_material_get_node_id,    "p:ui_node_t(i id)", p, i)
:510  X4(script_material_connect,    "v(p:ui_node_t from,i from_socket,p:ui_node_t to,i to_socket)", v, p,i,p,i)
:511  X2(script_material_disconnect, "v(p:ui_node_t to,i to_socket)", v, p, i)
:512  X1(script_material_remove_node,"v(p:ui_node_t node)", v, p)
:513  X4(script_material_set_float, "v(p:ui_node_t node,i is_input,i socket,f value)", v, p,i,i,f)
:514  X7(script_material_set_color,  "v(p:ui_node_t node,i is_input,i socket,f r,f g,f b,f a)", v, p,i,i,f,f,f,f)
:515  X6(script_material_set_vector, "v(p:ui_node_t node,i is_input,i socket,f x,f y,f z)", v, p,i,i,f,f,f)
:516  X3(script_material_set_button, "v(p:ui_node_t node,i button,f value)", v, p, i, f)
```

This is the richest and best-tested corner of the API — `dev/test.c:99-176` and `dev/blocks.c:22-31`
exercise all of it.

Enumerate the active material's nodes through the registered structs (no binding needed):

```c
context_t        *c = script_get_context();
ui_node_canvas_t *canvas = c->material->canvas;
for (int i = 0; i < canvas->nodes->length; ++i) {
    ui_node_t *n = canvas->nodes->buffer[i];
    printf("%d %s %s", n->id, n->name, n->type);
}
```

`ui_node_t` fields: `id`, `name`, `type`, `x`, `y`, `color`, `inputs`, `outputs`, `buttons`, `width`,
`flags`. `ui_node_socket_t`: `id`, `node_id`, `name`, `type`, `color`, `default_value` (`f32_array_t*`),
`min`, `max`, `precision`, `display`. `ui_node_canvas_t`: `name`, `nodes`, `links`.
`ui_node_link_t`: `id`, `from_id`, `from_socket`, `to_id`, `to_socket` — this is how you read the
existing graph topology.

`script_material_get_node(type)` returns the **first** node of that type in the canvas, so it is only
reliable for singletons like `"OUTPUT_MATERIAL_PBR"`; use `script_material_get_node_id(id)` otherwise.

**Valid `type` strings** (from `nodes_material/*.c`, `.type = "..."`, plus `OUTPUT_MATERIAL_PBR` from
`material_output_node.c`):

`ATTRIBUTE BAKE_CURVATURE BLUR BOOL BRIGHTCONTRAST BUMP CLAMP COLMASK COMBINE_COLOR COMBXYZ CURVE_RGB
CURVE_VEC CUSTOM DIRECT_WARP ENUM FLOAT_CURVE GAMMA GROUP GROUP_INPUT GROUP_OUTPUT HUE_SAT INVERT_COLOR
LAYER LAYER_MASK MAPPING MAPRANGE MATERIAL MATH MIX_NORMAL_MAP MIX_RGB NEW_GEOMETRY NORMAL NORMAL_MAP
OBJECT_INFO OUTPUT_MATERIAL_PBR PICKER QUANTIZE REPLACECOL RGB RGBA RGBTOBW SCRIPT_CPU SEPARATE_COLOR
SEPXYZ SHADER_GPU STRING TEX_BAKE TEX_BRICK TEX_CAMERA TEX_CHECKER TEX_COORD TEX_GABOR TEX_GRADIENT
TEX_IMAGE TEX_MAGIC TEX_NOISE TEX_TEXT TEX_VORONOI TEX_WAVE TILESHEET TILESHEET_ANIM UVMAP VALTORGB
VALUE VECTOR VECT_MATH VECT_ROTATE VECT_TRANSFORM WIREFRAME`

Custom nodes (plugin-defined) — `plugin_material_category_add/remove`,
`plugin_material_custom_nodes_set/remove` (`:529-536`); `hello_node.c` is the full recipe.
Brush-graph equivalents exist (`plugin_brush_*`), but brush node types are only
`BOOL ENUM TEX_IMAGE VALUE VECTOR` and custom brush nodes cannot read their inputs (1.9).

## 2.7 Painting, brush, tools

```
:541  X1(context_select_tool, "v(i i)", v, i)
:498  X2(script_paint,       "v(f x,f y)", v, f, f)
:499  X3(script_paint_world, "v(f x,f y,f z)", v, f, f, f)
:500  X0(script_paint_end,   "v()", v)
:501  X0(script_fill_layer,  "v()", v)
:497  X7(script_draw_particles, "v(p:gpu_texture_t texture,f x,f y,f w,f h,i atlas_x,i atlas_frames)", v, p,f,f,f,f,i,i)
```

`script_paint(x, y)` takes **normalised screen coordinates** and must be terminated by
`script_paint_end()` to close the stroke; `dev/test.c:60-70` paints two strokes this way.
`script_paint_world(x, y, z)` is the world-space variant.

Both silently no-op unless a project is open, a layer is selected and it is not a group
(`script_paint_allowed`, `minic_impl.c:187`).

Brush parameters are set on `context_t` directly (2.1), not via bindings. `script_fill_layer()` fills
the active layer with the active material (`minic_impl.c:301`) and pushes an undo step.

## 2.8 Layers — the empty room

**This is the least-served area of the API and the answer is close to "nothing".** Exhaustive grep of
all 529 entries for `layer`:

```
:501  X0(script_fill_layer, "v()", v)          <- the ONLY layer operation
```

Plus, in the registered structs: `config_t.layer_res` (int), `context_t.layer` (**untyped `void *`**),
`project_t.layer_datas` (**untyped `void *`, always NULL live** — `export_arm.c:415`).

There is **no** binding to create, delete, duplicate, rename, reorder, show/hide, group, mask, set
opacity or blend mode, set resolution, or enumerate layers. `slot_layer_t` (`types.h:17`, 40 fields)
is not a registered struct, so `context_t.layer` cannot be dereferenced, and a script-defined struct
cannot overlay it (1.8).

What you *can* say about layers from a plugin:

- whether one is selected: `script_get_context()->layer != NULL`
- the configured layer resolution: `script_get_config()->layer_res`
- fill the selected one: `script_fill_layer()`
- paint into the selected one: `script_paint*`

Everything else is `unsupported`. Any MCP tool promising layer CRUD would have to drive the UI, and
there is no UI-automation binding either (`ui_*` bindings *draw* your own widgets; they do not click
ArmorPaint's).

## 2.9 Files, JSON, armpack — the transport layer

```
:314  X1(file_read_directory, "p:any_array_t(p:char path)", p, p)
:307  X1(iron_read_directory, "p:char(p:char path)", p, p)      -- newline-joined string
:308  X1(iron_create_directory,"v(p:char path)", v, p)
:309  X1(iron_is_directory,   "b(p:char path)", b, p)
:310  X1(iron_file_exists,    "b(p:char path)", b, p)
:311  X1(iron_delete_file,    "v(p:char path)", v, p)
:312  X3(iron_file_save_bytes,"v(p:char path,p:buffer_t bytes,i length)", v, p, p, i)
:315  X2(file_copy,           "v(p:char src_path,p:char dst_path)", v, p, p)
:316  X1(file_start,          "v(p:char path)", v, p)
:76   X1(data_get_blob,       "p:buffer_t(p:char file)", p, p)
:81   X1(data_delete_blob,    "v(p:char handle)", v, p)
:86   X0(data_path,           "p:char()", p)
:85   X1(data_is_abs,         "b(p:char file)", b, p)
:229  X1(sys_buffer_to_string,"p:char(p:buffer_t b)", p, p)
:230  X1(sys_string_to_buffer,"p:buffer_t(p:char str)", p, p)
:298-306  iron_file_reader_open/close/read/size/pos/seek, iron_file_writer_open/write/close
:313  X4(iron_file_download,  "v(p:char url,p callback,i size,p:char dst_path)", v, p,p,i,p)
:317  X4(file_download_to,    "v(p:char url,p:char dst_path,p done,i size)", v, p,p,p,i)
```

`iron_file_save_bytes(path, bytes, 0)` writes the whole buffer (`length > 0` truncates; `iron_file.c:412`).
It is a plain truncating `_wfopen(path, "wb")` — **no rename/move binding exists anywhere in the 529**,
which is the origin of `PROTOCOL.md`'s two-file commit. Confirmed.

`iron_file_download` / `file_download_to` are HTTPS GET only. **No inbound sockets. No listener.**
Confirmed.

### ⚠ `json_parse_to_map` is flat, string-typed, and array-hostile

```
:549  X1(json_parse,         "p(p:char s)", p, p)
:550  X1(json_parse_to_map,  "p:any_map_t(p:char s)", p, p)
:551-565  json_encode_begin / _end / _string / _string_array / _f32 / _i32 / _null /
          _f32_array / _i32_array / _bool / _begin_array / _end_array /
          _begin_object / _end_object / _map
:566  X1(json_encode_to_armpack, "p:buffer_t(p:char json)", p, p)
:569-595  armpack_decode / _decode_to_map / _decode_to_json / encoders / sizers /
          armpack_map_get_f32 / armpack_map_get_i32
```

`token_write_to_map` (`base/sources/iron_json.c:297`) — read it before designing the wire format:

1. **Every value comes back as a `char *`** — the raw source substring. Numbers are strings; booleans
   are the strings `"true"`/`"false"`. **JSON string escapes are NOT decoded** (`\n` stays as two
   characters, `\\` stays as two). Send paths with forward slashes.
2. **Nested objects are flattened into the same map.** `{"op":"x","args":{"kind":"paint"}}` yields
   `op → "x"` and `kind → "paint"`; the key `args` never appears. The source carries a
   `// TODO: Object containing another object`. Nested keys therefore **share the top-level namespace**
   — an arg named `id` or `op` would clobber the envelope.
3. **Arrays are skipped and corrupt the rest of the parse.** The `JSMN_ARRAY` branch is `ti++` and
   returns, so the array's element tokens are subsequently consumed as key/value pairs. Everything
   after an array in the document is garbage. **Send no JSON arrays.**
4. **Key detection is `s[t->end + 1] == ':'`** (`iron_json.c:91`). A space between a key's closing
   quote and the colon breaks it. **The server must emit compact JSON** —
   `json.dumps(obj, separators=(",", ":"))`.
5. **There is no `atoi`/`atof` binding.** Verified by grep over all 529. Numeric args must be converted
   in minic (see `to_int` in 1.14). `armpack_map_get_i32/f32` (`:594-595`) do **not** help — they
   reinterpret the stored pointer as a `ptr_storage_t` union and only work on maps from
   `armpack_decode_to_map`.

Recommended wire shape given all of the above: **flat object, no arrays, no nesting, all values as
strings, compact separators.** e.g.

```json
{"v":"1","id":"42","op":"select_tool","a_tool":"0"}
```

with an `a_` prefix on argument keys to keep them out of the envelope namespace. (Nesting `args` also
*works* thanks to the flattening, but it hides the collision hazard — prefer the explicit prefix.)

For responses, build the JSON with `string()` — `json_encode_*` maintains a single process-wide static
buffer (`iron_json.c:332`) and is not reentrant-safe alongside host use.

An alternative worth measuring later: have Python write **armpack** (msgpack-family) and use
`data_get_blob` → `armpack_decode_to_map` → `armpack_map_get_i32/f32`, which preserves types. Untested
in this tree from a plugin; do not adopt without a Phase-A experiment.

## 2.10 Viewport, camera, render

```
:542  X3(gpu_create_render_target, "p:gpu_texture_t(i width,i height,i format)", p, i, i, i)
:543  X5(viewport_capture_screenshot_to, "v(p:gpu_texture_t target,f x,f y,f w,f h)", v, p,f,f,f,f)
:544  X1(viewport_save_texture,   "v(p:gpu_texture_t screenshot)", v, p)
:519  X1(context_set_viewport_mode,  "v(i mode)", v, i)
:520  X1(context_set_camera_controls,"v(i i)", v, i)
:518  X1(context_set_viewport_shader,"v(p viewport_shader)", v, p)
:104-113  render_path_set_target / _end / _draw_meshes / _draw_skydome / _bind_target /
          _draw_shader / _load_shader / _resize / _create_render_target, render_target_create
:30-32   camera_object_build_proj / _remove / _build_mat
```

**`viewport_save_texture` does not write to disk.** `viewport.c:96` PNG-encodes the texture into
`g_project->packed_assets` in memory and imports it as a project texture asset. `iron_encode_png` and
`gpu_get_texture_pixels` are **not bound**, so there is no plugin path from a GPU texture to a file.
The screenshot workflow is: `gpu_create_render_target` → `viewport_capture_screenshot_to` (repeatedly,
tiling) → `viewport_save_texture`; `make_tilesheet.c` is the reference implementation, including the
required `context->capturing_screenshot = true` / `ddirty = 2` handshake and a 2-frame settle wait.

The camera is reachable as a scene object (`scene_get_child` + `transform_t`), and its last-saved pose
is in `project_t.camera_world` / `camera_origin` / `camera_fov`. There is **no** "set camera to this
pose" binding — you would move the camera object's transform and rebuild its matrix.

## 2.11 Console & UI

```
:457  X1(console_info,  "v(p:char s)", v, p)
:458  X1(console_error, "v(p:char s)", v, p)
:459  X1(console_log,   "v(p:char s)", v, p)
:460  X3(ui_box_show_message, "v(p:char title,p:char text,i copyable)", v, p, p, i)
:490  X2(script_show_message, "v(p:char text,f seconds)", v, p, f)
:461  X4(ui_files_show2, "v(p:char filters,i is_save,i open_multiple,p files_done)", v, p,i,i,p)
:116-193  the full immediate-mode ui_* widget set (ui_panel, ui_button, ui_text, ui_slider,
          ui_check, ui_radio, ui_combo, ui_text_input, ui_text_area, ui_color_wheel,
          ui_row/row2..row7, ui_separator, ui_tooltip, ui_handle_create, ...)
:196-216  ui_nodes_* / UI_NODE_* node-editor geometry helpers
```

Console output is **write-only** — there is no binding to read the console back, so an agent cannot
retrieve ArmorPaint's own log messages.

`ui_*` widgets draw **your** panel inside the Plugins tab. They cannot drive ArmorPaint's own UI.

## 2.12 Remaining areas (complete, for closure)

- **strings** `:269-295` (30) — see 1.10.
- **arrays / buffers** `:345-433` (89) — push/resize/pop/shift/splice/concat/slice/insert/remove/
  index_of/reverse/sort for every element type; `buffer_get_*`/`buffer_set_*` for u8/i8/u16/i16/f16/
  u32/i32/f32/f64/i64; every `*_array_create*` constructor.
- **maps** `:326-342` (17) — `i32_map`, `f32_map`, `any_map` (string keys) and `*_imap` (int keys),
  `map_keys`, `map_delete`.
- **draw / 2D** `:240-261` (22) — immediate-mode drawing into a texture target.
- **line/shape draw** `:233-237` (5).
- **input** `:436-448` (13) — `mouse_down/started/released`, `keyboard_down/started/released/repeat`,
  `mouse_view_x/y`, `keyboard_key_code`, plus the `mouse_x`/`mouse_y` globals.
- **math** — `cosf`, `sinf`, `vec4_fdist`, `mat4_cofactor`, `iron_random_get*` are in the X-list
  (`:4-10`); the ~90 vec/quat/mat functions are registered separately as natives
  (`MINIC_MATH_API`, `minic_api.c:201-290`) and are callable by the same names.
- **engine plumbing** `:14-113` — `object_*`, `transform_*`, `camera_object_*`, `world_data_*`,
  `shader_data_*`, `shader_context_*`, `mesh_data_*`, `mesh_object_*`, `data_get_*`, `scene_*`,
  `render_path_*`. Mostly for scene/renderer authoring, not painting.
- **physics** `:483-488` — `script_physics_set_shape/mass/velocity`, `_apply_impulse`,
  `_sync_transform`, `trait_point_and_click_controller_walk_to`.
- **player/stage** `:491-496` — `script_set_stage`, `script_get_stage`, `script_fade_to_stage`,
  `script_show_envmap`, `script_add_trait`, `script_set_tilesheet_anim`.

---

## 2.13 Proposed MCP tool surface (54 tools — shipped as 58)

> **This section is the design-time proposal, kept because it records the binding behind each
> tool.** The shipped surface is **58**: four tools were added during implementation and are not
> numbered below — `ap_bridge_status` and `ap_read_image_file` (both answered by the Python server,
> with no minic binding behind them, which is why they were not in a binding-derived list),
> `ap_material_update` (`script_material_update()`), and `ap_capture_viewport`
> (`viewport_save_texture_to_file()`, patched builds only — see UPSTREAM_CHANGES.md). The
> authoritative list is `TOOLS` in `armorpaint_mcp/server.py`.

Every tool below is backed by named bindings or registered struct fields. `ctx` = `script_get_context()`,
`cfg` = `script_get_config()`, `prj` = `script_get_project()`.

### Bridge & session (5)

| # | Tool | Implementation |
|---|---|---|
| 1 | `ap_ping` | `sys_time`, `sys_title`, `project_filepath_get`; heartbeat file per `PROTOCOL.md` |
| 2 | `ap_get_app_info` | `sys_title`, `sys_w`, `sys_h`, `sys_x`, `sys_y`, `data_path`, `prj->version` |
| 3 | `ap_bridge_set_enabled` | plugin-local flag; gates `iron_delay_idle_sleep` (0.3) so the app can sleep again |
| 4 | `ap_console_write` | `console_info` / `console_error` / `console_log` (level arg) |
| 5 | `ap_show_message` | `script_show_message(text, seconds)`; `ui_box_show_message(title, text, copyable)` for modal |

### Project (8)

| # | Tool | Implementation |
|---|---|---|
| 6 | `ap_project_new` | `script_project_new` |
| 7 | `ap_project_open` | `iron_file_exists` guard → `script_project_open(path)` |
| 8 | `ap_project_save` | `project_filepath_get` guard (`""` ⇒ `no_project`) → `project_save(false)` |
| 9 | `ap_project_save_as` | `project_filepath_set(path)` → `project_save(false)` (`dev/test.c:238`) |
| 10 | `ap_project_get_info` | `project_filepath_get`, `project_basepath_get`, `prj->version`, `is_bgra`, `envmap*`, `camera_fov` |
| 11 | `ap_project_list_texture_assets` | `prj->assets` (`string_array_t`) — **live** |
| 12 | `ap_project_list_scripts` | `prj->script_datas` — snapshot, flag as such |
| 13 | `ap_quit` | `script_quit` |

### Import / export (7)

| # | Tool | Implementation |
|---|---|---|
| 14 | `ap_import_asset` | `script_import_asset(path, 0)` (texture / mesh / `.arm` by extension) |
| 15 | `ap_import_envmap` | `script_import_asset(path, 1)`; read back `prj->envmap` |
| 16 | `ap_set_envmap_params` | `prj->envmap_strength`, `prj->envmap_angle` (direct field writes) |
| 17 | `ap_export_textures` | `export_texture_run(dir, 0)` + `file_read_directory(dir)` to report names. **Must document**: dir, name from last dialog use or `untitled`, 8-bit PNG, format not settable |
| 18 | `ap_export_material_bake` | `export_texture_run(dir, 1)` |
| 19 | `ap_export_mesh` | `script_export_mesh(path)` → `<path>.obj`; verify with `iron_file_exists` |
| 20 | `ap_export_material` | `script_export_material(path)` → `.arm` |

### Files (3)

| # | Tool | Implementation |
|---|---|---|
| 21 | `ap_fs_list` | `file_read_directory`, `iron_is_directory` |
| 22 | `ap_fs_stat` | `iron_file_exists`, `iron_is_directory`, `data_is_abs` |
| 23 | `ap_fs_mkdir` | `iron_create_directory` |

### Introspection (5)

| # | Tool | Implementation |
|---|---|---|
| 24 | `ap_get_context` | all 18 registered `context_t` fields (2.1) — the workhorse read |
| 25 | `ap_get_config` | all 16 registered `config_t` fields |
| 26 | `ap_set_config` | writes to the same 16 (`layer_res`, `camera_fov`, `workspace`, `workflow`, …) |
| 27 | `ap_get_main_object` | `context_main_object()` → `mo->base->name`, `mo->base->visible`, transform |
| 28 | `ap_get_object` | `script_get_object(name)` → `object_t` fields + transform |

### Objects & meshes (6)

| # | Tool | Implementation |
|---|---|---|
| 29 | `ap_shape_list` | `script_shape_list()` |
| 30 | `ap_shape_add` | `script_shape_add(name)` (validates against the list, `minic_impl.c:708`) |
| 31 | `ap_object_duplicate` | `script_get_object` → `script_object_duplicate` |
| 32 | `ap_object_set_transform` | `o->transform->loc/rot/scale` + `transform_build_matrix` (`dev/blocks.c:95`) |
| 33 | `ap_object_set_visible` | `o->visible` |
| 34 | `ap_append_mesh` | `script_append_mesh(path)` / `script_append_mesh_obj(data)` |

### Materials (7)

| # | Tool | Implementation |
|---|---|---|
| 35 | `ap_material_get_active` | `ctx->material->canvas->name` + the 9 `paint_*` channel flags |
| 36 | `ap_material_create` | `script_material_create(name)` |
| 37 | `ap_material_select` | `script_get_material(name)` → `script_material_set(m)` |
| 38 | `ap_material_delete` | `script_get_material(name)` → `script_material_delete(m)` |
| 39 | `ap_material_assign` | `script_get_object` + `script_get_material` → `script_object_set_material` |
| 40 | `ap_material_set_channels` | `m->paint_base` … `m->paint_subs` |
| 41 | `ap_material_list` ⚠ | `prj->material_nodes` re-typed as `any_array_t` → `ui_node_canvas_t->name`. **DEGRADED**: snapshot of last save/load, `NULL` before first save, misses materials created this session. Ship it labelled, or omit |

### Material nodes (6)

| # | Tool | Implementation |
|---|---|---|
| 42 | `ap_node_list` | walk `ctx->material->canvas->nodes` (id/name/type/x/y) and `->links` (topology) |
| 43 | `ap_node_add` | `script_material_create_node_at(type, x, y)`; validate `type` against the 68-name list (2.6) |
| 44 | `ap_node_remove` | `script_material_get_node_id(id)` → `script_material_remove_node` |
| 45 | `ap_node_connect` | `script_material_connect(from, from_socket, to, to_socket)` |
| 46 | `ap_node_disconnect` | `script_material_disconnect(to, to_socket)` |
| 47 | `ap_node_set_value` | `script_material_set_float` / `_set_color` / `_set_vector` / `_set_button` (dispatch on a `kind` arg); follow with `script_material_update()` |

### Painting & viewport (7)

| # | Tool | Implementation |
|---|---|---|
| 48 | `ap_select_tool` | `context_select_tool(i)` (0–13), verify via `ctx->tool` |
| 49 | `ap_set_brush` | `ctx->brush_radius/opacity/hardness/scale/angle/blending` |
| 50 | `ap_paint_stroke` | N × `script_paint(x, y)` + `script_paint_end()`. Points arrive as a **delimited string**, not a JSON array (2.9 #3) |
| 51 | `ap_paint_stroke_world` | N × `script_paint_world(x, y, z)` + `script_paint_end()` |
| 52 | `ap_fill_layer` | `script_fill_layer()` (guard: `ctx->layer != NULL`) |
| 53 | `ap_set_display_channel` | `context_set_viewport_mode(i)` (0–15, `enums.h:58`), verify via `ctx->viewport_mode` |
| 54 | `ap_capture_to_project` | `gpu_create_render_target` → `viewport_capture_screenshot_to` → `viewport_save_texture`; lands as a **project texture asset**, not a file. Follow `make_tilesheet.c`'s `capturing_screenshot`/`ddirty`/2-frame settle handshake |

**54 proposed, 58 shipped** (see the note at the head of this section for the four additions).
Every one is backed by a named binding or a registered struct field — except `ap_bridge_status` and
`ap_read_image_file`, which the Python server answers by itself. The single degraded entry (#41) is
marked. Each maps to one `else if` arm in the dispatcher, well inside the 32-function budget
because ops are arms, not functions.

## 2.14 EXCLUDED — and exactly why

| Would-be tool | Verdict |
|---|---|
| `list_layers`, `add_paint_layer`, `add_fill_layer`, `add_group_layer`, `delete_layer`, `duplicate_layer`, `rename_layer`, `move_layer`, `set_layer_opacity`, `set_layer_blending_mode`, `set_layer_enabled`, `add_layer_mask`, `remove_layer_mask`, `add_mask_fill`, `set_layer_projection` | **No binding exists.** `script_fill_layer` is the only layer op in the 529. `slot_layer_t` is unregistered so `context_t.layer` cannot be dereferenced; `project_t.layer_datas` is `NULL` in a live session (`export_arm.c:415`) |
| `list_channels`, `add_channel`, `remove_channel` | No channel bindings. Only the 9 per-material `paint_*` booleans on `slot_material_t`, which are material-level flags, not texture-set channels |
| `set_texture_set_resolution` | Not exposed. `config_t.layer_res` is a global default, not a per-set resolution, and changing it does not resize existing layers |
| `list_texture_sets`, `get_uv_tiles` | No bindings; `g_project->_` (runtime) is unregistered |
| `bake_maps` | No bake-run binding. `TOOL_TYPE_BAKE` selects the *tool*, but every bake parameter lives on unregistered `context_t` fields and UI handles. `export_texture_run(path, 1)` bakes the **material to a plane**, which is a different operation |
| `undo` / `redo` | No binding (grep: zero hits for `undo`/`redo`/`history`) |
| `export_textures` **with format/bit-depth/preset control** | `export_texture_run` takes only `(path, bake_material)`. `format_type`, `layers_export` and the bits handle are unregistered. Ship `ap_export_textures` without those knobs and say so |
| `screenshot_to_file` / `get_viewport_image` | `viewport_save_texture` writes to `g_project->packed_assets` in memory (`viewport.c:96`); `iron_encode_png` and `gpu_get_texture_pixels` are **not bound**. No plugin path from GPU texture → disk file |
| `list_meshes` / `list_paint_objects` | `project_t` exposes neither `mesh_assets` nor the runtime `paint_objects`; `mesh_datas` is a save/load snapshot. Only `context_main_object()` and `script_get_object(name)` work |
| `set_camera` (pose) | No camera-set binding; only `camera_object_build_proj/_build_mat` on a camera object you would have to locate and drive by transform |
| `get_environment_map` / `set_environment_map` (by resource) | Partially covered: `prj->envmap` (name, read) and `script_import_asset(path, 1)` (set from file). No shelf/resource lookup |
| `set_tone_mapping`, `set_color_lut` | `config_t.lut_path` and the `rp_*` post-process fields are **not** among the 16 registered `config_t` fields |
| `list_shelves`, `search_resources`, `import_resource` | No shelf/resource-database bindings; only raw filesystem + `script_import_asset` |
| `get_project_metadata` / `set_project_metadata` | No metadata bindings |
| `execute_python` / arbitrary script eval | No `minic_eval` binding exposed to plugins |
| `read_console_log` | `console_*` are write-only |
| any tool driving ArmorPaint's own UI (click a button, open a tab) | `ui_*` bindings draw *your* widgets; there is no UI-automation surface |
| socket / HTTP-server transport | No inbound socket bindings; `iron_file_download` and `file_download_to` are HTTPS GET only |
| atomic response write | No rename/move binding anywhere in the 529 — hence the two-file commit in `PROTOCOL.md` |

---

## Appendix A — corrections and confirmations for `PROTOCOL.md`

**Confirmed as written:**
- 529 bindings, no inbound sockets, downloads are HTTPS GET only.
- No rename/move binding ⇒ the plugin cannot write atomically ⇒ two-file commit is necessary.
- `data_get_blob` memoises by path forever (`engine.c:1879`); `data_delete_blob` is mandatory.
- `iron_file_save_bytes` is a truncating `_wfopen(path, "wb")` (`iron_file.c:412`).
- `export_texture_run` is the only real disk-writing image export; `viewport_save_texture` is in-memory.
- No threads; every handler runs inline on the render thread.
- One request per frame — and this is now also a **memory-safety** requirement (1.11a), not only a
  latency one.

**Resolved (was listed as the design's biggest open question):**
- §"The idle gate" asks whether `iron_delay_idle_sleep()` also defeats the **background** gate.
  **It does.** Both gates test the same `paused_frames` counter (`iron.h:217` and `:234`) and
  `iron_delay_idle_sleep()` sets it to `0` (`iron.h:1011`). Steady state with a per-frame reset is
  0→2→0; neither threshold (3 and 120) is ever reached. Foregrounding is not required.
  The tolerance is narrow: **≤3 consecutive missed frames**, so the call must precede any early return.

**Corrections:**
- The gate lives in `_update()` in `base/sources/iron.h:215`, not in `base_update()`. The plugin
  `on_update` dispatch is in `paint/sources/ui/ui_base.c:225` (inside `ui_base_update`, outside its
  `if (base_ui_enabled)` block).
- **The request envelope in §Envelopes will not survive `json_parse_to_map`.** `"args": {...}` is
  silently **flattened** into the top-level map (the key `args` disappears), and any JSON **array**
  corrupts the remainder of the parse (`iron_json.c:297-320`). Also: every value returns as a
  **string** with escapes undecoded, there is **no `atoi`/`atof` binding**, and key detection requires
  **no whitespace before the colon** (`iron_json.c:91`). See 2.9 for the recommended flat, compact,
  string-typed shape.
- `heartbeat.json`'s `t` from `sys_time()` is correct as monotonic-within-a-run.
- The plugin's own UI toggle only renders while the **Plugins tab** is open (`tab_plugins.c:27`), so
  the toggle must not be the only way to change bridge state — keep `ap_bridge_set_enabled` as a tool
  and persist the flag in a file the plugin reads at start.

## Appendix B — pre-flight checklist for the plugin author

- [ ] `main` is the last function in the file.
- [ ] ≤ 32 non-`main` functions; ops live in one `if / else if` dispatcher.
- [ ] No `#define`. No `switch`. No ternary. No casts. No `i++` inside an expression.
- [ ] Every `&&` / `||` chain audited for null dereference on the right-hand side.
- [ ] Every mixed `&&`/`||` expression parenthesised.
- [ ] No fixed-size array (`T x[N];`) declared inside any function.
- [ ] ≤ ~1 request and a few dozen helper calls per frame (28 KB of arena per call, 8 MB total).
- [ ] `string()` not called in a hot loop (it leaks).
- [ ] `data_delete_blob(path)` after every `data_get_blob(path)`.
- [ ] Request file deleted **before** the handler runs.
- [ ] `.done` marker written **last**, in its own `iron_file_save_bytes` call.
- [ ] `iron_delay_idle_sleep()` reached on every frame the bridge is enabled, before any early return.
- [ ] Every argument validated before it reaches a binding — there are no exceptions and pointer
      dereference is unchecked.
- [ ] All paths use forward slashes (backslash escapes truncate string literals).
- [ ] Any enum constants prefixed to avoid the process-wide 512-entry shared namespace.
