"""Table-function inlining (`ADR-035`) and BigQuery-to-SQLite AST transpilation pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from bq_eval.runner.contract.spec import TableFunctionSpec
from bq_eval.runner.engine.rewriter import (
    _EMPTY_SCOPE,
    _extract_table_parts,
    _rewrite_qualified_tables,
    _rewrite_struct_paths,
    parse_qualified_parts,
    sqlite_table_name,
)
from bq_eval.runner.engine.transpiler import _rewrite_bq_node_for_sqlite

__all__ = [
    "TableFunctionDef",
    "TableFunctionRegistry",
    "canonical_key",
    "parse_qualified_parts",
    "sqlite_table_name",
    "transpile_bigquery_ast",
    "transpile_bigquery_sql",
    "transpile_bigquery_staged",
]


def canonical_key(dataset: str | None, name: str) -> tuple[str | None, str]:
    """Build case-normalized lookup key for dataset-qualified functions and tables."""
    return (dataset.lower() if dataset else None, name.lower())


def _extract_tvf_call_parts(table_node: exp.Table) -> tuple[str | None, str, list[exp.Expr]] | None:
    if not isinstance(table_node.this, exp.Anonymous):
        return None
    anon = table_node.this
    parts: list[str] = []
    if table_node.catalog:
        parts.append(table_node.catalog)
    if table_node.db:
        parts.append(table_node.db)
    raw_fn = anon.name
    if raw_fn:
        parts.extend(p for p in raw_fn.replace("`", "").split(".") if p)
    if not parts:
        return None
    dataset, fn_name = parse_qualified_parts(".".join(parts))
    return dataset, fn_name, list(anon.expressions)


@dataclass(frozen=True)
class TableFunctionDef:
    """Parsed BigQuery `CREATE TABLE FUNCTION` definition ready for AST inlining."""

    dataset: str | None
    name: str
    params: tuple[str, ...]
    body: exp.Expr

    def to_spec(self) -> TableFunctionSpec:
        key = f"{self.dataset}.{self.name}" if self.dataset else self.name
        return TableFunctionSpec(
            dataset=self.dataset,
            name=self.name,
            params=self.params,
            canonical_key=key,
        )


_ATOMIC_PARAM_TYPES = (
    exp.Literal,
    exp.Column,
    exp.Func,
    exp.Anonymous,
    exp.Cast,
    exp.TryCast,
    exp.Paren,
    exp.Null,
)


def _query_output_aliases(
    node: exp.Expr,
    *,
    param_names: frozenset[str] = frozenset(),
    body_shadowed: frozenset[str] = frozenset(),
) -> frozenset[str]:
    cur: exp.Expr | None = node
    while isinstance(cur, exp.Subquery):
        cur = cur.this
    if isinstance(cur, exp.Union) and cur.this is not None:
        return _query_output_aliases(
            cur.this,
            param_names=param_names,
            body_shadowed=body_shadowed,
        )
    if not isinstance(cur, exp.Select):
        return frozenset()
    names: set[str] = set()
    for expr in cur.expressions:
        if isinstance(expr, exp.Alias) and expr.alias:
            names.add(expr.alias.lower())
        elif isinstance(expr, exp.Column) and expr.name:
            lower = expr.name.lower()
            if expr.table or lower in body_shadowed or lower not in param_names:
                names.add(lower)
    return frozenset(names)


def _substitute_with_clause(
    with_clause: exp.With,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str],
    cte_columns: dict[str, frozenset[str]],
) -> exp.With:
    for cte in with_clause.expressions:
        if isinstance(cte, exp.CTE) and cte.this is not None:
            sub_body = _substitute_in_place(
                cte.this,
                param_map,
                shadowed=shadowed,
                cte_columns=cte_columns,
            )
            cte.set("this", sub_body)
            if cte.alias:
                cte_columns[cte.alias.lower()] = _query_output_aliases(sub_body)
    return with_clause


def _substitute_source_expr(
    src: exp.Expr,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str],
    cte_columns: Mapping[str, frozenset[str]],
) -> tuple[exp.Expr, frozenset[str]]:
    sub_src = _substitute_in_place(
        src,
        param_map,
        shadowed=shadowed,
        cte_columns=cte_columns,
    )
    if isinstance(sub_src, exp.Table) and not sub_src.db and not sub_src.catalog and sub_src.name:
        return sub_src, cte_columns.get(sub_src.name.lower(), frozenset())
    if isinstance(sub_src, exp.Subquery) and sub_src.this is not None:
        return sub_src, _query_output_aliases(sub_src.this)
    return sub_src, frozenset()


def _substitute_child(
    value: object,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str],
    cte_columns: Mapping[str, frozenset[str]],
) -> object:
    if isinstance(value, exp.Expr):
        return _substitute_in_place(
            value,
            param_map,
            shadowed=shadowed,
            cte_columns=cte_columns,
        )
    if isinstance(value, list):
        return [
            _substitute_in_place(
                item,
                param_map,
                shadowed=shadowed,
                cte_columns=cte_columns,
            )
            if isinstance(item, exp.Expr)
            else item
            for item in value
        ]
    return value


def _prepare_select_sources(
    cloned: exp.Select,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str],
    local_cte_cols: Mapping[str, frozenset[str]],
) -> tuple[frozenset[str], list[object] | None]:
    from_shadowed: set[str] = set()
    from_clause = cloned.args.get("from_")
    if isinstance(from_clause, exp.From) and from_clause.this is not None:
        sub_src, cols = _substitute_source_expr(
            from_clause.this,
            param_map,
            shadowed=shadowed,
            cte_columns=local_cte_cols,
        )
        from_clause.set("this", sub_src)
        from_shadowed.update(cols)

    raw_joins = cloned.args.get("joins")
    if not isinstance(raw_joins, list):
        return frozenset(from_shadowed), None
    prepared_joins: list[object] = []
    for item in raw_joins:
        if isinstance(item, exp.Join) and item.this is not None:
            sub_src, cols = _substitute_source_expr(
                item.this,
                param_map,
                shadowed=shadowed,
                cte_columns=local_cte_cols,
            )
            item.set("this", sub_src)
            from_shadowed.update(cols)
            prepared_joins.append(item)
        else:
            prepared_joins.append(item)
    return frozenset(from_shadowed), prepared_joins


def _substitute_select_expressions(
    expressions: list[exp.Expr],
    param_map: Mapping[str, exp.Expr],
    *,
    body_shadowed: frozenset[str],
    local_cte_cols: Mapping[str, frozenset[str]],
) -> list[exp.Expr]:
    rebuilt: list[exp.Expr] = []
    for expr in expressions:
        is_bare_param = (
            isinstance(expr, exp.Column)
            and not expr.table
            and not expr.db
            and not expr.catalog
            and expr.name.lower() in param_map
            and expr.name.lower() not in body_shadowed
        )
        orig_name = expr.name if is_bare_param else ""
        sub = _substitute_in_place(
            expr,
            param_map,
            shadowed=body_shadowed,
            cte_columns=local_cte_cols,
        )
        rebuilt.append(exp.alias_(sub, orig_name) if is_bare_param else sub)
    return rebuilt


def _substitute_select_params(
    cloned: exp.Select,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str],
    local_cte_cols: Mapping[str, frozenset[str]],
) -> exp.Select:
    from_shadowed, prepared_joins = _prepare_select_sources(
        cloned,
        param_map,
        shadowed=shadowed,
        local_cte_cols=local_cte_cols,
    )
    body_shadowed = shadowed | from_shadowed
    if prepared_joins is not None:
        for item in prepared_joins:
            if isinstance(item, exp.Join):
                for jkey, jval in list(item.args.items()):
                    if jkey != "this":
                        item.set(
                            jkey,
                            _substitute_child(
                                jval,
                                param_map,
                                shadowed=body_shadowed,
                                cte_columns=local_cte_cols,
                            ),
                        )
        cloned.set("joins", prepared_joins)

    post_shadowed = body_shadowed | _query_output_aliases(
        cloned,
        param_names=frozenset(param_map.keys()),
        body_shadowed=body_shadowed,
    )
    cloned.set(
        "expressions",
        _substitute_select_expressions(
            list(cloned.expressions),
            param_map,
            body_shadowed=body_shadowed,
            local_cte_cols=local_cte_cols,
        ),
    )
    post_keys = {"group", "having", "order", "qualify"}
    for key, value in list(cloned.args.items()):
        if key in {"with_", "from_", "joins", "expressions"}:
            continue
        active_shadow = post_shadowed if key in post_keys else body_shadowed
        cloned.set(
            key,
            _substitute_child(
                value,
                param_map,
                shadowed=active_shadow,
                cte_columns=local_cte_cols,
            ),
        )
    return cloned


def _substitute_in_place(
    node: exp.Expr,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str] = frozenset(),
    cte_columns: Mapping[str, frozenset[str]] | None = None,
) -> exp.Expr:
    if isinstance(node, exp.Column):
        if (
            not node.table
            and not node.db
            and not node.catalog
            and node.name.lower() in param_map
            and node.name.lower() not in shadowed
        ):
            replacement = param_map[node.name.lower()].copy()
            return (
                replacement
                if isinstance(replacement, _ATOMIC_PARAM_TYPES)
                else exp.Paren(this=replacement)
            )
        return node

    local_cte_cols: dict[str, frozenset[str]] = dict(cte_columns or {})
    with_clause = node.args.get("with_")
    if isinstance(with_clause, exp.With):
        _substitute_with_clause(
            with_clause,
            param_map,
            shadowed=shadowed,
            cte_columns=local_cte_cols,
        )
    if isinstance(node, exp.Select):
        return _substitute_select_params(
            node,
            param_map,
            shadowed=shadowed,
            local_cte_cols=local_cte_cols,
        )
    for key, value in list(node.args.items()):
        if key == "with_":
            continue
        node.set(
            key,
            _substitute_child(
                value,
                param_map,
                shadowed=shadowed,
                cte_columns=local_cte_cols,
            ),
        )
    return node


def _substitute_params(
    node: exp.Expr,
    param_map: Mapping[str, exp.Expr],
    *,
    shadowed: frozenset[str] = frozenset(),
    cte_columns: Mapping[str, frozenset[str]] | None = None,
) -> exp.Expr:
    return _substitute_in_place(
        node.copy(),
        param_map,
        shadowed=shadowed,
        cte_columns=cte_columns,
    )


class TableFunctionRegistry:
    """In-memory registry that inlines BigQuery `CREATE TABLE FUNCTION` calls into subqueries."""

    def __init__(self, *, default_dataset: str | None = None) -> None:
        self._defs: dict[tuple[str | None, str], TableFunctionDef] = {}
        self._views: dict[tuple[str | None, str], TableFunctionDef] = {}
        self._default_dataset = default_dataset

    def register_view_ast(self, stmt: exp.Create) -> str:
        """Register a parsed `CREATE [OR REPLACE] VIEW` statement for staged AST inlining."""
        if not isinstance(stmt.this, exp.Table) or stmt.expression is None:
            raise ValueError("Expected CREATE VIEW with table name and SELECT body.")
        parts = _extract_table_parts(stmt.this)
        if parts is None:
            raise ValueError("Missing view name.")
        raw_dataset, name = parts
        dataset = raw_dataset or self._default_dataset
        body = stmt.expression
        if isinstance(body, exp.Subquery) and body.this is not None:
            body = body.this
        defn = TableFunctionDef(dataset=dataset, name=name, params=(), body=body.copy())
        self._views[canonical_key(dataset, name)] = defn
        return f"{dataset}__{name}" if dataset else name

    def resolve_view(self, dataset: str | None, name: str) -> TableFunctionDef | None:
        if dataset is None and self._default_dataset is not None:
            default_hit = self._views.get(canonical_key(self._default_dataset, name))
            if default_hit is not None:
                return default_hit
        return self._views.get(canonical_key(dataset, name))

    def register_ast(self, stmt: exp.Create) -> TableFunctionDef:
        """Register a parsed `CREATE [OR REPLACE] TABLE FUNCTION` statement."""
        if not isinstance(stmt.this, exp.UserDefinedFunction):
            raise ValueError("Expected CREATE TABLE FUNCTION with UserDefinedFunction header.")
        udf = stmt.this
        if not isinstance(udf.this, exp.Table):
            raise ValueError("Expected table-qualified name on CREATE TABLE FUNCTION.")
        parts = _extract_table_parts(udf.this)
        if parts is None:
            raise ValueError("Missing table function name.")
        raw_dataset, name = parts
        dataset = raw_dataset or self._default_dataset
        params: list[str] = [expr.name for expr in udf.expressions]
        body = stmt.expression
        if body is None:
            raise ValueError(f"Table function {name!r} has an empty body.")
        if isinstance(body, exp.Subquery) and body.this is not None:
            body = body.this
        defn = TableFunctionDef(
            dataset=dataset,
            name=name,
            params=tuple(params),
            body=body.copy(),
        )
        self._defs[canonical_key(dataset, name)] = defn
        return defn

    def resolve(self, dataset: str | None, name: str) -> TableFunctionDef | None:
        if dataset is None and self._default_dataset is not None:
            default_hit = self._defs.get(canonical_key(self._default_dataset, name))
            if default_hit is not None:
                return default_hit
        exact = self._defs.get(canonical_key(dataset, name))
        if exact is not None:
            return exact
        if dataset is None:
            matches = [d for (_, fn_key), d in self._defs.items() if fn_key == name.lower()]
            if len(matches) == 1:
                return matches[0]
        return None

    def specs(self) -> tuple[TableFunctionSpec, ...]:
        return tuple(defn.to_spec() for defn in self._defs.values())

    def extract_hoisted_tvfs(
        self,
        node: exp.Expr,
    ) -> tuple[list[tuple[str, exp.Expr]], exp.Expr]:
        """Extract nested TVF calls in topological order so SQLite executes each stage once."""
        hoisted: dict[tuple[tuple[str | None, str], str], tuple[str, exp.Expr]] = {}
        root_ast = self.inline_ast(node, _hoisted=hoisted)
        return list(hoisted.values()), root_ast

    def _resolve_inlinable(
        self,
        cur: exp.Expr,
    ) -> tuple[TableFunctionDef, list[exp.Expr]] | None:
        if not isinstance(cur, exp.Table):
            return None
        call_parts = _extract_tvf_call_parts(cur)
        if call_parts is not None:
            dataset, fn_name, call_args = call_parts
            defn = self.resolve(dataset, fn_name)
            return (defn, call_args) if defn is not None else None
        table_parts = _extract_table_parts(cur)
        if table_parts is None:
            return None
        v_defn = self.resolve_view(*table_parts)
        return (v_defn, []) if v_defn is not None else None

    def inline_ast(
        self,
        node: exp.Expr,
        *,
        _active_stack: frozenset[tuple[str | None, str]] = frozenset(),
        _hoisted: dict[tuple[tuple[str | None, str], str], tuple[str, exp.Expr]] | None = None,
    ) -> exp.Expr:
        """Recursively inline every registered table-function invocation, hoisting shared CTEs."""
        is_root_select = _hoisted is None and isinstance(node, exp.Select)
        hoisted = {} if is_root_select else _hoisted

        def _inline_node(cur: exp.Expr) -> exp.Expr:
            target = self._resolve_inlinable(cur)
            if target is None:
                return cur
            defn, call_args = target
            key = canonical_key(defn.dataset, defn.name)
            if key in _active_stack:
                raise ValueError(f"Recursive table-function cycle detected involving {defn.name!r}")
            if len(call_args) != len(defn.params):
                raise ValueError(
                    f"Table function {defn.name!r} expects {len(defn.params)} args, got {len(call_args)}"
                )
            resolved_args = [
                self.inline_ast(arg, _active_stack=_active_stack, _hoisted=hoisted)
                for arg in call_args
            ]
            alias = (
                cur.args["alias"].copy()
                if cur.args.get("alias") is not None
                else exp.TableAlias(this=exp.to_identifier(defn.name))
            )
            param_map = {
                pname.lower(): arg_expr
                for pname, arg_expr in zip(defn.params, resolved_args, strict=True)
            }
            if hoisted is not None:
                args_sig = ",".join(arg.sql("bigquery") for arg in resolved_args)
                hoist_key = (key, args_sig)
                if hoist_key not in hoisted:
                    substituted = _substitute_params(defn.body, param_map)
                    nested = self.inline_ast(
                        substituted,
                        _active_stack=_active_stack | {key},
                        _hoisted=hoisted,
                    )
                    hoisted[hoist_key] = (f"_bq_tvf_{len(hoisted)}", nested)
                return exp.Table(this=exp.to_identifier(hoisted[hoist_key][0]), alias=alias)
            nested = self.inline_ast(
                _substitute_params(defn.body, param_map),
                _active_stack=_active_stack | {key},
            )
            return exp.Subquery(this=nested, alias=alias)

        rewritten = node.copy().transform(_inline_node, copy=False)
        if is_root_select and hoisted:
            cte_nodes = [
                exp.CTE(
                    this=body,
                    alias=exp.TableAlias(this=exp.to_identifier(cte_name)),
                    materialized=True,
                )
                for cte_name, body in hoisted.values()
            ]
            existing_with = rewritten.args.get("with_")
            if isinstance(existing_with, exp.With):
                existing_with.set("expressions", [*cte_nodes, *existing_with.expressions])
            else:
                rewritten.set("with_", exp.With(expressions=cte_nodes))
        return rewritten


def _finalize_sqlite_ast(
    ast_node: exp.Expr,
    *,
    qualify_datasets: bool,
    default_dataset: str | None,
    preserved_tables: frozenset[str] = frozenset(),
) -> str:
    rewritten, flat_to_original = _rewrite_qualified_tables(
        ast_node,
        qualify_datasets=qualify_datasets,
        default_dataset=default_dataset,
        preserved_tables=preserved_tables,
    )
    rewritten = _rewrite_struct_paths(
        rewritten,
        scope=_EMPTY_SCOPE,
        flat_to_original=flat_to_original,
    )
    for _ in range(12):
        changed = False

        def _rewrite_step(node: exp.Expr) -> exp.Expr:
            nonlocal changed
            out = _rewrite_bq_node_for_sqlite(node)
            if out is not node:
                changed = True
            return out

        rewritten = rewritten.transform(_rewrite_step, copy=False)
        if not changed:
            break
    return rewritten.sql(dialect="sqlite")


def transpile_bigquery_staged(
    ast_node: exp.Expr,
    *,
    registry: TableFunctionRegistry | None = None,
    qualify_datasets: bool = True,
    default_dataset: str | None = None,
) -> tuple[list[tuple[str, str]], str]:
    """Transpile a query into sequential temp-table stages for nested TVFs plus the main SELECT."""
    if registry is None or not isinstance(ast_node, exp.Select):
        return [], transpile_bigquery_ast(
            ast_node,
            registry=registry,
            qualify_datasets=qualify_datasets,
            default_dataset=default_dataset,
        )
    raw_stages, root_ast = registry.extract_hoisted_tvfs(ast_node)
    stage_sqls: list[tuple[str, str]] = []
    preserved: set[str] = set()
    for stage_name, stage_ast in raw_stages:
        sql_text = _finalize_sqlite_ast(
            stage_ast,
            qualify_datasets=qualify_datasets,
            default_dataset=default_dataset,
            preserved_tables=frozenset(preserved),
        )
        stage_sqls.append((stage_name, sql_text))
        preserved.add(stage_name.lower())
    main_sql = _finalize_sqlite_ast(
        root_ast,
        qualify_datasets=qualify_datasets,
        default_dataset=default_dataset,
        preserved_tables=frozenset(preserved),
    )
    return stage_sqls, main_sql


def transpile_bigquery_ast(
    ast_node: exp.Expr,
    *,
    registry: TableFunctionRegistry | None = None,
    qualify_datasets: bool = True,
    default_dataset: str | None = None,
) -> str:
    """Inline table functions, flatten dataset names, rewrite structs, and emit SQLite SQL."""
    rewritten = registry.inline_ast(ast_node) if registry is not None else ast_node.copy()
    return _finalize_sqlite_ast(
        rewritten,
        qualify_datasets=qualify_datasets,
        default_dataset=default_dataset,
    )


def transpile_bigquery_sql(
    sql: str,
    *,
    registry: TableFunctionRegistry | None = None,
    qualify_datasets: bool = True,
    default_dataset: str | None = None,
) -> str:
    """Parse one BigQuery SQL statement and transpile it into executable SQLite SQL."""
    ast_node = sqlglot.parse_one(sql, read="bigquery")
    return transpile_bigquery_ast(
        ast_node,
        registry=registry,
        qualify_datasets=qualify_datasets,
        default_dataset=default_dataset,
    )
