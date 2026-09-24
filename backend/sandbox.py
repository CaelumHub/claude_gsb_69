"""A restricted-Python sandbox for smart-contract execution.

Security model (defense in depth):

1. **Static AST validation** — the source is parsed and walked; ``import``,
   ``from``, dunder/``__class__``-style attribute escapes, ``global``/``nonlocal``,
   and a denylist of builtins are all rejected before anything runs.
2. **Restricted builtins** — ``__builtins__`` is replaced with a small whitelist
   (no ``open``, ``eval``, ``exec``, ``__import__``, ``input``, ``globals``, …),
   which removes every IO / reflection / network primitive.
3. **Instruction budget** — a ``sys.settrace`` counter enforces a bytecode/line
   budget so infinite loops and quadratic blow-ups are terminated.
4. **Bounded output** — ``print`` is redirected into an in-memory buffer with a
   hard byte cap, so contracts cannot flood the node with output.

The contract gets a tiny API surface (``state``, ``msg``, ``emit``, ``require``,
``transfer``) injected as globals; it has no access to the node process, the
filesystem, or the network.
"""

import ast
import io
import sys

# --------------------------------------------------------------------------- #
# Denylist / allowlist of builtins
# --------------------------------------------------------------------------- #
FORBIDDEN_BUILTINS = {
    "__import__", "open", "eval", "exec", "compile", "input", "globals",
    "locals", "vars", "dir", "help", "memoryview", "exit", "quit",
    "breakpoint", "getattr", "setattr", "delattr", "hasattr",
    "object", "type", "super", "classmethod", "staticmethod", "property",
    "id", "hash", "__build_class__", "execfile", "copyright", "credits",
    "license",
}

# Builtins a contract may rely on for ordinary computation.
SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "bytes", "chr", "dict", "divmod", "enumerate",
    "filter", "float", "format", "frozenset", "int", "isinstance", "issubclass",
    "iter", "len", "list", "map", "max", "min", "next", "ord", "pow", "print",
    "range", "repr", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip",
}

# Attribute names that are always rejected (escape hatches to object internals).
FORBIDDEN_ATTRIBUTES = {
    "__class__", "__bases__", "__mro__", "__subclasses__", "__globals__",
    "__builtins__", "__dict__", "__code__", "__closure__", "__func__",
    "__self__", "__init__", "__new__", "__del__", "__reduce__", "__reduce_ex__",
    "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
    "__import__", "__loader__", "__spec__", "__annotations__", "__module__",
    "__name__", "__qualname__", "__doc__", "__call__", "__hash__",
    "mro", "getattribute", "getattr", "setattr",
}

FORBIDDEN_NAMES = FORBIDDEN_BUILTINS | {"os", "sys", "socket", "subprocess",
    "shutil", "importlib", "builtins", "io", "pathlib", "pickle", "ctypes",
    "inspect", "signal", "threading", "multiprocessing"}


class SandboxError(Exception):
    """Raised when a contract violates a sandbox rule."""


# --------------------------------------------------------------------------- #
# Static validation
# --------------------------------------------------------------------------- #
class _Validator(ast.NodeVisitor):
    ALLOWED_NODES = {
        "Module", "FunctionDef", "AsyncFunctionDef", "Return", "Assign",
        "AugAssign", "AnnAssign", "Expr", "If", "For", "While", "Break",
        "Continue", "Pass", "Compare", "BoolOp", "BinOp", "UnaryOp", "Not",
        "And", "Or", "Add", "Sub", "Mult", "Div", "FloorDiv", "Mod", "Pow",
        "LShift", "RShift", "BitOr", "BitAnd", "BitXor", "Invert", "USub",
        "UAdd", "Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot", "In",
        "NotIn", "Call", "Name", "Constant", "Attribute", "List", "Tuple",
        "Dict", "Set", "Subscript", "Slice", "Index", "Load", "Store", "Del",
        "arguments", "arg", "keyword", "ListComp", "SetComp", "DictComp",
        "GeneratorExp", "comprehension", "IfExp", "NamedExpr", "Lambda",
        "JoinedStr", "FormattedValue", "Starred", "Delete", "Global", "Nonlocal",
        "Raise", "Try", "TryStar", "ExceptHandler", "Assert", "With", "withitem",
        "Import", "ImportFrom", "alias", "Await", "Yield", "YieldFrom",
        "ClassDef", "Tuple", "Match", "match_case", "match_value", "MatchAs",
        "MatchMapping", "MatchSequence", "MatchSingleton", "MatchOr",
        "MatchStar", "MatchClass",
    }

    def __init__(self):
        self.errors = []

    def _fail(self, node, msg):
        self.errors.append(f"line {getattr(node, 'lineno', '?')}: {msg}")

    def visit_Import(self, node):
        self._fail(node, "import is forbidden in contracts")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        self._fail(node, "import is forbidden in contracts")
        self.generic_visit(node)

    def visit_Global(self, node):
        self._fail(node, "'global' is forbidden in contracts")
        self.generic_visit(node)

    def visit_Nonlocal(self, node):
        self._fail(node, "'nonlocal' is forbidden in contracts")
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self._fail(node, "class definitions are forbidden in contracts")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in FORBIDDEN_NAMES:
            self._fail(node, f"name '{node.id}' is forbidden")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr in FORBIDDEN_ATTRIBUTES or node.attr.startswith("__"):
            self._fail(node, f"attribute access '{node.attr}' is forbidden")
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        # Direct calls to forbidden builtins by name.
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
            self._fail(node, f"calling '{func.id}' is forbidden")
        # Attribute calls that could reach object internals.
        if isinstance(func, ast.Attribute):
            if func.attr in FORBIDDEN_ATTRIBUTES or func.attr.startswith("__"):
                self._fail(node, f"calling '{func.attr}' is forbidden")
        self.generic_visit(node)


def validate_source(code):
    """Return ``(ok, message)`` after statically vetting contract source."""
    if not isinstance(code, str) or not code.strip():
        return False, "contract source is empty"
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return False, f"syntax error: {e.msg} (line {e.lineno})"
    validator = _Validator()
    validator.visit(tree)
    if validator.errors:
        return False, "; ".join(validator.errors[:5])
    return True, "ok"


# --------------------------------------------------------------------------- #
# Runtime execution
# --------------------------------------------------------------------------- #
class OutputBuffer(io.StringIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit
        self._count = 0

    def write(self, s):
        s = str(s)
        if self._count + len(s) > self.limit:
            raise SandboxError("contract output exceeded the size limit")
        self._count += len(s)
        return super().write(s)


class _InstructionLimiter:
    """Counts trace events; raises once the budget is exhausted."""

    def __init__(self, budget):
        self.budget = budget
        self.count = 0

    def __call__(self, frame, event, arg):
        self.count += 1
        if self.count > self.budget:
            raise SandboxError("contract exceeded the instruction budget")
        return self


def build_restricted_builtins(print_fn):
    """Return a ``__builtins__`` dict containing only safe builtins."""
    env = {}
    for name in SAFE_BUILTINS:
        if name == "print":
            env[name] = print_fn
        elif hasattr(__builtins__, name):
            env[name] = getattr(__builtins__, name)
        elif name in (__builtins__ if isinstance(__builtins__, dict) else {}):
            env[name] = __builtins__[name]
    return env


def exec_restricted(code, context, instruction_budget=200_000, output_limit=50_000):
    """Execute contract source in a restricted environment.

    ``context`` is a dict of globals exposed to the contract (``state``, ``msg``,
    ``emit``, …).  Returns ``{"ok": bool, "output": str, "error": str|None,
    "instructions": int}``.  Never raises to the caller.
    """
    out = OutputBuffer(output_limit)
    result = {"ok": False, "output": "", "error": None, "instructions": 0}

    ok, msg = validate_source(code)
    if not ok:
        result["error"] = msg
        return result

    env = dict(context)
    env["__builtins__"] = build_restricted_builtins(out.write)
    limiter = _InstructionLimiter(instruction_budget)

    old_trace = sys.gettrace()
    sys.settrace(limiter)
    try:
        compiled = compile(code, "<contract>", "exec")
        exec(compiled, env, env)
        result["ok"] = True
    except SandboxError as e:
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - we report, never propagate
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        sys.settrace(old_trace)

    result["output"] = out.getvalue()
    result["instructions"] = limiter.count
    return result


def call_function(code, function_name, args, context, **kwargs):
    """Execute source, then call ``function_name(*args)`` inside the sandbox."""
    out = OutputBuffer(kwargs.get("output_limit", 50_000))
    result = {"ok": False, "output": "", "error": None, "instructions": 0,
              "return": None}

    ok, msg = validate_source(code)
    if not ok:
        result["error"] = msg
        return result

    env = dict(context)
    env["__builtins__"] = build_restricted_builtins(out.write)
    limiter = _InstructionLimiter(kwargs.get("instruction_budget", 200_000))

    old_trace = sys.gettrace()
    sys.settrace(limiter)
    try:
        compiled = compile(code, "<contract>", "exec")
        exec(compiled, env, env)
        func = env.get(function_name)
        if not callable(func):
            result["error"] = f"function '{function_name}' not found in contract"
        else:
            result["return"] = func(*args)
            result["ok"] = True
    except SandboxError as e:
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        sys.settrace(old_trace)

    result["output"] = out.getvalue()
    result["instructions"] = limiter.count
    return result
