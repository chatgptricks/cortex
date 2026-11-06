from datetime import UTC, datetime

import pytest

from app.dashboard_analysis import analyze, describe_shape


def test_complete_filter_sort_and_internal_links():
    rows = [{"a": "evolving.ai", "sc": f"Post{i}", "l": i, "caption": "Elon Musk", "d": f"2026-11-{1+i%5:02}T12:00:00Z"} for i in range(250)]
    rows += [{"a": "elsewhere", "sc": "Wrong", "l": 9999, "caption": "unrelated", "d": "2026-11-05T12:00:00Z"}]
    result = analyze({"posts": rows}, {"items_path": "posts", "filters": [{"field": "caption", "op": "contains", "value": "ELON MUSK"}, {"field": "l", "op": "gt", "value": 220}], "sort": [{"field": "l", "direction": "desc"}], "fields": ["l", "dashboard_url"], "limit": 1})
    assert result["complete"] and result["input_rows"] == 251 and result["matched_rows"] == 29
    assert result["data"] == [{"l": 249, "dashboard_url": "https://sentientdash.app/index.html?post=evolving.ai%3APost249"}]


def test_unknown_metrics_grouping_and_rolling_window():
    rows = [{"account": "a", "likes": None, "at": "2026-11-05T11:00:00-06:00"}, {"account": "a", "likes": 0, "at": "2026-11-05T16:00:00Z"}, {"account": "b", "likes": None, "at": "2026-11-05T12:00:00Z"}, {"account": "a", "likes": 999, "at": "2026-11-06T20:00:00Z"}]
    result = analyze(rows, {"filters": [{"field": "at", "op": "within_last_hours", "value": 24}], "group_by": ["account"], "aggregations": [{"field": "likes", "op": "avg", "as": "average"}]}, now=datetime(2026, 11, 6, 0, tzinfo=UTC), complete=False)
    assert not result["complete"] and result["matched_rows"] == 3
    assert result["data"] == [{"account": "a", "row_count": 2, "average": 0, "average_known": 1}, {"account": "b", "row_count": 1, "average": None, "average_known": 0}]
    with pytest.raises(ValueError): analyze(rows, {"code": "print('no execution')"})
    assert describe_shape({"posts": rows})["posts"]["items_path"] == "posts"
