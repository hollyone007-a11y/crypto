"""Static allowlist check for LLM-written strategy files (strategies/candidates/).

Not a real sandbox. It is one layer: code that passes is still only executed in
a key-less subprocess for evaluation, and in-process only after promotion."""
import ast

ALLOWED_IMPORTS = {"math", "numpy", "pandas", "dataclasses", "typing",
                   "research.features", "strategies.base"}
BANNED_NAMES = {"exec", "eval", "compile", "open", "__import__", "globals", "locals", "vars",
                "getattr", "setattr", "delattr", "input", "breakpoint", "help", "memoryview",
                "object", "type", "super", "classmethod", "staticmethod", "property"}
BANNED_ATTRS = {"eval", "query", "load", "save", "savez", "fromfile", "tofile", "loadtxt",
                "savetxt", "genfromtxt", "memmap", "pipe", "system", "popen", "ctypeslib"}
BANNED_ATTR_PREFIXES = ("read_", "to_csv", "to_pickle", "to_parquet", "to_sql", "to_json",
                        "to_html", "to_excel", "to_hdf", "to_feather", "to_clipboard",
                        "to_latex", "to_markdown", "to_stata", "to_orc", "to_xml", "_")


class CodeRejected(ValueError):
    pass


def check_source(src: str, class_name: str) -> None:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise CodeRejected(f"syntax error: {e}") from e
    classes = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m not in ALLOWED_IMPORTS:
                    raise CodeRejected(f"import not allowed: {m}")
            if isinstance(node, ast.ImportFrom) and node.level:
                raise CodeRejected("relative imports not allowed")
        elif isinstance(node, ast.ClassDef):
            classes.append(node)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            pass
        elif isinstance(node, ast.FunctionDef):
            pass
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            pass  # docstring
        else:
            raise CodeRejected(f"top-level statement not allowed: {type(node).__name__}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await,
                             ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.While)):
            raise CodeRejected(f"construct not allowed: {type(node).__name__}")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body:
            raise CodeRejected("nested import not allowed")
        if isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            raise CodeRejected(f"name not allowed: {node.id}")
        if isinstance(node, ast.Attribute):
            a = node.attr
            if a in BANNED_ATTRS or a.startswith(BANNED_ATTR_PREFIXES):
                raise CodeRejected(f"attribute not allowed: .{a}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            raise CodeRejected("dunder strings not allowed")
    target = [c for c in classes if c.name == class_name]
    if len(target) != 1:
        raise CodeRejected(f"must define exactly one class {class_name}")
    bases = {b.id for b in target[0].bases if isinstance(b, ast.Name)}
    if "Strategy" not in bases:
        raise CodeRejected(f"{class_name} must subclass Strategy")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "signals" for n in target[0].body):
        raise CodeRejected(f"{class_name} must define signals()")
