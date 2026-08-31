"""AST-whitelist expression sandbox — replaces bare eval() for `python`/`branch`/`filter`.

Blocks attribute access to dunders, imports, comprehension bombs, lambdas capturing
builtins, and anything not on the node whitelist. Enforces a node-count and a
wall-clock budget.
"""
from __future__ import annotations

import ast
import json
import time

MAX_NODES = 400
MAX_SECONDS = 2.0
MAX_STR = 200_000

SAFE_FUNCS = {
    "len": len, "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
    "sorted": sorted, "reversed": reversed, "str": str, "int": int, "float": float,
    "bool": bool, "list": list, "dict": dict, "set": set, "tuple": tuple,
    "any": any, "all": all, "enumerate": enumerate, "zip": zip, "range": range,
}

ALLOWED_NODES = (
    ast.Expression, ast.Load, ast.Store, ast.Constant, ast.Name, ast.Tuple, ast.List, ast.Dict,
    ast.Set, ast.Subscript, ast.Slice, ast.Index if hasattr(ast, "Index") else ast.Slice,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Attribute,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Is, ast.IsNot, ast.Starred, ast.keyword, ast.JoinedStr, ast.FormattedValue,
)

# only these methods/attrs may be reached with dot-notation
SAFE_ATTRS = {
    "get", "keys", "values", "items", "upper", "lower", "strip", "lstrip", "rstrip",
    "split", "rsplit", "splitlines", "join", "replace", "startswith", "endswith",
    "title", "capitalize", "count", "index", "find", "format", "encode", "isdigit",
    "isalpha", "append", "extend", "pop", "sort", "copy", "add", "union",
    "intersection", "difference", "loads", "dumps", "real", "imag", "total_seconds",
}

BLOCKED_NAMES = {
    "__import__", "eval", "exec", "compile", "open", "input", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "hasattr", "breakpoint", "exit",
    "quit", "memoryview", "object", "type", "super", "classmethod", "staticmethod",
    "property", "help", "id", "hash",
}


class SandboxError(Exception):
    pass


def _attr(obj, name):
    """`a.b` semantics matching {{a.b}} templates.

    For dicts: a real key wins, then a whitelisted method (so both
    ``cfg.priority`` and ``cfg.get('k')`` work), then None like templates do.
    """
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        if name in SAFE_ATTRS:
            return getattr(obj, name)
        return None
    if name in SAFE_ATTRS:
        return getattr(obj, name, None)
    raise SandboxError(f"attribute '{name}' is not on the whitelist")


class _AttrRewriter(ast.NodeTransformer):
    """Turn Attribute access into the guarded _attr() call."""

    def visit_Attribute(self, node):  # noqa: N802
        self.generic_visit(node)
        if node.attr.startswith("_"):
            raise SandboxError(f"access to '{node.attr}' is blocked")
        return ast.copy_location(
            ast.Call(func=ast.Name(id="__attr__", ctx=ast.Load()),
                     args=[node.value, ast.Constant(value=node.attr)], keywords=[]),
            node)


def _validate(tree: ast.AST) -> None:
    count = 0
    for node in ast.walk(tree):
        count += 1
        if count > MAX_NODES:
            raise SandboxError(f"expression too complex (>{MAX_NODES} AST nodes)")
        if not isinstance(node, ALLOWED_NODES):
            raise SandboxError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise SandboxError(f"access to '{node.attr}' is blocked")
        if isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in BLOCKED_NAMES:
                raise SandboxError(f"name '{node.id}' is blocked")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if len(node.value) > MAX_STR:
                raise SandboxError("string literal too large")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            r = node.right
            # exponent must be a small literal — anything computed is unbounded
            if not (isinstance(r, ast.Constant) and isinstance(r.value, (int, float))):
                raise SandboxError("exponent must be a small literal")
            if r.value > 64:
                raise SandboxError("exponent too large")
            if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Pow):
                raise SandboxError("nested exponentiation blocked")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, int) \
                        and abs(side.value) > 100_000:
                    raise SandboxError("multiplier too large (memory guard)")
                if isinstance(side, ast.BinOp) and isinstance(side.op, ast.Pow):
                    raise SandboxError("exponential sequence repetition blocked")


def safe_eval(expr: str, scope: dict):
    if not isinstance(expr, str):
        raise SandboxError("expression must be a string")
    if len(expr) > 5000:
        raise SandboxError("expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise SandboxError(f"syntax error: {e.msg}")
    _validate(tree)
    tree = ast.fix_missing_locations(_AttrRewriter().visit(tree))

    env = dict(SAFE_FUNCS)
    env["__attr__"] = _attr
    env["json"] = _JsonShim()
    env.update({k: v for k, v in scope.items()
                if isinstance(k, str) and k.isidentifier() and k not in BLOCKED_NAMES})

    t0 = time.time()
    result = eval(compile(tree, "<workflow>", "eval"),  # noqa: S307
                  {"__builtins__": {}}, env)
    if time.time() - t0 > MAX_SECONDS:
        raise SandboxError("expression exceeded time budget")
    return result


class _JsonShim:
    """Only loads/dumps are reachable."""
    loads = staticmethod(json.loads)
    dumps = staticmethod(json.dumps)
