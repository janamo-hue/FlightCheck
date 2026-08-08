"""report.sparkline degenerate cases, reached only indirectly elsewhere."""

from src import report


def test_sparkline_needs_two_points():
    assert "Not enough history" in report.sparkline([("t", 300.0)])


def test_sparkline_flat_series_does_not_divide_by_zero():
    # A fare that held steady is a common real case; the (hi-lo) or 1.0 guard
    # must keep it from crashing the published page.
    svg = report.sparkline([("a", 300.0), ("b", 300.0), ("c", 300.0)])
    assert "<svg" in svg


def test_sparkline_marks_the_low():
    svg = report.sparkline([("a", 400.0), ("b", 300.0), ("c", 350.0)])
    assert "<circle" in svg
    assert "low 300" in svg
