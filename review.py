"""What a routine does, read from its code without running it, for the approval prompt.

The prompt only has room for the first lines of a script. This summary covers all of it: every
app it opens, every adb shell command, what it types and taps, and anything it imports (a routine
runs on this computer, so an import can reach past the phone).
"""
from __future__ import annotations

import ast

_SHOW = 6  # entries per category before "and N more"
_VALUE_CHARS = 60  # a parameter value longer than this is cut short

# helper name -> category label, in display order
_CALLS = {
    "open_app": "Opens apps",
    "shell": "Runs adb shell",
    "type_text": "Types",
    "tap_ui": "Taps by label",
    "tap_text": "Taps by label",
    "tap_image": "Taps by image",
    "step": "Steps",
}
_COUNTED = {"tap": "taps at coordinates", "swipe": "swipes", "checkpoint": "checkpoints"}


def _assignments(tree: ast.AST) -> dict[str, ast.expr]:
    """name -> what it was set to, for names assigned exactly once (spotify = PARAMS[...])."""
    seen: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            seen.setdefault(node.targets[0].id, []).append(node.value)
    return {name: values[0] for name, values in seen.items() if len(values) == 1}


def _param_value(node: ast.expr, params: dict) -> str | None:
    """The value a PARAMS['key'] lookup gets on this run, shown short; None for anything else."""
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "PARAMS"
            and isinstance(node.slice, ast.Constant) and node.slice.value in params):
        value = repr(params[node.slice.value])
        return value if len(value) <= _VALUE_CHARS else value[:_VALUE_CHARS - 1] + "…"
    return None


def _arg(node: ast.Call, source: str, names: dict[str, ast.expr], params: dict) -> str:
    if not node.args:
        return "(no argument)"
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return repr(first.value)
    text = ast.get_source_segment(source, first) or "(computed)"
    if isinstance(first, ast.Name) and first.id in names:
        first = names[first.id]
        text = f"{text} ({ast.get_source_segment(source, first) or '(computed)'})"
    value = _param_value(first, params)
    return f"{text} = {value}" if value else text


def _called(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")


def _imports(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or ".")
    return sorted(set(names))


def _line(label: str, items: list[str]) -> str:
    unique = list(dict.fromkeys(items))
    shown = ", ".join(unique[:_SHOW])
    more = f" and {len(unique) - _SHOW} more" if len(unique) > _SHOW else ""
    return f"  {label}: {shown}{more}"


def summarize(code: str, params: dict | None = None) -> str:
    """A few lines naming everything the script can do; never raises.

    `params` are the values PARAMS holds on this run, so `open_app(PARAMS['app'])` reads as the app
    it opens rather than as a name.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"  (does not parse: {exc.msg} on line {exc.lineno})"
    names = _assignments(tree)
    params = params if isinstance(params, dict) else {}
    found: dict[str, list[str]] = {}
    counts = dict.fromkeys(_COUNTED, 0)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called(node)
        if name in _CALLS:
            found.setdefault(_CALLS[name], []).append(_arg(node, code, names, params))
        elif name in counts:
            counts[name] += 1
    lines = [_line(label, found[label]) for label in dict.fromkeys(_CALLS.values()) if label in found]
    tally = [f"{n} {_COUNTED[name]}" for name, n in counts.items() if n]
    if tally:
        lines.append("  Also: " + ", ".join(tally))
    imports = _imports(tree)
    if imports:
        lines.append(_line("Imports (runs on this computer)", imports))
    return "\n".join(lines) or "  (calls no phone helpers)"
