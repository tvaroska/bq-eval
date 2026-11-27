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
    unnest_aliases: frozenset[str] = frozenset()
    unnest_offsets: Mapping[str, str] = ()  # type: ignore[assignment]


_EMPTY_SCOPE = _ScopeBindings(frozenset(), {}, {}, frozenset(), {})


def _merge_scope(outer: _ScopeBindings, local: _ScopeBindings) -> _ScopeBindings:
    merged_qualified = dict(outer.qualified)
    merged_qualified.update(local.qualified)
    merged_implicit = dict(outer.implicit)
    merged_implicit.update(local.implicit)
    merged_offsets = dict(outer.unnest_offsets)
    merged_offsets.update(local.unnest_offsets)
    return _ScopeBindings(
        aliases=outer.aliases | local.aliases,
        qualified=merged_qualified,
        implicit=merged_implicit,
        unnest_aliases=outer.unnest_aliases | local.unnest_aliases,
        unnest_offsets=merged_offsets,
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
        return node
    parts = _extract_table_parts(node)
    if parts is None:
        return node
    dataset, obj = parts
    if dataset is None and not node.catalog:
        if (
            default_dataset
            and qualify_datasets
            and obj.lower() not in active_ctes
            and not obj.lower().startswith("_bq_tvf_")
        ):
            flat_name = f"{default_dataset}__{obj}"
            flat_to_original[flat_name] = (default_dataset, obj)
            rebuilt = exp.Table(this=exp.to_identifier(flat_name, quoted=False))
            if node.args.get("alias") is not None:
                rebuilt.set("alias", node.args["alias"])
            return rebuilt
        return node
    flat_name = f"{dataset}__{obj}" if (qualify_datasets and dataset) else obj
    flat_to_original[flat_name] = (dataset, obj)
    rebuilt = exp.Table(this=exp.to_identifier(flat_name, quoted=False))
    if node.args.get("alias") is not None:
        rebuilt.set("alias", node.args["alias"])
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
    next_ctes = set(active_ctes)
    with_clause = node.args.get("with_")
    if isinstance(with_clause, exp.With):
        for cte in with_clause.expressions:
            if isinstance(cte, exp.CTE) and cte.this is not None:
                cte.set(
                    "this",
                    _rewrite_tables_scoped(
                        cte.this,
                        qualify_datasets=qualify_datasets,
                        default_dataset=default_dataset,
                        active_ctes=frozenset(next_ctes),
                        flat_to_original=flat_to_original,
                    ),
                )
                if cte.alias:
                    next_ctes.add(cte.alias.lower())
    scoped_ctes = frozenset(next_ctes)
    for key, value in list(node.args.items()):
        if key == "with_":
            continue
        if isinstance(value, exp.Expr):
            node.set(
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
            node.set(
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
    return node


def _rewrite_qualified_tables(
    ast_node: exp.Expr,
    *,
    qualify_datasets: bool,
    default_dataset: str | None = None,
    preserved_tables: frozenset[str] = frozenset(),
) -> tuple[exp.Expr, dict[str, tuple[str | None, str]]]:
    flat_to_original: dict[str, tuple[str | None, str]] = {}
    rewritten = _rewrite_tables_scoped(
        ast_node.copy(),
        qualify_datasets=qualify_datasets,
        default_dataset=default_dataset,
        active_ctes=preserved_tables,
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


def _unnest_array_expr(unnest: exp.Unnest) -> exp.Expr:
    target = unnest.expressions[0].copy() if unnest.expressions else exp.Literal.string("[]")
    if isinstance(target, exp.Anonymous) and target.name.upper() in {
        "JSON_QUERY_ARRAY",
        "JSON_EXTRACT_ARRAY",
        "JSON_VALUE_ARRAY",
        "JSON_EXTRACT_STRING_ARRAY",
    }:
        if len(target.expressions) >= 2:
            extracted = exp.Anonymous(
                this="BQ_JSON_QUERY",
                expressions=[target.expressions[0].copy(), target.expressions[1].copy()],
            )
        else:
            extracted = target.expressions[0].copy()
        return exp.Anonymous(this="BQ_JSON_ENSURE_ARRAY", expressions=[extracted])
    if isinstance(target, (exp.JSONValueArray, exp.JSONExtractArray)) and target.this is not None:
        raw_path = (
            target.expression.sql("bigquery").strip("'") if target.expression is not None else "$"
        )
        if raw_path and raw_path != "$":
            extracted = exp.Anonymous(
                this="BQ_JSON_QUERY",
                expressions=[target.this.copy(), exp.Literal.string(raw_path)],
            )
        else:
            extracted = target.this.copy()
        return exp.Anonymous(this="BQ_JSON_ENSURE_ARRAY", expressions=[extracted])
    return exp.Anonymous(this="BQ_JSON_ENSURE_ARRAY", expressions=[target])


def _extract_unnest_alias_and_offset(unnest: exp.Unnest) -> tuple[str | None, str | None]:
    alias_name: str | None = unnest.alias or None
    alias_node = unnest.args.get("alias")
    if alias_name is None and isinstance(alias_node, exp.TableAlias) and alias_node.columns:
        alias_name = alias_node.columns[0].name
    offset_expr = unnest.args.get("offset")
    offset_name: str | None = None
    if isinstance(offset_expr, exp.Identifier):
        offset_name = offset_expr.name
    elif isinstance(offset_expr, exp.Expr):
        offset_name = offset_expr.alias or offset_expr.name or None
    return alias_name, offset_name


def _collect_local_scope(
    select_node: exp.Select,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> _ScopeBindings:
    aliases: set[str] = set()
    qualified: dict[tuple[str, str], str] = {}
    implicit: dict[str, str | None] = {}
    unnest_aliases: set[str] = set()
    unnest_offsets: dict[str, str] = {}
    sources: list[exp.Expr] = []
    from_clause = select_node.args.get("from_")
    if isinstance(from_clause, exp.From) and from_clause.this is not None:
        sources.append(from_clause.this)
    for join in select_node.args.get("joins") or []:
        if isinstance(join, exp.Join) and join.this is not None:
            sources.append(join.this)
    for src in sources:
        if isinstance(src, exp.Unnest):
            alias_name, offset_name = _extract_unnest_alias_and_offset(src)
            if alias_name:
                aliases.add(alias_name.lower())
                unnest_aliases.add(alias_name.lower())
                if offset_name:
                    unnest_offsets[offset_name.lower()] = alias_name
            continue
        if src.alias:
            aliases.add(src.alias.lower())
        elif isinstance(src, exp.Table) and src.name:
            aliases.add(src.name.lower())
            _record_unaliased_table(src.name, flat_to_original, qualified, implicit)
    return _ScopeBindings(
        frozenset(aliases),
        qualified,
        implicit,
        frozenset(unnest_aliases),
        unnest_offsets,
    )


def _extract_dotted_parts(node: exp.Expr) -> list[str] | None:
    if isinstance(node, exp.Column):
        if isinstance(node.this, exp.Star):
            return None
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
    if parts[0].lower() in scope.unnest_aliases:
        val_col = exp.Column(this=exp.to_identifier("value"), table=exp.to_identifier(parts[0]))
        return exp.Anonymous(
            this="BQ_STRUCT_FIELD",
            expressions=[val_col, exp.Literal.string(".".join(parts[1:]))],
        )
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
    scope: _ScopeBindings = _EMPTY_SCOPE,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> exp.Expr:
    if isinstance(source, exp.Unnest):
        alias_name, _ = _extract_unnest_alias_and_offset(source)
        arr_expr = _rewrite_struct_paths_in_place(
            _unnest_array_expr(source),
            scope=scope,
            flat_to_original=flat_to_original,
        )
        return exp.Table(
            this=exp.Anonymous(this="json_each", expressions=[arr_expr]),
            alias=exp.TableAlias(this=exp.to_identifier(alias_name)) if alias_name else None,
        )
    if isinstance(source, exp.Subquery) and source.this is not None:
        source.set(
            "this",
            _rewrite_struct_paths_in_place(
                source.this,
                scope=_EMPTY_SCOPE,
                flat_to_original=flat_to_original,
            ),
        )
        return source
    return _rewrite_struct_paths_in_place(
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
    was_unnest = isinstance(item.this, exp.Unnest)
    if item.this is not None:
        item.set(
            "this",
            _rewrite_from_or_join_source(
                item.this,
                scope=next_scope,
                flat_to_original=flat_to_original,
            ),
        )
    if was_unnest and item.args.get("side") and item.args.get("on") is None:
        item.set("on", exp.Boolean(this=True))
    for jkey, jval in list(item.args.items()):
        if jkey != "this" and isinstance(jval, exp.Expr):
            item.set(
                jkey,
                _rewrite_struct_paths_in_place(
                    jval,
                    scope=next_scope,
                    flat_to_original=flat_to_original,
                ),
            )
    return item


def _rewrite_struct_child(
    value: object,
    *,
    scope: _ScopeBindings,
    flat_to_original: Mapping[str, tuple[str | None, str]],
) -> object:
    if isinstance(value, exp.Expr):
        return _rewrite_struct_paths_in_place(
            value,
            scope=scope,
            flat_to_original=flat_to_original,
        )
    if isinstance(value, list):
        return [
            _rewrite_struct_paths_in_place(
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
    next_scope = _merge_scope(scope, _collect_local_scope(node, flat_to_original))
    for key, value in list(node.args.items()):
        if key == "with_" and isinstance(value, exp.With):
            node.set(
                key,
                _rewrite_struct_paths_in_place(
                    value,
                    scope=_EMPTY_SCOPE,
                    flat_to_original=flat_to_original,
                ),
            )
        elif key == "from_" and isinstance(value, exp.From) and value.this is not None:
            value.set(
                "this",
                _rewrite_from_or_join_source(
                    value.this,
                    flat_to_original=flat_to_original,
                ),
            )
        elif key == "joins" and isinstance(value, list):
            node.set(
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
        elif key == "expressions" and isinstance(value, list):
            rebuilt_exprs: list[object] = []
            for item in value:
                if (
                    isinstance(item, exp.Column)
                    and not item.table
                    and not item.db
                    and not item.catalog
                    and not isinstance(item.this, exp.Star)
                    and (
                        item.name.lower() in next_scope.unnest_aliases
                        or item.name.lower() in next_scope.unnest_offsets
                    )
                ):
                    orig_name = item.name
                    rewritten_col = _rewrite_struct_paths_in_place(
                        item,
                        scope=next_scope,
                        flat_to_original=flat_to_original,
                    )
                    rebuilt_exprs.append(exp.alias_(rewritten_col, orig_name))
                else:
                    rebuilt_exprs.append(
                        _rewrite_struct_child(
                            item,
                            scope=next_scope,
                            flat_to_original=flat_to_original,
                        )
                    )
            node.set(key, rebuilt_exprs)
        else:
            node.set(
                key,
                _rewrite_struct_child(
                    value,
                    scope=next_scope,
                    flat_to_original=flat_to_original,
                ),
            )
    return node


def _rewrite_struct_paths_in_place(
    node: exp.Expr,
    *,
    scope: _ScopeBindings = _EMPTY_SCOPE,
    flat_to_original: Mapping[str, tuple[str | None, str]] | None = None,
) -> exp.Expr:
    orig_map: Mapping[str, tuple[str | None, str]] = flat_to_original or {}
    if isinstance(node, (exp.Column, exp.Dot)):
        if (
            isinstance(node, exp.Column)
            and not node.table
            and not node.db
            and not node.catalog
            and not isinstance(node.this, exp.Star)
        ):
            lower = node.name.lower()
            if lower in scope.unnest_aliases:
                return exp.Column(
                    this=exp.to_identifier("value"),
                    table=exp.to_identifier(node.name),
                )
            if lower in scope.unnest_offsets:
                src_alias = scope.unnest_offsets[lower]
                return exp.Cast(
                    this=exp.Column(
                        this=exp.to_identifier("key"),
                        table=exp.to_identifier(src_alias),
                    ),
                    to=exp.DataType.build("INTEGER", dialect="sqlite"),
                )
        parts = _extract_dotted_parts(node)
        if parts is not None and len(parts) >= 2:
            return _rewrite_dotted_path(parts, scope=scope)
        if isinstance(node, exp.Column):
            return node
    if isinstance(node, exp.Select):
        return _rewrite_select_struct_paths(
            node,
            scope=scope,
            flat_to_original=orig_map,
        )
    for key, value in list(node.args.items()):
        child_scope = _EMPTY_SCOPE if key == "with_" else scope
        node.set(
            key,
            _rewrite_struct_child(
                value,
                scope=child_scope,
                flat_to_original=orig_map,
            ),
        )
    return node


def _rewrite_struct_paths(
    node: exp.Expr,
    *,
    scope: _ScopeBindings = _EMPTY_SCOPE,
    flat_to_original: Mapping[str, tuple[str | None, str]] | None = None,
) -> exp.Expr:
    return _rewrite_struct_paths_in_place(
        node.copy(),
        scope=scope,
        flat_to_original=flat_to_original,
    )
