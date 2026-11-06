"""Small, deterministic analytics over authorized API responses; no code or SQL."""
from collections import defaultdict
from datetime import UTC, datetime, timedelta
import math
import re

from .dashboard_rankings import dashboard_post_url


def field(row, path):
    if not isinstance(path, str) or len(path) > 120:
        raise ValueError("Use a documented field path.")
    value = row
    for key in path.split(".") if path else []:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def collection(data, path):
    rows = field(data, path)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("items_path must identify an array of objects. Inspect the response shape first.")
    return rows


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def matches(row, condition, now):
    value, expected, op = field(row, condition["field"]), condition.get("value"), condition["op"]
    if op == "eq": return value == expected
    if op == "ne": return value != expected
    if op == "contains": return isinstance(value, str) and isinstance(expected, str) and expected.casefold() in value.casefold()
    if op == "in": return isinstance(expected, list) and value in expected
    if op == "is_null": return (value is None) == bool(expected)
    if op == "within_last_hours":
        if not number(expected) or not 0 < expected <= 24 * 366: raise ValueError("Invalid rolling window.")
        date = timestamp(value)
        return date is not None and now - timedelta(hours=expected) <= date <= now
    if op in {"after", "before"}:
        a, b = timestamp(value), timestamp(expected)
        if b is None: raise ValueError("Provide an ISO date or timestamp.")
        return a is not None and (a >= b if op == "after" else a <= b)
    if op in {"gt", "gte", "lt", "lte"}:
        if value is None: return False
        if not number(value) or not number(expected): raise ValueError("Numeric comparisons require numeric fields.")
        return {"gt": value > expected, "gte": value >= expected, "lt": value < expected, "lte": value <= expected}[op]
    raise ValueError("Unknown filter operation.")


def add_links(value):
    if isinstance(value, list): return [add_links(item) for item in value]
    if not isinstance(value, dict): return value
    result = {key: add_links(item) for key, item in value.items()}
    account = value.get("account") or value.get("a") or value.get("post_account")
    code = value.get("shortcode") or value.get("sc") or value.get("post_shortcode")
    if isinstance(account, str) and isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,64}", account) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
        result["dashboard_url"] = dashboard_post_url(account, code)
    return result


def describe_shape(value, path="", depth=0):
    if isinstance(value, list):
        return {"items_path": path, "count": len(value), "example": describe_shape(value[0], path + "[]", depth + 1) if value else None}
    if isinstance(value, dict) and depth < 4:
        return {key: describe_shape(item, path + ("." if path else "") + key, depth + 1) for key, item in list(value.items())[:60]}
    if isinstance(value, str): return value[:160]
    if value is None or number(value) or isinstance(value, bool): return value
    return type(value).__name__


def analyze(data, spec, *, complete=True, now=None):
    if not isinstance(spec, dict) or set(spec) - {"items_path", "filters", "fields", "group_by", "aggregations", "sort", "limit"}:
        raise ValueError("Use the documented analysis properties.")
    now = now or datetime.now(UTC)
    rows = collection(data, spec.get("items_path", ""))
    filters = spec.get("filters", [])
    if not isinstance(filters, list) or len(filters) > 12: raise ValueError("Use at most twelve filters.")
    selected = [row for row in rows if all(matches(row, condition, now) for condition in filters)]
    group_by, aggregates = spec.get("group_by", []), spec.get("aggregations", [])
    if not isinstance(group_by, list) or len(group_by) > 3 or not isinstance(aggregates, list) or len(aggregates) > 8:
        raise ValueError("Use up to three group fields and eight aggregations.")
    if group_by or aggregates:
        groups = defaultdict(list)
        if not selected and not group_by: groups[()] = []
        for row in selected:
            key = tuple(field(row, name) for name in group_by)
            if any(isinstance(value, (dict, list)) for value in key): raise ValueError("Group fields must be scalar.")
            groups[key].append(row)
        results = []
        for key, members in groups.items():
            result = dict(zip(group_by, key))
            result["row_count"] = len(members)
            for aggregate in aggregates:
                alias, op = aggregate["as"], aggregate["op"]
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", alias): raise ValueError("Invalid result field name.")
                values = [field(row, aggregate.get("field", "")) for row in members]
                known = [value for value in values if number(value)]
                if op == "count": result[alias] = len(members)
                elif op == "count_distinct": result[alias] = len({str(value) for value in values if value is not None})
                elif op in {"sum", "avg", "min", "max"}:
                    result[alias] = None if not known else {"sum": sum, "min": min, "max": max, "avg": lambda values: sum(values) / len(values)}[op](known)
                    result[alias + "_known"] = len(known)
                else: raise ValueError("Unknown aggregation.")
            results.append(result)
    else:
        results = list(selected)
    sort = spec.get("sort", [])
    if not isinstance(sort, list) or len(sort) > 3: raise ValueError("Use up to three sort fields.")
    for order in reversed(sort):
        name, direction = order["field"], order.get("direction", "desc")
        if direction not in {"asc", "desc"}: raise ValueError("Invalid sort direction.")
        known = [row for row in results if field(row, name) is not None]
        missing = [row for row in results if field(row, name) is None]
        def key(row):
            value = field(row, name)
            return (0, value) if number(value) else (1, timestamp(value).timestamp()) if timestamp(value) else (2, str(value).casefold())
        results = sorted(known, key=key, reverse=direction == "desc") + missing
    limit = min(100, max(1, int(spec.get("limit", 20))))
    fields = spec.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 20: raise ValueError("Select up to twenty fields.")
    output = results[:limit]
    if fields:
        output = [{name: field(add_links(row), name) for name in fields} for row in output]
    return {"complete": complete, "input_rows": len(rows), "matched_rows": len(selected), "result_rows": len(results), "returned_rows": len(output), "data": add_links(output)}
