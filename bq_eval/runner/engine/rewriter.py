"""Scoped dataset-qualified table flattening and BigQuery STRUCT path rewriting for SQLite."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlglot import exp

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_PROJ_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def parse_qualified_parts(raw_name: str) -> tuple[str | None, str]:
    """Return `(dataset, object_name)` after stripping optional project prefix and backticks."""
    cleaned = raw_name.strip().replace("`", "")
    parts = [p for p in cleaned.split(".") if p]
    if not parts or len(parts) > 3:
        raise ValueError(f"Unsupported SQL object name: {raw_name!r}")
    if len(parts) == 3:
        proj, dataset, obj = parts
        if not _SAFE_PROJ_RE.fullmatch(proj):
            raise ValueError(f"Invalid project identifier in {raw_name!r}")
        if not _SAFE_IDENT_RE.fullmatch(dataset) or not _SAFE_IDENT_RE.fullmatch(obj):
            raise ValueError(f"Invalid dataset/object identifier in {raw_name!r}")
        return dataset, obj
    if len(parts) == 2:
        dataset, obj = parts
        if not _SAFE_IDENT_RE.fullmatch(dataset) or not _SAFE_IDENT_RE.fullmatch(obj):
            raise ValueError(f"Invalid dataset/object identifier in {raw_name!r}")
        return dataset, obj
    obj = parts[0]
    if not _SAFE_IDENT_RE.fullmatch(obj):
        raise ValueError(f"Invalid object identifier in {raw_name!r}")
    return None, obj


def sqlite_table_name(raw_name: str) -> str:
    """Map `[project.]dataset.table` to `dataset__table` (or bare `table` if unqualified)."""
    dataset, obj = parse_qualified_parts(raw_name)
    return f"{dataset}__{obj}" if dataset else obj


def _extract_table_parts(table_node: exp.Table) -> tuple[str | None, str] | None:
    if isinstance(table_node.this, exp.Anonymous):
        return None
    parts: list[str] = []
    if table_node.catalog:
        parts.append(table_node.catalog)
    if table_node.db:
        parts.append(table_node.db)
    if table_node.name:
        parts.append(table_node.name)
    if not parts:
        return None
    joined = ".".join(parts)
    return parse_qualified_parts(joined)


@dataclass(frozen=True)
class _ScopeBindings:
    aliases: frozenset[str]
    qualified: Mapping[tuple[str, str], str]
    implicit: Mapping[str, str | None]


_EMPTY_SCOPE = _ScopeBindings(frozenset(), {}, {})


def _merge_scope(outer: _ScopeBindings, local: _ScopeBindings) -> _ScopeBindings:
    merged_qualified = dict(outer.qualified)
    merged_qualified.update(local.qualified)
    merged_implicit = dict(outer.implicit)
    merged_implicit.update(local.implicit)
    return _ScopeBindings(
        aliases=outer.aliases | local.aliases,
        qualified=merged_qualified,
        implicit=merged_implicit,
    )


def _rewrite_table_node(
    node: exp.Table,
    *,
    qualify_datasets: bool,
    default_dataset: str | None,
    active_ctes: frozenset[str],
    flat_to_original: dict[str, tuple[str | None, str]],
) -> exp.Table:
    if isinstance(node.this, exp.Anonymous):
        return node.copy()
    parts = _extract_table_parts(node)
    if parts is None:
        return node.copy()
    dataset, obj = parts
    if dataset is None and not node.catalog:
        if default_dataset and qualify_datasets and obj.lower() not in active_ctes:
            flat_name = f"{default_dataset}__{obj}"
            flat_to_original[flat_name] = (default_dataset, obj)
            rebuilt = exp.Table(this=exp.to_identifier(flat_name, quoted=False))
            if node.args.get("alias") is not None:
                rebuilt.set("alias", node.args["alias"].copy())
            return rebuilt
        return node.copy()
    flat_name = f"{dataset}__{obj}" if (qualify_datasets and dataset) else obj
    flat_to_original[flat_name] = (dataset, obj)
    rebuilt = exp.Table(this=exp.to_identifier(flat_name, quoted=False))
    if node.args.get("alias") is not None:
        rebuilt.set("alias", node.args["alias"].copy())
    return rebuilt


def _rewrite_tables_scoped(
    node: exp.Expr,
    *,
    qualify_datasets: bool,
    default_dataset: str | None,
    active_ctes: frozenset[str],
    flat_to_original: dict[str, tuple[str | None, str]],
) -> exp.Expr:
    if isinstance(node, exp.Table):
        return _rewrite_table_node(
            node,
            qualify_datasets=qualify_datasets,
            default_dataset=default_dataset,
            active_ctes=active_ctes,
            flat_to_original=flat_to_original,
        )
    cloned: exp.Expr = node.copy()
    next_ctes = set(active_ctes)
    with_clause = cloned.args.get("with_")
    if isinstance(with_clause, exp.With):
        new_ctes: list[exp.CTE] = []
        for cte in with_clause.expressions:
            if isinstance(cte, exp.CTE) and cte.this is not None:
                rewritten_cte = cte.copy()
                rewritten_cte.set(
                    "this",
                    _rewrite_tables_scoped(
                        cte.this,
                        qualify_datasets=qualify_datasets,
                        default_dataset=default_dataset,
                        active_ctes=frozenset(next_ctes),
                        flat_to_original=flat_to_original,
                    ),
                )
                new_ctes.append(rewritten_cte)
                if cte.alias:
                    next_ctes.add(cte.alias.lower())
            else:
                new_ctes.append(cte)
        new_with = with_clause.copy()
        new_with.set("expressions", new_ctes)
        cloned.set("with_", new_with)
    scoped_ctes = frozenset(next_ctes)
    for key, value in list(cloned.args.items()):
        if key == "with_":
            continue
        if isinstance(value, exp.Expr):
            cloned.set(
                key,
                _rewrite_tables_scoped(
                    value,
                    qualify_datasets=qualify_datasets,
                    default_dataset=default_dataset,
                    active_ctes=scoped_ctes,
                    flat_to_original=flat_to_original,
                ),
            )
        elif isinstance(value, list):
            cloned.set(
                key,
                [
                    _rewrite_tables_scoped(
                        item,
                        qualify_datasets=qualify_datasets,
                        default_dataset=default_dataset,
                        active_ctes=scoped_ctes,
                        flat_to_original=flat_to_original,
                    )
                    if isinstance(item, exp.Expr)
                    else item
                    for item in value
                ],
            )
    return cloned


def _rewrite_qualified_tables(
    ast_node: exp.Expr,
    *,
    qualify_datasets: bool,
    default_dataset: str | None = None,
) -> tuple[exp.Expr, dict[str, tuple[str | None, str]]]:
    flat_to_original: dict[str, tuple[str | None, str]] = {}
    rewritten = _rewrite_tables_scoped(
        ast_node,
        qualify_datasets=qualify_datasets,
        default_dataset=default_dataset,
        active_ctes=frozenset(),
        flat_to_original=flat_to_original,
    )
    return rewritten, flat_to_original


def _record_unaliased_table(
    table_name: str,
    flat_to_original: Mapping[str, tuple[str | None, str]],
    qualified: dict[tuple[str, str], str],
    implicit: dict[str, str | None],
) -> None:
    orig = flat_to_original.get(table_name)
    if orig is None:
        return
    dataset, obj = orig
    if dataset is not None:
        qualified[(dataset.lower(), obj.lower())] = table_name
    obj_key = obj.lower()
    if obj_key not in implicit:
        implicit[obj_key] = table_name
    elif implicit[obj_key] != table_name:
        implicit[obj_key] = None


def _collect_local_scope(
    select_node: exp.Select,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> _ScopeBindings:
    aliases: set[str] = set()
    qualified: dict[tuple[str, str], str] = {}
    implicit: dict[str, str | None] = {}
    sources: list[exp.Expr] = []
    from_clause = select_node.args.get("from_")
    if isinstance(from_clause, exp.From) and from_clause.this is not None:
        sources.append(from_clause.this)
    for join in select_node.args.get("joins") or []:
        if isinstance(join, exp.Join) and join.this is not None:
            sources.append(join.this)
    for src in sources:
        if src.alias:
            aliases.add(src.alias.lower())
        elif isinstance(src, exp.Table) and src.name:
            aliases.add(src.name.lower())
            _record_unaliased_table(src.name, flat_to_original, qualified, implicit)
    return _ScopeBindings(frozenset(aliases), qualified, implicit)


def _extract_dotted_parts(node: exp.Expr) -> list[str] | None:
    if isinstance(node, exp.Column):
        parts = [p for p in (node.catalog, node.db, node.table, node.name) if p]
        return parts or None
    if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Identifier):
        left = _extract_dotted_parts(node.this)
        if left is not None:
            return [*left, node.expression.name]
    return None


def _build_table_or_struct_expr(table_name: str, rest: list[str]) -> exp.Expr:
    base = exp.Column(this=exp.to_identifier(rest[0]), table=exp.to_identifier(table_name))
    if len(rest) == 1:
        return base
    return exp.Anonymous(
        this="BQ_STRUCT_FIELD",
        expressions=[base, exp.Literal.string(".".join(rest[1:]))],
    )


def _rewrite_dotted_path(parts: list[str], *, scope: _ScopeBindings) -> exp.Expr:
    if len(parts) >= 3:
        flat = scope.qualified.get((parts[0].lower(), parts[1].lower()))
        if flat is not None:
            return _build_table_or_struct_expr(flat, parts[2:])
    if parts[0].lower() in scope.aliases:
        return _build_table_or_struct_expr(parts[0], parts[1:])
    implicit_flat = scope.implicit.get(parts[0].lower())
    if implicit_flat is not None:
        return _build_table_or_struct_expr(implicit_flat, parts[1:])
    base = exp.Column(this=exp.to_identifier(parts[0]))
    return exp.Anonymous(
        this="BQ_STRUCT_FIELD",
        expressions=[base, exp.Literal.string(".".join(parts[1:]))],
    )


def _rewrite_from_or_join_source(
    source: exp.Expr,
    *,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> exp.Expr:
    if isinstance(source, exp.Subquery) and source.this is not None:
        rebuilt = source.copy()
        rebuilt.set(
            "this",
            _rewrite_struct_paths(
                source.this,
                scope=_EMPTY_SCOPE,
                flat_to_original=flat_to_original,
            ),
        )
        return rebuilt
    return _rewrite_struct_paths(
        source,
        scope=_EMPTY_SCOPE,
        flat_to_original=flat_to_original,
    )


def _rewrite_join_item(
    item: object,
    *,
    next_scope: _ScopeBindings,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> object:
    if not isinstance(item, exp.Join):
        return item
    new_join = item.copy()
    if new_join.this is not None:
        new_join.set(
            "this",
            _rewrite_from_or_join_source(
                new_join.this,
                flat_to_original=flat_to_original,
            ),
        )
    for jkey, jval in list(new_join.args.items()):
        if jkey != "this" and isinstance(jval, exp.Expr):
            new_join.set(
                jkey,
                _rewrite_struct_paths(
                    jval,
                    scope=next_scope,
                    flat_to_original=flat_to_original,
                ),
            )
    return new_join


def _rewrite_struct_child(
    value: object,
    *,
    scope: _ScopeBindings,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> object:
    if isinstance(value, exp.Expr):
        return _rewrite_struct_paths(
            value,
            scope=scope,
            flat_to_original=flat_to_original,
        )
    if isinstance(value, list):
        return [
            _rewrite_struct_paths(
                item,
                scope=scope,
                flat_to_original=flat_to_original,
            )
            if isinstance(item, exp.Expr)
            else item
            for item in value
        ]
    return value


def _rewrite_select_struct_paths(
    node: exp.Select,
    *,
    scope: _ScopeBindings,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> exp.Select:
    cloned = node.copy()
    next_scope = _merge_scope(scope, _collect_local_scope(node, flat_to_original))
    for key, value in list(cloned.args.items()):
        if key == "with_" and isinstance(value, exp.With):
            cloned.set(
                key,
                _rewrite_struct_paths(
                    value,
                    scope=_EMPTY_SCOPE,
                    flat_to_original=flat_to_original,
                ),
            )
        elif key == "from_" and isinstance(value, exp.From) and value.this is not None:
            new_from = value.copy()
            new_from.set(
                "this",
                _rewrite_from_or_join_source(
                    value.this,
                    flat_to_original=flat_to_original,
                ),
            )
            cloned.set(key, new_from)
        elif key == "joins" and isinstance(value, list):
            cloned.set(
                key,
                [
                    _rewrite_join_item(
                        item,
                        next_scope=next_scope,
                        flat_to_original=flat_to_original,
                    )
                    for item in value
                ],
            )
        else:
            cloned.set(
                key,
                _rewrite_struct_child(
                    value,
                    scope=next_scope,
                    flat_to_original=flat_to_original,
                ),
            )
    return cloned


def _rewrite_struct_paths(
    node: exp.Expr,
    *,
    scope: _ScopeBindings = _EMPTY_SCOPE,
    flat_to_original: Mapping[str, tuple[str | None, str]] | None = None,
) -> exp.Expr:
    orig_map: Mapping[str, tuple[str | None, str]] = flat_to_original or {}
    if isinstance(node, (exp.Column, exp.Dot)):
        parts = _extract_dotted_parts(node)
        if parts is not None and len(parts) >= 2:
            return _rewrite_dotted_path(parts, scope=scope)
        if isinstance(node, exp.Column):
            return node.copy()
    if isinstance(node, exp.Select):
        return _rewrite_select_struct_paths(
            node,
            scope=scope,
            flat_to_original=orig_map,
        )
    cloned: exp.Expr = node.copy()
    for key, value in list(cloned.args.items()):
        child_scope = _EMPTY_SCOPE if key == "with_" else scope
        cloned.set(
            key,
            _rewrite_struct_child(
                value,
                scope=child_scope,
                flat_to_original=orig_map,
            ),
        )
    return cloned
