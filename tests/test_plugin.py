from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import base64

from pytest_automation_report.plugin import AutomationReportPlugin


class DummyConfig:
    rootpath = "."


def make_report(nodeid, when, outcome, duration, longrepr=None, wasxfail=False):
    return SimpleNamespace(
        nodeid=nodeid,
        when=when,
        outcome=outcome,
        duration=duration,
        longrepr=longrepr,
        wasxfail=wasxfail if wasxfail else None,
        passed=outcome == "passed",
        failed=outcome == "failed",
        skipped=outcome == "skipped",
    )


class DummyFailedOutcome:
    def __init__(self, report):
        self._report = report

    def get_result(self):
        return self._report


class DummyDriver:
    def __init__(self, content_base64):
        self.content_base64 = content_base64

    def get_screenshot_as_base64(self):
        return self.content_base64


class DummyPlaywrightPage:
    def __init__(self, content_bytes):
        self.content_bytes = content_bytes
        self.calls = []

    def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        return self.content_bytes


class DummyPlaywrightContext:
    def __init__(self, pages):
        self.pages = pages


def test_build_report_html_includes_chart_sections(tmp_path):
    plugin = AutomationReportPlugin(
        config=DummyConfig(),
        report_path=tmp_path / "automation-report.html",
        title="CI Dashboard",
    )
    plugin.started_at = datetime(2026, 4, 12, 5, 30, tzinfo=timezone.utc)
    plugin.finished_at = datetime(2026, 4, 12, 5, 35, tzinfo=timezone.utc)
    plugin.collected = 4

    plugin.pytest_runtest_logreport(make_report("tests/test_api.py::test_ok", "setup", "passed", 0.01))
    plugin.pytest_runtest_logreport(make_report("tests/test_api.py::test_ok", "call", "passed", 0.10))
    plugin.pytest_runtest_logreport(make_report("tests/test_api.py::test_ok", "teardown", "passed", 0.01))

    plugin.pytest_runtest_logreport(make_report("tests/test_api.py::test_fail", "setup", "passed", 0.01))
    plugin.pytest_runtest_logreport(
        make_report("tests/test_api.py::test_fail", "call", "failed", 0.22, longrepr="AssertionError: boom")
    )
    plugin.pytest_runtest_logreport(make_report("tests/test_api.py::test_fail", "teardown", "passed", 0.02))

    plugin.pytest_runtest_logreport(
        make_report("tests/test_ui.py::test_skip", "setup", "skipped", 0.00, longrepr="Skipped: waiting")
    )

    plugin.pytest_runtest_logreport(make_report("tests/test_jobs.py::test_error", "setup", "passed", 0.01))
    plugin.pytest_runtest_logreport(make_report("tests/test_jobs.py::test_error", "call", "passed", 0.15))
    plugin.pytest_runtest_logreport(
        make_report("tests/test_jobs.py::test_error", "teardown", "failed", 0.03, longrepr="teardown exploded")
    )

    html = plugin.build_report_html()

    assert "CI Dashboard" in html
    assert 'data-tab-target="summary"' in html
    assert 'data-tab-target="details"' in html
    assert 'data-tab-panel="summary"' in html
    assert 'data-tab-panel="details"' in html
    assert '<div class="paginated-section" data-pagination-root data-page-size="5">' in html
    assert '<article class="table-card paginated-section" data-pagination-root data-page-size="20">' in html
    assert 'data-page-size="5"' in html
    assert 'data-page-size="20"' in html
    assert 'data-pagination-prev' in html
    assert 'data-pagination-next' in html
    assert 'data-pagination-summary' in html
    assert 'Page ${currentPage} of ${totalPages}' in html
    assert 'Showing ${rangeStart}-${rangeEnd} of ${totalItems} items' in html
    assert "Outcome Distribution" in html
    assert "Phase Duration Breakdown" in html
    assert "Top Slowest Tests" in html
    assert "Module Execution Breakdown" in html
    assert "AssertionError: boom" in html
    assert "teardown exploded" in html
    assert "test_fail" in html
    assert "test_error" in html
    assert html.count('<article class="failure-card" data-pagination-item>') == 2
    assert html.count("<tr data-pagination-item>") == 4


def test_write_report_creates_file(tmp_path):
    plugin = AutomationReportPlugin(
        config=DummyConfig(),
        report_path=tmp_path / "reports" / "automation-report.html",
        title="Smoke Suite",
    )
    plugin.started_at = datetime.now(timezone.utc)
    plugin.finished_at = datetime.now(timezone.utc)
    plugin.collected = 1

    plugin.pytest_runtest_logreport(make_report("tests/test_smoke.py::test_ready", "setup", "passed", 0.01))
    plugin.pytest_runtest_logreport(make_report("tests/test_smoke.py::test_ready", "call", "passed", 0.02))
    plugin.pytest_runtest_logreport(make_report("tests/test_smoke.py::test_ready", "teardown", "passed", 0.01))

    plugin._write_report(session=None)

    report_path = tmp_path / "reports" / "automation-report.html"
    assert report_path.exists()
    assert "Smoke Suite" in report_path.read_text(encoding="utf-8")


def test_manual_screenshot_attachment_is_rendered_in_report(tmp_path):
    plugin = AutomationReportPlugin(
        config=DummyConfig(),
        report_path=tmp_path / "automation-report.html",
        title="UI Suite",
    )
    plugin.started_at = datetime.now(timezone.utc)
    plugin.finished_at = datetime.now(timezone.utc)

    plugin.pytest_runtest_logreport(make_report("tests/test_ui.py::test_form", "setup", "passed", 0.01))
    plugin.pytest_runtest_logreport(
        make_report("tests/test_ui.py::test_form", "call", "failed", 0.20, longrepr="AssertionError: modal missing")
    )
    plugin.attach_screenshot(
        nodeid="tests/test_ui.py::test_form",
        image_bytes=b"fake-png",
        name="Failed state",
    )

    html = plugin.build_report_html()

    assert "Failed state" in html
    assert "data:image/png;base64," in html
    assert base64.b64encode(b"fake-png").decode("ascii") in html


def test_auto_capture_failure_screenshot_from_driver():
    plugin = AutomationReportPlugin(
        config=DummyConfig(),
        report_path=Path("automation-report.html"),
        title="Selenium Suite",
    )
    encoded = base64.b64encode(b"driver-png").decode("ascii")
    item = SimpleNamespace(
        nodeid="tests/test_ui.py::test_checkout",
        funcargs={"driver": DummyDriver(encoded)},
    )
    report = SimpleNamespace(when="call", failed=True)
    hook = plugin.pytest_runtest_makereport(item, call=None)

    next(hook)
    try:
        hook.send(DummyFailedOutcome(report))
    except StopIteration:
        pass

    attachments = plugin.results["tests/test_ui.py::test_checkout"].attachments
    assert len(attachments) == 1
    assert attachments[0]["name"] == "Failure Screenshot (driver)"
    assert attachments[0]["content_base64"] == encoded


def test_auto_capture_failure_screenshot_from_playwright_page():
    plugin = AutomationReportPlugin(
        config=DummyConfig(),
        report_path=Path("automation-report.html"),
        title="Playwright Suite",
    )
    page = DummyPlaywrightPage(b"playwright-png")
    item = SimpleNamespace(
        nodeid="tests/test_ui.py::test_checkout",
        funcargs={"page": page},
    )
    report = SimpleNamespace(when="call", failed=True)
    hook = plugin.pytest_runtest_makereport(item, call=None)

    next(hook)
    try:
        hook.send(DummyFailedOutcome(report))
    except StopIteration:
        pass

    attachments = plugin.results["tests/test_ui.py::test_checkout"].attachments
    assert len(attachments) == 1
    assert attachments[0]["name"] == "Failure Screenshot (page)"
    assert attachments[0]["content_base64"] == base64.b64encode(b"playwright-png").decode("ascii")
    assert page.calls == [{"type": "png"}]


def test_auto_capture_failure_screenshot_from_playwright_context():
    plugin = AutomationReportPlugin(
        config=DummyConfig(),
        report_path=Path("automation-report.html"),
        title="Playwright Suite",
    )
    first_page = DummyPlaywrightPage(b"first-page")
    last_page = DummyPlaywrightPage(b"last-page")
    item = SimpleNamespace(
        nodeid="tests/test_ui.py::test_checkout",
        funcargs={"context": DummyPlaywrightContext([first_page, last_page])},
    )
    report = SimpleNamespace(when="call", failed=True)
    hook = plugin.pytest_runtest_makereport(item, call=None)

    next(hook)
    try:
        hook.send(DummyFailedOutcome(report))
    except StopIteration:
        pass

    attachments = plugin.results["tests/test_ui.py::test_checkout"].attachments
    assert len(attachments) == 1
    assert attachments[0]["name"] == "Failure Screenshot (context)"
    assert attachments[0]["content_base64"] == base64.b64encode(b"last-page").decode("ascii")
    assert first_page.calls == []
    assert last_page.calls == [{"type": "png"}]
