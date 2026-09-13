from __future__ import annotations

import ast
import re


class _StateProxy(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _wrap_state(value):
    if isinstance(value, dict):
        return _StateProxy({key: _wrap_state(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_wrap_state(item) for item in value]
    return value


_COMPARISON_OPS = {
    ast.Eq: lambda left, right: left == right,
    ast.NotEq: lambda left, right: left != right,
    ast.Is: lambda left, right: left is right,
    ast.IsNot: lambda left, right: left is not right,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Lt: lambda left, right: left < right,
    ast.LtE: lambda left, right: left <= right,
    ast.Gt: lambda left, right: left > right,
    ast.GtE: lambda left, right: left >= right,
}


def _normalize_expression(condition: str) -> str:
    expression = str(condition).strip()
    expression = re.sub(r"\btrue\b", "True", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\bfalse\b", "False", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\bnull\b", "None", expression, flags=re.IGNORECASE)
    return expression


def _eval_ast(node: ast.AST, state: _StateProxy) -> object:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, state)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_eval_ast(value, state)) for value in node.values)
        if isinstance(node.op, ast.Or):
            return any(bool(_eval_ast(value, state)) for value in node.values)
        raise ValueError("unsupported boolean operator")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(_eval_ast(node.operand, state))
    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left, state)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_ast(comparator, state)
            fn = _COMPARISON_OPS.get(type(op))
            if fn is None or not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id == "state":
            return state
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return None
        raise ValueError(f"unsupported name: {node.id}")
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        value = _eval_ast(node.value, state)
        if node.attr.startswith("_"):
            raise ValueError("private attributes are not allowed")
        if isinstance(value, dict):
            return value[node.attr]
        raise ValueError("attribute access is only allowed on state objects")
    if isinstance(node, ast.Subscript):
        value = _eval_ast(node.value, state)
        key = _eval_ast(node.slice, state)
        if not isinstance(value, (dict, list, tuple)):
            raise ValueError("subscript access is only allowed on collections")
        return value[key]
    if isinstance(node, ast.List):
        return [_eval_ast(elt, state) for elt in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_ast(elt, state) for elt in node.elts)
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


def eval_condition(condition: str | None, state: dict) -> bool:
    if condition is None or not str(condition).strip():
        return True
    try:
        wrapped_state = _wrap_state(state)
        expression = _normalize_expression(condition)
        tree = ast.parse(expression, mode="eval")
        return bool(_eval_ast(tree, wrapped_state))
    except Exception:
        return False
