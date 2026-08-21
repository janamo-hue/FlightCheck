"""report.sparkline degenerate cases, reached only indirectly elsewhere."""

from src import report


def test_sparkline_needs_two_points():
    assert "Not enough history" in report.sparkline([("t", 300.0)])


def test_sparkline_flat_series_does_not_divide_by_zero():
    # A fare that held steady is a common real case; the (hi-lo) or 1.0 guard
    # must keep it from crashing the published page.
    # x is now days to departure; a flat fare must still render, and a flat
    # distance span (two same-day observations) must not divide by zero either.
    svg = report.sparkline([(90.0, 300.0), (83.0, 300.0), (76.0, 300.0)])
    assert "<svg" in svg
    assert "<svg" in report.sparkline([(90.0, 300.0), (90.0, 310.0)])


def test_sparkline_marks_the_low():
    svg = report.sparkline([(90.0, 400.0), (83.0, 300.0), (76.0, 350.0)])
    assert "<circle" in svg
    assert "low 300" in svg


def test_sparkline_x_axis_is_days_to_departure():
    # Most distant on the left, nearest on the right, regardless of the order
    # the points arrive in. Two points 90d and 10d out: the 90d point must sit
    # at the left pad and the 10d point at the right pad.
    svg = report.sparkline([(10.0, 350.0), (90.0, 400.0)])
    x_left = report.PAD
    x_right = report.W - report.PAD
    first, second = (
        pair.split(",") for pair in
        svg.split('polyline points="')[1].split('"')[0].split())
    assert abs(float(first[0]) - x_left) < 0.6      # 90d out drawn first, left
    assert abs(float(second[0]) - x_right) < 0.6    # 10d out at the right edge
    assert "90d out" in svg and "10d out" in svg
