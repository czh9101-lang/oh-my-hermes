"""Static unittest inventory discovery that executes no test-module code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from tools.test_sharding import ShardingError


@dataclass(frozen=True, slots=True)
class ClassSource:
    """One statically declared class and its resolvable inheritance edges."""

    qualified_name: str
    bases: tuple[str, ...]
    methods: tuple[str, ...]
    is_test_module: bool


@dataclass(frozen=True, slots=True)
class ModuleSource:
    """Names imported by a source file without importing that source file."""

    aliases: dict[str, str]


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _relative_module(module: str, imported: str | None, level: int) -> str:
    if level == 0:
        return imported or ""
    package = module.split(".")[:-1]
    if level > len(package) + 1:
        raise ShardingError(f"relative import escapes the test root: {module}")
    prefix = package[: len(package) - level + 1]
    return ".".join((*prefix, *(imported or "").split("."))).strip(".")


def _reference(node: ast.expr, aliases: dict[str, str], module: str) -> str:
    match node:
        case ast.Name(id=name):
            return aliases.get(name, f"{module}.{name}")
        case ast.Attribute(value=value, attr=attr):
            return f"{_reference(value, aliases, module)}.{attr}"
        case _:
            raise ShardingError(f"unsupported dynamic base class in {module}")


def _reject_dynamic_shapes(tree: ast.Module, module: str, is_test_module: bool) -> None:
    if not is_test_module:
        return
    for node in ast.walk(tree):
        match node:
            case ast.FunctionDef(name="load_tests") | ast.AsyncFunctionDef(name="load_tests"):
                raise ShardingError(f"unsupported load_tests discovery hook in {module}")
            case ast.ClassDef(body=body):
                for member in body:
                    match member:
                        case ast.Assign(targets=targets):
                            if any(
                                isinstance(target, ast.Name | ast.Attribute)
                                and (
                                    (isinstance(target, ast.Name) and target.id.startswith("test"))
                                    or (
                                        isinstance(target, ast.Attribute)
                                        and target.attr.startswith("test")
                                    )
                                )
                                for target in targets
                            ):
                                raise ShardingError(
                                    f"unsupported dynamically assigned test in {module}"
                                )
                        case ast.AnnAssign(target=ast.Name(id=name)) if name.startswith("test"):
                            raise ShardingError(
                                f"unsupported dynamically assigned test in {module}"
                            )
                        case ast.AnnAssign(target=ast.Attribute(attr=name)) if name.startswith("test"):
                            raise ShardingError(
                                f"unsupported dynamically assigned test in {module}"
                            )
            case ast.Assign(targets=targets):
                if any(
                    isinstance(target, ast.Attribute) and target.attr.startswith("test")
                    for target in targets
                ):
                    raise ShardingError(f"unsupported dynamically assigned test in {module}")
            case ast.AnnAssign(target=ast.Attribute(attr=name)) if name.startswith("test"):
                raise ShardingError(f"unsupported dynamically assigned test in {module}")
            case ast.Call(func=ast.Name(id="setattr"), args=arguments) if len(arguments) >= 2:
                name = arguments[1]
                if not isinstance(name, ast.Constant) or not isinstance(name.value, str) or name.value.startswith("test"):
                    raise ShardingError(f"unsupported dynamically assigned test in {module}")


def _parse_sources(root: Path) -> tuple[dict[str, ClassSource], dict[str, ModuleSource]]:
    classes: dict[str, ClassSource] = {}
    modules: dict[str, ModuleSource] = {}
    for path in sorted(root.rglob("*.py")):
        module = _module_name(root, path)
        if not module:
            continue
        is_test_module = fnmatch(path.name, "test*.py")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ShardingError(f"static discovery failed for {path}: {exc}") from exc
        _reject_dynamic_shapes(tree, module, is_test_module)
        aliases: dict[str, str] = {}
        for node in tree.body:
            match node:
                case ast.Import(names=names):
                    for imported in names:
                        aliases[imported.asname or imported.name.split(".")[0]] = imported.name
                case ast.ImportFrom(module=imported, level=level, names=names):
                    base = _relative_module(module, imported, level)
                    for imported_name in names:
                        if imported_name.name == "*":
                            raise ShardingError(f"unsupported wildcard import in {module}")
                        aliases[imported_name.asname or imported_name.name] = (
                            f"{base}.{imported_name.name}".strip(".")
                        )
        modules[module] = ModuleSource(aliases=aliases)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            qualified_name = f"{module}.{node.name}"
            if qualified_name in classes:
                raise ShardingError(f"duplicate static class name: {qualified_name}")
            methods = tuple(
                member.name
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name.startswith("test")
            )
            classes[qualified_name] = ClassSource(
                qualified_name=qualified_name,
                bases=tuple(_reference(base, aliases, module) for base in node.bases),
                methods=methods,
                is_test_module=is_test_module,
            )
    return classes, modules


def _is_test_case(name: str, classes: dict[str, ClassSource], trail: frozenset[str] = frozenset()) -> bool:
    if name in {"unittest.TestCase", "unittest.case.TestCase"}:
        return True
    if name in trail:
        raise ShardingError(f"cyclic test-class inheritance: {name}")
    source = classes.get(name)
    return source is not None and any(
        _is_test_case(base, classes, trail | {name}) for base in source.bases
    )


def _test_methods(name: str, classes: dict[str, ClassSource], trail: frozenset[str] = frozenset()) -> tuple[str, ...]:
    if name in trail:
        raise ShardingError(f"cyclic test-class inheritance: {name}")
    source = classes[name]
    inherited = {
        method
        for base in source.bases
        if base in classes
        for method in _test_methods(base, classes, trail | {name})
    }
    return tuple(sorted(set(source.methods) | inherited))


def discover_inventory(start_dir: Path) -> tuple[str, ...]:
    """Return unittest's static, exact inventory without importing test modules."""

    if not start_dir.is_dir():
        raise ShardingError(f"discovery start directory is missing: {start_dir}")
    classes, _ = _parse_sources(start_dir)
    inventory = tuple(
        sorted(
            f"{source.qualified_name}.{method}"
            for source in classes.values()
            if source.is_test_module and _is_test_case(source.qualified_name, classes)
            for method in _test_methods(source.qualified_name, classes)
        )
    )
    if not inventory:
        raise ShardingError(f"static discovery found no tests under {start_dir}")
    if len(inventory) != len(set(inventory)):
        raise ShardingError("static discovery produced duplicate test IDs")
    return inventory
