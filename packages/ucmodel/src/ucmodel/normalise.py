"""Raw Unity Catalog state to the model.

The model is a dict keyed by logical name, such as `table:sales.orders`, with
the bound catalog removed everywhere, so a shadow model and a target model
can be compared directly. Every rule here comes from a probe; `AGENTS.md`
records which.

Each object keeps identity and audit fields under `meta`. Comparisons
ignore `meta`, and drift attribution reads it.
"""

from __future__ import annotations

import json
import re

from .sqltext import strip_catalog

# Properties that change by themselves or say nothing the source could have
# declared. They never enter the model.
VOLATILE_PREFIXES = (
    "spark.sql.statistics.",
    "view.",
    "io.unitycatalog.",
    "delta.constraints.",  # modelled as CHECK constraints
    "delta.rowTracking.materialized",
    "databricks.delta.expressionStats.",
)
VOLATILE_KEYS = {
    "delta.lastCommitTimestamp",
    "delta.lastUpdateVersion",
    "delta.columnMapping.maxColumnId",
    "clusterByAuto",  # modelled as clustering
    "clusteringColumns",
    "transient_lastDdlTime",
}

# Column metadata the platform writes. The model keeps defaults, generation
# expressions and identity settings, and drops everything else.
COLUMN_METADATA_KEPT = {
    "CURRENT_DEFAULT",
    "delta.generationExpression",
    "delta.identity.start",
    "delta.identity.step",
    "delta.identity.allowExplicitInsert",
}

# A shallow clone is a Delta table of its own, sharing its source's files. Rehearsal reads
# them (probes/phase4.py, p6).
TABLE_TYPES = {"MANAGED", "MANAGED_SHALLOW_CLONE"}
SUPPORTED_TABLE_TYPES = TABLE_TYPES | {"VIEW"}


def normalise(raw: dict) -> dict[str, dict]:
    catalog = raw["catalog"]
    model: dict[str, dict] = {}
    for schema, s in raw["schemas"].items():
        if s is None:
            continue
        model[f"schema:{schema}"] = _schema(schema, s)
        for name, t in s["tables"].items():
            obj = _table_or_view(catalog, schema, name, t)
            model[f"{obj['kind']}:{schema}.{name}"] = obj
        for name, f in s["functions"].items():
            model[f"function:{schema}.{name}"] = _function(catalog, schema, name, f)
        for name, v in s["volumes"].items():
            obj = _volume(schema, name, v)
            model[f"{obj['kind']}:{schema}.{name}"] = obj
    return dict(sorted(model.items()))


def _strip(name: str | None, catalog: str) -> str | None:
    """`catalog.schema.x` becomes `schema.x` when the catalog is the bound one."""
    if name is None:
        return None
    parts = name.split(".")
    if len(parts) == 3 and parts[0] == catalog:
        return ".".join(parts[1:])
    return name


def _text(value: str | None) -> str | None:
    return value if value else None


def _type(type_text: str) -> str:
    return re.sub(r"\s+collate\s+utf8_binary\b", "", type_text, flags=re.IGNORECASE)


def properties(props: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in sorted(props.items())
        if k not in VOLATILE_KEYS and not k.startswith(VOLATILE_PREFIXES)
    }


def _meta(info: dict, *keys: str) -> dict:
    return {k: info[k] for k in keys if k in info}


def _schema(name: str, s: dict) -> dict:
    info = s["info"]
    return {
        "kind": "schema",
        "name": name,
        "comment": _text(info.get("comment")),
        "properties": properties(info.get("properties", {})),
        "tags": dict(sorted(s["tags"].items())),
        "grants": dict(sorted(s["grants"].items())),
        "meta": _meta(info, "schema_id", "owner", "updated_at", "updated_by"),
    }


def _nested(value: dict | list | None, key: str) -> list:
    """REST wraps some lists one level deep: {"row_filters": {"row_filters": [...]}}."""
    if isinstance(value, dict):
        return value.get(key) or []
    return value or []


def _column(catalog: str, c: dict, tags: dict[str, str]) -> dict:
    metadata = json.loads(c.get("type_json", "{}")).get("metadata", {})
    identity = None
    if "delta.identity.start" in metadata:
        identity = {
            "start": metadata["delta.identity.start"],
            "step": metadata["delta.identity.step"],
            "always": not metadata.get("delta.identity.allowExplicitInsert", False),
        }
    masks = _nested(c.get("column_masks"), "column_masks")
    mask = None
    if masks:
        mask = {
            "function": _strip(masks[0]["function_name"], catalog),
            "using": masks[0].get("using_column_names", []),
        }
    return {
        "name": c["name"],
        "type": _type(c["type_text"]),
        "nullable": c.get("nullable", True),
        "comment": _text(c.get("comment")),
        "default": metadata.get("CURRENT_DEFAULT"),
        "generated": metadata.get("delta.generationExpression"),
        "identity": identity,
        "mask": mask,
        "tags": dict(sorted(tags.items())),
    }


def _constraints(catalog: str, info: dict, props: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in info.get("table_constraints", []) or []:
        if pk := c.get("primary_key_constraint"):
            out[pk["name"]] = {
                "type": "primary_key",
                "columns": pk["child_columns"],
                "rely": bool(pk.get("rely", False)),
            }
        elif fk := c.get("foreign_key_constraint"):
            out[fk["name"]] = {
                "type": "foreign_key",
                "columns": fk["child_columns"],
                "references": _strip(fk["parent_table"], catalog),
                "parent_columns": fk["parent_columns"],
            }
    for key, expression in props.items():
        if key.startswith("delta.constraints."):
            name = key.removeprefix("delta.constraints.")
            out[name] = {"type": "check", "expression": expression}
    return dict(sorted(out.items()))


def _clustering(props: dict) -> dict | None:
    if props.get("clusterByAuto") == "true":
        return {"auto": True}
    columns = json.loads(props.get("clusteringColumns", "[]"))
    if columns:
        return {"columns": [".".join(path) for path in columns]}
    return None


def _table_or_view(catalog: str, schema: str, name: str, t: dict) -> dict:
    info = t["info"]
    props = info.get("properties", {})
    table_type = info.get("table_type")
    base = {"name": f"{schema}.{name}", "comment": _text(info.get("comment"))}
    meta = _meta(info, "table_id", "owner", "created_at", "updated_at", "updated_by")
    if table_type not in SUPPORTED_TABLE_TYPES or (
        table_type in TABLE_TYPES and info.get("data_source_format") != "DELTA"
    ):
        return {
            "kind": "unsupported",
            **base,
            "type": f"{table_type} {info.get('data_source_format', '')}".strip(),
            "meta": meta,
        }
    columns = [
        _column(catalog, c, t["column_tags"].get(c["name"], {}))
        for c in sorted(info.get("columns", []), key=lambda c: c["position"])
    ]
    common = {
        "properties": properties(props),
        "tags": dict(sorted(t["tags"].items())),
        "grants": dict(sorted(t["grants"].items())),
        "meta": meta,
    }
    if table_type == "VIEW":
        return {
            "kind": "view",
            **base,
            "definition": strip_catalog(info.get("view_definition", ""), catalog).strip(),
            "schema_mode": props.get("view.schemaMode"),
            "columns": [
                {k: c[k] for k in ("name", "type", "comment", "tags")} for c in columns
            ],
            **common,
        }
    filters = _nested(info.get("row_filters") or info.get("row_filter"), "row_filters")
    return {
        "kind": "table",
        **base,
        "columns": columns,
        "constraints": _constraints(catalog, info, props),
        "partitioning": [
            c["name"]
            for c in sorted(
                (c for c in info.get("columns", []) if c.get("partition_index") is not None),
                key=lambda c: c["partition_index"],
            )
        ],
        "clustering": _clustering(props),
        "row_filter": {
            "function": _strip(filters[0]["function_name"], catalog),
            "columns": filters[0].get("input_column_names", []),
        } if filters else None,
        **common,
    }


def _function(catalog: str, schema: str, name: str, f: dict) -> dict:
    info = f["info"]
    language = "SQL" if info.get("routine_body") == "SQL" else (
        info.get("external_language") or "EXTERNAL").upper()
    body = info.get("routine_definition") or ""
    if language == "SQL":
        body = strip_catalog(body, catalog)
    params = (info.get("input_params") or {}).get("parameters", [])
    returns = _type(info.get("full_data_type") or info.get("data_type", ""))
    if info.get("data_type") == "TABLE_TYPE":
        cols = (info.get("return_params") or {}).get("parameters", [])
        returns = "TABLE(" + ", ".join(f"{p['name']} {_type(p['type_text'])}" for p in cols) + ")"
    return {
        "kind": "function",
        "name": f"{schema}.{name}",
        "comment": _text(info.get("comment")),
        "parameters": [
            {
                "name": p["name"],
                "type": _type(p["type_text"]),
                "default": p.get("parameter_default"),
                "comment": _text(p.get("comment")),
            }
            for p in sorted(params, key=lambda p: p.get("position", 0))
        ],
        "returns": returns,
        "language": language,
        "body": body.strip(),
        "deterministic": bool(info.get("is_deterministic", True)),
        "data_access": info.get("sql_data_access"),
        "grants": dict(sorted(f["grants"].items())),
        "meta": _meta(info, "function_id", "owner", "created_at", "updated_at", "updated_by"),
    }


def _volume(schema: str, name: str, v: dict) -> dict:
    info = v["info"]
    base = {
        "name": f"{schema}.{name}",
        "comment": _text(info.get("comment")),
        "meta": _meta(info, "volume_id", "owner", "created_at", "updated_at", "updated_by"),
    }
    if info.get("volume_type") != "MANAGED":
        return {"kind": "unsupported", **base, "type": f"{info.get('volume_type')} VOLUME"}
    return {
        "kind": "volume",
        **base,
        "tags": dict(sorted(v["tags"].items())),
        "grants": dict(sorted(v["grants"].items())),
    }
