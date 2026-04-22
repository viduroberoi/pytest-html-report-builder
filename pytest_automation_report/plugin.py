from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from statistics import median
from typing import Any, Callable
import base64
import math
import platform

import pytest


OUTCOME_ORDER = ["passed", "failed", "skipped", "error", "xfailed", "xpassed"]
OUTCOME_COLORS = {
    "passed": "#1d8348",
    "failed": "#c0392b",
    "skipped": "#b9770e",
    "error": "#7d3c98",
    "xfailed": "#2471a3",
    "xpassed": "#ba4a00",
}
PHASE_COLORS = {
    "setup": "#5dade2",
    "call": "#48c9b0",
    "teardown": "#f5b041",
}
FAILURE_DETAILS_PAGE_SIZE = 5
RESULTS_PAGE_SIZE = 20
SLOWEST_TEST_LIMIT = 10
SUITE_OUTCOME_LIMIT = 8
INSIGHT_LIST_LIMIT = 5
DEFAULT_REPORT_SUBTITLE = "Self-contained pytest execution report with runtime metrics, outcome analytics, and detailed failure visibility."


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("automation-report")
    group.addoption(
        "--automation-report",
        action="store",
        default=None,
        metavar="PATH",
        help="Generate a self-contained HTML automation report at PATH.",
    )
    group.addoption(
        "--automation-report-title",
        action="store",
        default=None,
        metavar="TITLE",
        help="Override the HTML report title.",
    )
    group.addoption(
        "--automation-report-subtitle",
        action="store",
        default=None,
        metavar="SUBTITLE",
        help="Override the subtitle text shown below the HTML report title.",
    )
    parser.addini(
        "automation_report",
        "Default output path for the generated HTML automation report.",
        default="",
    )
    parser.addini(
        "automation_report_title",
        "Default title for the generated HTML automation report.",
        default="Pytest Automation Report",
    )
    parser.addini(
        "automation_report_subtitle",
        "Default subtitle text shown below the generated HTML automation report title.",
        default=DEFAULT_REPORT_SUBTITLE,
    )


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        return

    report_path = config.getoption("--automation-report") or config.getini("automation_report")
    if not report_path:
        return

    title = config.getoption("--automation-report-title") or config.getini("automation_report_title")
    subtitle = config.getoption("--automation-report-subtitle") or config.getini("automation_report_subtitle")
    plugin = AutomationReportPlugin(config=config, report_path=Path(report_path), title=title, subtitle=subtitle)
    config.pluginmanager.register(plugin, "automation-report-plugin")


@dataclass
class TestResult:
    nodeid: str
    module_path: str
    class_name: str
    test_name: str
    outcome: str = "passed"
    phase_outcomes: dict[str, str] = field(default_factory=dict)
    phase_durations: dict[str, float] = field(default_factory=dict)
    total_duration: float = 0.0
    longrepr: str = ""
    attachments: list[dict[str, str]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        if self.class_name:
            return f"{self.class_name}::{self.test_name}"
        return self.test_name


class AutomationReportPlugin:
    def __init__(self, config: pytest.Config, report_path: Path, title: str, subtitle: str = DEFAULT_REPORT_SUBTITLE) -> None:
        self.config = config
        self.report_path = report_path
        self.title = title
        self.subtitle = subtitle
        self.results: dict[str, TestResult] = {}
        self.collected = 0
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self._item_nodeids: dict[pytest.Item, str] = {}

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        self.started_at = datetime.now(timezone.utc)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = len(session.items)
        for item in session.items:
            self._item_nodeids[item] = item.nodeid

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo[Any]):
        outcome = yield
        report = outcome.get_result()

        if report.when == "call" and report.failed:
            self._auto_attach_failure_screenshot(item)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        result = self.results.setdefault(report.nodeid, self._new_result(report.nodeid))
        result.phase_outcomes[report.when] = report.outcome
        result.phase_durations[report.when] = report.duration
        result.total_duration = sum(result.phase_durations.values())

        derived = self._derive_outcome(report)
        current_priority = self._outcome_priority(result.outcome)
        derived_priority = self._outcome_priority(derived)
        if derived_priority >= current_priority:
            result.outcome = derived
            if report.longrepr:
                result.longrepr = str(report.longrepr)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.finished_at = datetime.now(timezone.utc)
        self._write_report(session)

    def _new_result(self, nodeid: str) -> TestResult:
        parts = nodeid.split("::")
        module_path = parts[0]
        class_name = parts[1] if len(parts) > 2 else ""
        test_name = parts[-1]
        return TestResult(
            nodeid=nodeid,
            module_path=module_path,
            class_name=class_name,
            test_name=test_name,
        )

    def _derive_outcome(self, report: pytest.TestReport) -> str:
        wasxfail = bool(getattr(report, "wasxfail", False))
        if report.when == "call":
            if report.passed:
                return "xpassed" if wasxfail else "passed"
            if report.failed:
                return "failed"
            if report.skipped:
                return "xfailed" if wasxfail else "skipped"

        if report.when in {"setup", "teardown"}:
            if report.failed:
                return "error"
            if report.skipped:
                return "xfailed" if wasxfail else "skipped"

        return "passed"

    def _outcome_priority(self, outcome: str) -> int:
        priorities = {
            "passed": 0,
            "xfailed": 1,
            "skipped": 2,
            "xpassed": 3,
            "failed": 4,
            "error": 5,
        }
        return priorities.get(outcome, 0)

    def _auto_attach_failure_screenshot(self, item: pytest.Item) -> None:
        for fixture_name in ("driver", "browser", "selenium", "webdriver"):
            driver = item.funcargs.get(fixture_name)
            if driver is None:
                continue

            if self._attach_screenshot_from_driver(
                nodeid=item.nodeid,
                driver=driver,
                name=f"Failure Screenshot ({fixture_name})",
            ):
                return

        for fixture_name in ("page", "playwright_page"):
            page = item.funcargs.get(fixture_name)
            if page is None:
                continue

            if self._attach_screenshot_from_page(
                nodeid=item.nodeid,
                page=page,
                name=f"Failure Screenshot ({fixture_name})",
            ):
                return

        for fixture_name in ("context", "browser_context"):
            context = item.funcargs.get(fixture_name)
            if context is None:
                continue

            if self._attach_screenshot_from_browser_context(
                nodeid=item.nodeid,
                context=context,
                name=f"Failure Screenshot ({fixture_name})",
            ):
                return

    def _attach_screenshot_from_driver(self, nodeid: str, driver: Any, name: str) -> bool:
        get_base64 = getattr(driver, "get_screenshot_as_base64", None)
        if callable(get_base64):
            try:
                encoded = get_base64()
            except Exception:
                return False
            if encoded:
                self.attach_screenshot(nodeid=nodeid, image_base64=encoded, name=name)
                return True

        get_png = getattr(driver, "get_screenshot_as_png", None)
        if callable(get_png):
            try:
                png_bytes = get_png()
            except Exception:
                return False
            if png_bytes:
                self.attach_screenshot(nodeid=nodeid, image_bytes=png_bytes, name=name)
                return True

        return False

    def _attach_screenshot_from_page(self, nodeid: str, page: Any, name: str) -> bool:
        screenshot = getattr(page, "screenshot", None)
        if not callable(screenshot):
            return False

        try:
            image_bytes = screenshot(type="png")
        except TypeError:
            try:
                image_bytes = screenshot()
            except Exception:
                return False
        except Exception:
            return False

        if not image_bytes or not isinstance(image_bytes, (bytes, bytearray)):
            return False

        self.attach_screenshot(nodeid=nodeid, image_bytes=bytes(image_bytes), name=name)
        return True

    def _attach_screenshot_from_browser_context(self, nodeid: str, context: Any, name: str) -> bool:
        pages = getattr(context, "pages", None)
        if pages is None:
            return False

        try:
            candidates = list(pages)
        except TypeError:
            return False

        for page in reversed(candidates):
            if self._attach_screenshot_from_page(nodeid=nodeid, page=page, name=name):
                return True

        return False

    def attach_screenshot(
        self,
        *,
        nodeid: str,
        image_bytes: bytes | None = None,
        image_base64: str | None = None,
        name: str = "Screenshot",
        mime_type: str = "image/png",
    ) -> None:
        result = self.results.setdefault(nodeid, self._new_result(nodeid))
        encoded = image_base64 or (base64.b64encode(image_bytes).decode("ascii") if image_bytes else "")
        if not encoded:
            return

        result.attachments.append(
            {
                "name": name,
                "mime_type": mime_type,
                "content_base64": encoded,
            }
        )

    def _write_report(self, session: pytest.Session) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        html = self.build_report_html(session=session)
        self.report_path.write_text(html, encoding="utf-8")

    def build_report_html(self, session: pytest.Session | None = None) -> str:
        results = list(self.results.values())
        summary = self._build_summary(results)
        suite_name = str(self.config.rootpath if session is None else session.config.rootpath)
        failure_items = [result for result in results if result.outcome in {"failed", "error", "xpassed"}]
        generated_at = (self.finished_at or datetime.now(timezone.utc)).astimezone()

        donut_chart = render_donut_chart(
            "Outcome Distribution",
            [(label.title(), summary["counts"][label], OUTCOME_COLORS[label]) for label in OUTCOME_ORDER if summary["counts"][label] > 0],
        )
        slowest_results = sorted(results, key=lambda value: (-value.total_duration, value.nodeid))[:SLOWEST_TEST_LIMIT]
        module_chart = render_vertical_bar_chart(
            "Module Execution Breakdown",
            summary["module_durations"][:8],
            color="#2e86c1",
            formatter=format_seconds,
        )
        phase_chart = render_vertical_bar_chart(
            "Phase Duration Breakdown",
            [(phase.title(), summary["phase_totals"].get(phase, 0.0)) for phase in ["setup", "call", "teardown"]],
            color="#16a085",
            formatter=format_seconds,
            title_attr="Phase Duration Breakdown",
        )
        suite_outcome_chart = render_suite_outcome_chart(
            "Suite Execution Outcomes",
            summary["module_outcomes"][:SUITE_OUTCOME_LIMIT],
            title_attr="Suite Execution Outcomes",
        )

        metric_cards = "".join(
            [
                render_metric_card("Collected", str(self.collected or len(results))),
                render_metric_card("Executed", str(summary["executed"])),
                render_metric_card("Pass Rate", f"{summary['pass_rate']:.1f}%"),
                render_metric_card("Total Runtime", format_seconds(summary["total_duration"])),
                render_metric_card("Average Test", format_seconds(summary["average_duration"])),
                render_metric_card("Median Test", format_seconds(summary["median_duration"])),
            ]
        )
        outcome_badges = "".join(
            render_outcome_badge(outcome, summary["counts"][outcome])
            for outcome in OUTCOME_ORDER
            if summary["counts"][outcome] > 0
        )
        slowest_rows = "".join(
            f"""
            <tr>
              <td>{escape(item.display_name)}</td>
              <td>{escape(item.module_path)}</td>
              <td>{status_chip(item.outcome)}</td>
              <td>{format_seconds(item.total_duration)}</td>
            </tr>
            """
            for item in slowest_results
        )
        failure_cards = "".join(
            f"""
            <article class="failure-card" data-pagination-item>
              <header>
                <span>{status_chip(item.outcome)}</span>
                <strong>{escape(item.nodeid)}</strong>
              </header>
              <pre>{escape(item.longrepr or "No traceback captured.")}</pre>
              {render_attachment_gallery(item.attachments)}
            </article>
            """
            for item in failure_items
        )
        result_groups = "".join(
            render_result_group(index, item)
            for index, item in enumerate(
                sorted(
                    results,
                    key=lambda value: (OUTCOME_ORDER.index(value.outcome) if value.outcome in OUTCOME_ORDER else 99, value.nodeid),
                )
            )
        )
        failure_insights_markup = render_failure_insights(results)
        screenshots_markup = (
            f"""
          <div class="paginated-section" data-pagination-root data-page-size="{FAILURE_DETAILS_PAGE_SIZE}">
            {render_pagination_toolbar("screenshots")}
            <div class="failure-list" data-pagination-items>
              {failure_cards}
            </div>
          </div>
            """
            if failure_items
            else '<div class="empty-state">No failures, errors, or unexpected passes were recorded.</div>'
        )
        detailed_results_markup = (
            f"""
          <article class="table-card paginated-section" data-pagination-root data-page-size="{RESULTS_PAGE_SIZE}">
            {render_pagination_toolbar("detailed test results")}
            <table data-pagination-items>
              <thead>
                <tr>
                  <th>Node ID</th>
                  <th>Outcome</th>
                  <th>Setup</th>
                  <th>Call</th>
                  <th>Teardown</th>
                  <th>Total</th>
                  <th>Traceback</th>
                </tr>
              </thead>
              {result_groups}
            </table>
          </article>
            """
            if results
            else """
          <article class="table-card">
            <table>
              <thead>
                <tr>
                  <th>Node ID</th>
                  <th>Outcome</th>
                  <th>Setup</th>
                  <th>Call</th>
                  <th>Teardown</th>
                  <th>Total</th>
                  <th>Traceback</th>
                </tr>
              </thead>
              <tbody>
                <tr><td colspan="7" class="empty-state">No test results were collected.</td></tr>
              </tbody>
            </table>
          </article>
            """
        )

        return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{escape(self.title)}</title>
    <style>
      :root {{
        --bg: #f4f6f8;
        --surface: #ffffff;
        --surface-alt: #eef3f7;
        --text: #1f2d3d;
        --muted: #5d6d7e;
        --border: #d5dde5;
        --shadow: 0 16px 40px rgba(31, 45, 61, 0.08);
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        font-family: "Avenir Next", "Segoe UI", sans-serif;
        background:
          radial-gradient(circle at top left, rgba(93, 173, 226, 0.14), transparent 24%),
          linear-gradient(180deg, #f9fbfc 0%, #eef3f7 100%);
        color: var(--text);
      }}

      .container {{
        max-width: 1440px;
        margin: 0 auto;
        padding: 32px 20px 48px;
      }}

      .hero {{
        background: linear-gradient(135deg, #0f4c5c, #2c7da0);
        border-radius: 24px;
        color: white;
        padding: 28px;
        box-shadow: var(--shadow);
      }}

      .hero h1 {{
        margin: 0 0 8px;
        font-size: clamp(2rem, 4vw, 3rem);
        line-height: 1.1;
      }}

      .hero p {{
        margin: 0;
        color: rgba(255, 255, 255, 0.85);
      }}

      .sub-meta {{
        margin-top: 18px;
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
      }}

      .sub-meta span,
      .badge {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 8px 12px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.12);
        backdrop-filter: blur(10px);
        font-size: 0.92rem;
      }}

      .metrics,
      .chart-grid,
      .insights-grid,
      .slow-grid {{
        display: grid;
        gap: 16px;
        margin-top: 24px;
      }}

      .tab-nav {{
        display: flex;
        gap: 12px;
        margin-top: 24px;
        flex-wrap: wrap;
      }}

      .tab-button {{
        appearance: none;
        border: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.78);
        color: var(--text);
        border-radius: 999px;
        padding: 10px 16px;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        box-shadow: var(--shadow);
        transition: transform 120ms ease, background 120ms ease, color 120ms ease;
      }}

      .tab-button:hover {{
        transform: translateY(-1px);
      }}

      .tab-button.is-active {{
        background: linear-gradient(135deg, #0f4c5c, #2c7da0);
        color: white;
        border-color: transparent;
      }}

      .tab-panel {{
        display: none;
      }}

      .tab-panel.is-active {{
        display: block;
      }}

      .metrics {{
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      }}

      .chart-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
        align-items: start;
      }}

      .slow-grid {{
        grid-template-columns: 2fr 1fr;
      }}

      .insights-grid {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}

      .card,
      .table-card,
      .failure-card {{
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 20px;
        box-shadow: var(--shadow);
      }}

      .card,
      .table-card {{
        padding: 20px;
      }}

      .metric-card h2 {{
        margin: 0 0 10px;
        color: var(--muted);
        font-size: 0.95rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
      }}

      .metric-card strong {{
        font-size: 2rem;
        line-height: 1;
      }}

      .section-title {{
        margin: 28px 0 12px;
        font-size: 1.2rem;
      }}

      .chart-title {{
        margin: 0 0 14px;
        font-size: 1.15rem;
      }}

      .chart-title[title] {{
        cursor: help;
      }}

      .chart-card {{
        display: flex;
        flex-direction: column;
      }}

      .chart-visual {{
        min-height: 300px;
        display: flex;
        align-items: stretch;
      }}

      .legend {{
        display: flex;
        flex-wrap: wrap;
        gap: 8px 12px;
        margin-top: 12px;
      }}

      .legend-item {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: var(--muted);
        font-size: 0.9rem;
      }}

      .legend-dot {{
        width: 12px;
        height: 12px;
        border-radius: 999px;
      }}

      .insight-card {{
        display: flex;
        flex-direction: column;
        gap: 10px;
      }}

      .insight-title {{
        margin: 0;
        font-size: 1rem;
      }}

      .insight-list {{
        margin: 0;
        padding-left: 20px;
        color: var(--text);
      }}

      .insight-list li {{
        margin-bottom: 8px;
      }}

      table {{
        width: 100%;
        border-collapse: collapse;
      }}

      th,
      td {{
        padding: 12px 10px;
        border-bottom: 1px solid var(--border);
        text-align: left;
        vertical-align: top;
        font-size: 0.95rem;
      }}

      th {{
        color: var(--muted);
        font-weight: 700;
      }}

      tr:last-child td {{
        border-bottom: none;
      }}

      .status-chip {{
        display: inline-flex;
        align-items: center;
        padding: 5px 10px;
        border-radius: 999px;
        color: white;
        font-size: 0.82rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }}

      .failure-list {{
        display: grid;
        gap: 16px;
      }}

      .paginated-section {{
        display: grid;
        gap: 14px;
      }}

      .pagination-toolbar {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
      }}

      .pagination-summary,
      .pagination-status {{
        margin: 0;
        color: var(--muted);
        font-size: 0.92rem;
      }}

      .pagination-actions {{
        display: inline-flex;
        align-items: center;
        gap: 10px;
        flex-wrap: wrap;
      }}

      .pagination-button {{
        appearance: none;
        border: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.88);
        color: var(--text);
        border-radius: 999px;
        padding: 8px 14px;
        font: inherit;
        font-size: 0.92rem;
        font-weight: 700;
        cursor: pointer;
      }}

      .pagination-button:disabled {{
        opacity: 0.55;
        cursor: not-allowed;
      }}

      .failure-card {{
        padding: 18px;
      }}

      .failure-card header {{
        display: flex;
        gap: 12px;
        align-items: center;
        flex-wrap: wrap;
        margin-bottom: 12px;
      }}

      pre {{
        margin: 0;
        overflow-x: auto;
        white-space: pre-wrap;
        background: #13202b;
        color: #f8f9f9;
        padding: 14px;
        border-radius: 14px;
      }}

      .attachment-gallery {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 12px;
        margin-top: 14px;
      }}

      .attachment-card {{
        background: var(--surface-alt);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 12px;
      }}

      .attachment-card h3 {{
        margin: 0 0 10px;
        font-size: 0.92rem;
      }}

      .attachment-card img {{
        display: block;
        width: 100%;
        height: auto;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: white;
      }}

      .traceback-cell {{
        min-width: 220px;
      }}

      .result-group[hidden] {{
        display: none;
      }}

      .traceback-toggle {{
        appearance: none;
        border: 1px solid var(--border);
        background: rgba(255, 255, 255, 0.92);
        color: #0f4c5c;
        border-radius: 999px;
        padding: 7px 12px;
        font: inherit;
        font-size: 0.9rem;
        font-weight: 700;
        cursor: pointer;
      }}

      .traceback-toggle:hover {{
        background: var(--surface-alt);
      }}

      .traceback-row td {{
        padding: 0 10px 14px;
        border-bottom: 1px solid var(--border);
      }}

      .traceback-panel {{
        margin: 0;
      }}

      .traceback-row[hidden] {{
        display: none;
      }}

      .traceback-empty {{
        color: var(--muted);
      }}

      .empty-state {{
        color: var(--muted);
        padding: 18px;
        border-radius: 16px;
        background: var(--surface-alt);
      }}

      .chart-card svg {{
        width: 100%;
        height: 280px;
        display: block;
      }}

      .chart-visual .empty-state {{
        width: 100%;
      }}

      @media (max-width: 1360px) {{
        .chart-grid {{
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }}

        .insights-grid {{
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }}
      }}

      @media (max-width: 1100px) {{
        .chart-grid {{
          grid-template-columns: 1fr;
        }}

        .insights-grid {{
          grid-template-columns: 1fr;
        }}

        .chart-card svg {{
          height: 260px;
        }}
      }}

      @media (max-width: 960px) {{
        .slow-grid {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="container">
      <section class="hero">
        <h1>{escape(self.title)}</h1>
        <p>{escape(self.subtitle)}</p>
        <div class="sub-meta">
          <span>Suite: {escape(suite_name)}</span>
          <span>Generated: {escape(generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"))}</span>
          <span>Python: {escape(platform.python_version())}</span>
          <span>pytest: {escape(pytest.__version__)}</span>
        </div>
        <div class="sub-meta">
          {outcome_badges}
        </div>
      </section>

      <nav class="tab-nav" aria-label="Report sections">
        <button class="tab-button is-active" type="button" data-tab-target="summary" aria-selected="true">Summary</button>
        <button class="tab-button" type="button" data-tab-target="details" aria-selected="false">Details</button>
        <button class="tab-button" type="button" data-tab-target="screenshots" aria-selected="false">Screenshots</button>
      </nav>

      <section class="tab-panel is-active" data-tab-panel="summary">
        <section class="metrics">
          {metric_cards}
        </section>

        <section class="chart-grid">
          {donut_chart}
          {phase_chart}
          {module_chart}
          {suite_outcome_chart}
        </section>

        <section class="insights-grid">
          {failure_insights_markup}
        </section>

        <section class="slow-grid">
          <article class="table-card">
            <h2 class="chart-title">Slowest Tests</h2>
            <table>
              <thead>
                <tr>
                  <th>Test</th>
                  <th>Module</th>
                  <th>Outcome</th>
                  <th>Total Duration</th>
                </tr>
              </thead>
              <tbody>
                {slowest_rows or '<tr><td colspan="4" class="empty-state">No test timings were captured.</td></tr>'}
              </tbody>
            </table>
          </article>

          <article class="table-card">
            <h2 class="chart-title">Run Snapshot</h2>
            <table>
              <tbody>
                <tr><th>Started At</th><td>{escape(self._format_timestamp(self.started_at))}</td></tr>
                <tr><th>Finished At</th><td>{escape(self._format_timestamp(self.finished_at))}</td></tr>
                <tr><th>Collected Tests</th><td>{self.collected or len(results)}</td></tr>
                <tr><th>Executed Tests</th><td>{summary["executed"]}</td></tr>
                <tr><th>Failures + Errors</th><td>{summary["counts"]["failed"] + summary["counts"]["error"] + summary["counts"]["xpassed"]}</td></tr>
              </tbody>
            </table>
          </article>
        </section>
      </section>

      <section class="tab-panel" data-tab-panel="details">
        <section>
          <h2 class="section-title">Detailed Test Results</h2>
          {detailed_results_markup}
        </section>
      </section>

      <section class="tab-panel" data-tab-panel="screenshots">
        <section>
          <h2 class="section-title">Failure Details</h2>
          {screenshots_markup}
        </section>
      </section>
    </main>
    <script>
      const tabButtons = Array.from(document.querySelectorAll('[data-tab-target]'));
      const tabPanels = Array.from(document.querySelectorAll('[data-tab-panel]'));
      const paginationRoots = Array.from(document.querySelectorAll('[data-pagination-root]'));
      const tracebackToggles = Array.from(document.querySelectorAll('[data-traceback-toggle]'));

      for (const button of tabButtons) {{
        button.addEventListener('click', () => {{
          const target = button.getAttribute('data-tab-target');

          for (const otherButton of tabButtons) {{
            const active = otherButton === button;
            otherButton.classList.toggle('is-active', active);
            otherButton.setAttribute('aria-selected', active ? 'true' : 'false');
          }}

          for (const panel of tabPanels) {{
            panel.classList.toggle('is-active', panel.getAttribute('data-tab-panel') === target);
          }}
        }});
      }}

      for (const root of paginationRoots) {{
        const itemsParent = root.querySelector('[data-pagination-items]');
        const items = itemsParent ? Array.from(itemsParent.querySelectorAll('[data-pagination-item]')) : [];
        const summary = root.querySelector('[data-pagination-summary]');
        const status = root.querySelector('[data-pagination-status]');
        const previousButton = root.querySelector('[data-pagination-prev]');
        const nextButton = root.querySelector('[data-pagination-next]');
        const requestedSize = Number.parseInt(root.getAttribute('data-page-size') || '20', 10);
        const pageSize = Number.isFinite(requestedSize) && requestedSize > 0 ? requestedSize : 20;
        const totalItems = items.length;
        const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
        let currentPage = 1;

        const renderPage = () => {{
          const start = (currentPage - 1) * pageSize;
          const end = start + pageSize;

          items.forEach((item, index) => {{
            item.hidden = index < start || index >= end;
          }});

          const rangeStart = totalItems === 0 ? 0 : start + 1;
          const rangeEnd = totalItems === 0 ? 0 : Math.min(end, totalItems);

          if (summary) {{
            summary.textContent = totalItems <= pageSize
              ? `Showing all ${{totalItems}} items`
              : `Showing ${{rangeStart}}-${{rangeEnd}} of ${{totalItems}} items`;
          }}

          if (status) {{
            status.textContent = `Page ${{currentPage}} of ${{totalPages}}`;
          }}

          if (previousButton) {{
            previousButton.disabled = currentPage === 1;
          }}

          if (nextButton) {{
            nextButton.disabled = currentPage === totalPages;
          }}
        }};

        if (previousButton) {{
          previousButton.addEventListener('click', () => {{
            if (currentPage > 1) {{
              currentPage -= 1;
              renderPage();
            }}
          }});
        }}

        if (nextButton) {{
          nextButton.addEventListener('click', () => {{
            if (currentPage < totalPages) {{
              currentPage += 1;
              renderPage();
            }}
          }});
        }}

        renderPage();
      }}

      for (const toggle of tracebackToggles) {{
        toggle.addEventListener('click', () => {{
          const rowId = toggle.getAttribute('aria-controls');
          if (!rowId) {{
            return;
          }}

          const detailRow = document.getElementById(rowId);
          if (!detailRow) {{
            return;
          }}

          const willExpand = detailRow.hidden;
          detailRow.hidden = !willExpand;
          toggle.setAttribute('aria-expanded', willExpand ? 'true' : 'false');
          toggle.textContent = willExpand ? 'Hide stack trace' : 'View stack trace';
        }});
      }}
    </script>
  </body>
</html>
"""

    def _build_summary(self, results: list[TestResult]) -> dict[str, Any]:
        counts = Counter(result.outcome for result in results)
        phase_totals = {
            phase: sum(result.phase_durations.get(phase, 0.0) for result in results)
            for phase in ("setup", "call", "teardown")
        }
        durations = [result.total_duration for result in results]
        module_durations = Counter()
        module_outcome_counts: dict[str, Counter[str]] = {}
        for result in results:
            module_durations[result.module_path] += result.total_duration
            module_counter = module_outcome_counts.setdefault(result.module_path, Counter())
            module_counter[normalize_outcome_bucket(result.outcome)] += 1

        executed = len(results)
        passed = counts["passed"]
        pass_rate = (passed / executed * 100.0) if executed else 0.0
        ordered_modules = sorted(module_durations.items(), key=lambda item: item[1], reverse=True)
        ordered_module_outcomes = sorted(
            (
                (
                    module,
                    module_counter["passed"],
                    module_counter["failed"],
                    module_counter["skipped"],
                    module_counter["error"],
                    sum(module_counter.values()),
                )
                for module, module_counter in module_outcome_counts.items()
            ),
            key=lambda item: (item[5], item[2] + item[4], item[0]),
            reverse=True,
        )

        return {
            "counts": counts,
            "executed": executed,
            "pass_rate": pass_rate,
            "total_duration": sum(durations),
            "average_duration": (sum(durations) / executed) if executed else 0.0,
            "median_duration": median(durations) if durations else 0.0,
            "phase_totals": phase_totals,
            "module_durations": ordered_modules,
            "module_outcomes": ordered_module_outcomes,
        }

    def _format_timestamp(self, value: datetime | None) -> str:
        if value is None:
            return "N/A"
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def attach_screenshot(
    request: pytest.FixtureRequest,
    *,
    image_bytes: bytes | None = None,
    image_base64: str | None = None,
    name: str = "Screenshot",
    mime_type: str = "image/png",
) -> None:
    plugin = request.config.pluginmanager.get_plugin("automation-report-plugin")
    if plugin is None:
        return

    plugin.attach_screenshot(
        nodeid=request.node.nodeid,
        image_bytes=image_bytes,
        image_base64=image_base64,
        name=name,
        mime_type=mime_type,
    )


@pytest.fixture
def automation_report(request: pytest.FixtureRequest) -> Callable[..., None]:
    def _attach(
        *,
        image_bytes: bytes | None = None,
        image_base64: str | None = None,
        name: str = "Screenshot",
        mime_type: str = "image/png",
    ) -> None:
        attach_screenshot(
            request,
            image_bytes=image_bytes,
            image_base64=image_base64,
            name=name,
            mime_type=mime_type,
        )

    return _attach


def render_metric_card(label: str, value: str) -> str:
    return f"""
    <article class="card metric-card">
      <h2>{escape(label)}</h2>
      <strong>{escape(value)}</strong>
    </article>
    """


def render_outcome_badge(outcome: str, count: int) -> str:
    return (
        f'<span class="badge"><span class="status-chip" style="background:{OUTCOME_COLORS[outcome]}">'
        f"{escape(outcome)}</span>{count}</span>"
    )


def status_chip(outcome: str) -> str:
    return (
        f'<span class="status-chip" style="background:{OUTCOME_COLORS.get(outcome, "#566573")}">'
        f"{escape(outcome)}</span>"
    )


def render_attachment_gallery(attachments: list[dict[str, str]]) -> str:
    if not attachments:
        return ""

    cards = "".join(
        f"""
        <section class="attachment-card">
          <h3>{escape(attachment["name"])}</h3>
          <img
            src="data:{escape(attachment['mime_type'])};base64,{attachment['content_base64']}"
            alt="{escape(attachment['name'])}"
          />
        </section>
        """
        for attachment in attachments
    )
    return f'<div class="attachment-gallery">{cards}</div>'


def render_result_group(index: int, item: TestResult) -> str:
    traceback_id = f"traceback-row-{index}"
    traceback_cell = render_traceback_toggle(traceback_id, item.longrepr)
    traceback_row = render_traceback_row(traceback_id, item.longrepr)
    return f"""
    <tbody class="result-group" data-pagination-item>
      <tr>
        <td>{escape(item.nodeid)}</td>
        <td>{status_chip(item.outcome)}</td>
        <td>{format_seconds(item.phase_durations.get("setup", 0.0))}</td>
        <td>{format_seconds(item.phase_durations.get("call", 0.0))}</td>
        <td>{format_seconds(item.phase_durations.get("teardown", 0.0))}</td>
        <td>{format_seconds(item.total_duration)}</td>
        <td class="traceback-cell">{traceback_cell}</td>
      </tr>
      {traceback_row}
    </tbody>
    """


def render_traceback_toggle(traceback_id: str, longrepr: str) -> str:
    if not longrepr:
        return '<span class="traceback-empty">—</span>'

    return f"""
    <button
      class="traceback-toggle"
      type="button"
      data-traceback-toggle
      aria-expanded="false"
      aria-controls="{escape(traceback_id)}"
    >
      View stack trace
    </button>
    """


def render_traceback_row(traceback_id: str, longrepr: str) -> str:
    if not longrepr:
        return ""

    return f"""
    <tr class="traceback-row" id="{escape(traceback_id)}" hidden>
      <td colspan="7">
        <pre class="traceback-panel">{escape(longrepr)}</pre>
      </td>
    </tr>
    """


def render_pagination_toolbar(section_name: str) -> str:
    return f"""
    <div class="pagination-toolbar">
      <p class="pagination-summary" data-pagination-summary aria-live="polite"></p>
      <div class="pagination-actions">
        <button
          class="pagination-button"
          type="button"
          data-pagination-prev
          aria-label="Previous page for {escape(section_name)}"
        >
          Previous
        </button>
        <span class="pagination-status" data-pagination-status aria-live="polite"></span>
        <button
          class="pagination-button"
          type="button"
          data-pagination-next
          aria-label="Next page for {escape(section_name)}"
        >
          Next
        </button>
      </div>
    </div>
    """


def render_donut_chart(title: str, segments: list[tuple[str, float, str]]) -> str:
    total = sum(value for _, value, _ in segments)
    if total <= 0:
        return render_empty_chart(title, "No outcome data was captured.")

    radius = 72
    circumference = 2 * math.pi * radius
    offset = 0.0
    circles = []
    legends = []

    for label, value, color in segments:
        fraction = value / total
        dash = circumference * fraction
        circles.append(
            f'<circle cx="100" cy="100" r="{radius}" fill="none" stroke="{color}" stroke-width="24" '
            f'stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" stroke-dashoffset="{-offset:.2f}" '
            'transform="rotate(-90 100 100)" stroke-linecap="butt"></circle>'
        )
        offset += dash
        legends.append(legend_item(label, value, color))

    svg = f"""
    <svg viewBox="0 0 200 200" role="img" aria-label="{escape(title)}">
      <circle cx="100" cy="100" r="{radius}" fill="none" stroke="#e5eaee" stroke-width="24"></circle>
      {''.join(circles)}
      <text x="100" y="86" text-anchor="middle" font-size="12" fill="#5d6d7e" letter-spacing="0.08em">Tests</text>
      <text x="100" y="124" text-anchor="middle" font-size="30" font-weight="700" fill="#1f2d3d">{int(total)}</text>
    </svg>
    """
    return render_chart_card(title, svg, "".join(legends))


def render_horizontal_bar_chart(
    title: str,
    data: list[tuple[str, float, str]],
    *,
    unit: str = "",
    formatter: Callable[[float], str] | None = None,
) -> str:
    if not data:
        return render_empty_chart(title, "No timing data was captured.")

    max_value = max(value for _, value, _ in data) or 1
    bar_height = 28
    gap = 18
    width = 640
    left_pad = 220
    chart_width = width - left_pad - 40
    height = len(data) * (bar_height + gap) + 30
    rows = []
    legends = []

    for index, (label, value, color) in enumerate(data):
        y = index * (bar_height + gap) + 10
        bar_width = chart_width * (value / max_value)
        shown = formatter(value) if formatter else f"{value:.2f}{unit}"
        rows.append(
            f"""
            <text x="8" y="{y + 19}" font-size="12" fill="#34495e">{escape(trim_label(label, 34))}</text>
            <rect x="{left_pad}" y="{y}" width="{bar_width:.2f}" height="{bar_height}" rx="10" fill="{color}"></rect>
            <text x="{left_pad + bar_width + 10:.2f}" y="{y + 19}" font-size="12" fill="#34495e">{escape(shown)}</text>
            """
        )
        legends.append(legend_item(label, shown, color))

    svg = f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
      {''.join(rows)}
    </svg>
    """
    return render_chart_card(title, svg, "".join(legends[:5]))


def render_vertical_bar_chart(
    title: str,
    data: list[tuple[str, float]],
    *,
    color: str,
    formatter: Callable[[float], str] | None = None,
    title_attr: str | None = None,
) -> str:
    if not data:
        return render_empty_chart(title, "No module or phase data was captured.")

    max_value = max(value for _, value in data) or 1
    width = 640
    height = 320
    bottom = 250
    left = 44
    chart_width = width - left - 20
    bar_gap = 18
    bar_width = max(36.0, (chart_width - (len(data) - 1) * bar_gap) / max(len(data), 1))
    bars = []
    legends = []

    for index, (label, value) in enumerate(data):
        x = left + index * (bar_width + bar_gap)
        bar_height = 180 * (value / max_value)
        y = bottom - bar_height
        shown = formatter(value) if formatter else f"{value:.2f}"
        bars.append(
            f"""
            <g>
              <title>{escape(label)}</title>
              <rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{bar_height:.2f}" rx="12" fill="{color}"></rect>
              <text x="{x + bar_width / 2:.2f}" y="{bottom + 18}" text-anchor="middle" font-size="12" fill="#34495e">{escape(trim_label(label, 12))}</text>
              <text x="{x + bar_width / 2:.2f}" y="{y - 8:.2f}" text-anchor="middle" font-size="12" fill="#34495e">{escape(shown)}</text>
            </g>
            """
        )
        legends.append(legend_item(label, shown, color))

    svg = f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
      <line x1="{left}" y1="{bottom}" x2="{width - 10}" y2="{bottom}" stroke="#ccd6dd" stroke-width="2"></line>
      {''.join(bars)}
    </svg>
    """
    return render_chart_card(title, svg, "".join(legends), title_attr=title_attr)


def render_empty_chart(title: str, message: str) -> str:
    return render_chart_card(title, f'<div class="empty-state">{escape(message)}</div>', "")


def render_chart_card(title: str, chart_markup: str, legend_markup: str, *, title_attr: str | None = None) -> str:
    if title_attr and chart_markup.lstrip().startswith("<svg"):
        svg_open, svg_rest = chart_markup.split(">", 1)
        chart_markup = f"{svg_open}><title>{escape(title_attr)}</title>{svg_rest}"
    return f"""
    <article class="card chart-card">
      <h2 class="chart-title">{escape(title)}</h2>
      <div class="chart-visual"{f' title="{escape(title_attr)}"' if title_attr else ''}>
        {chart_markup}
      </div>
      {'<div class="legend">' + legend_markup + '</div>' if legend_markup else ''}
    </article>
    """


def render_suite_outcome_chart(
    title: str,
    module_outcomes: list[tuple[str, int, int, int, int, int]],
    *,
    title_attr: str | None = None,
) -> str:
    if not module_outcomes:
        return render_empty_chart(title, "No suite-level execution data was captured.")

    width = 760
    height = 320
    bottom = 250
    left = 44
    chart_width = width - left - 20
    bar_gap = 18
    bar_width = max(42.0, (chart_width - (len(module_outcomes) - 1) * bar_gap) / max(len(module_outcomes), 1))
    bars = []
    total_passed = 0
    total_failed = 0
    total_skipped = 0
    total_error = 0
    max_total = max(total for _, _, _, _, _, total in module_outcomes) or 1

    for index, (module, passed, failed, skipped, error, total) in enumerate(module_outcomes):
        total_passed += passed
        total_failed += failed
        total_skipped += skipped
        total_error += error
        x = left + index * (bar_width + bar_gap)
        y = bottom
        scale = 180 / max_total
        segments = [
            ("Passed", passed, OUTCOME_COLORS["passed"]),
            ("Failed", failed, OUTCOME_COLORS["failed"]),
            ("Skipped", skipped, OUTCOME_COLORS["skipped"]),
            ("Error", error, OUTCOME_COLORS["error"]),
        ]
        segment_markup = []
        for label, value, color in segments:
            if value <= 0:
                continue
            seg_height = value * scale
            y -= seg_height
            segment_markup.append(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{seg_height:.2f}" rx="6" fill="{color}"></rect>'
            )

        bars.append(
            f"""
            <g>
              <title>{escape(module)}</title>
              {''.join(segment_markup)}
              <text x="{x + bar_width / 2:.2f}" y="{bottom + 18}" text-anchor="middle" font-size="12" fill="#34495e">{escape(trim_label(module, 12))}</text>
              <text x="{x + bar_width / 2:.2f}" y="{y - 8:.2f}" text-anchor="middle" font-size="12" fill="#34495e">{total}</text>
            </g>
            """
        )

    legend_parts = []
    if total_passed > 0:
        legend_parts.append(legend_item("Passed", total_passed, OUTCOME_COLORS["passed"]))
    if total_failed > 0:
        legend_parts.append(legend_item("Failed", total_failed, OUTCOME_COLORS["failed"]))
    if total_skipped > 0:
        legend_parts.append(legend_item("Skipped", total_skipped, OUTCOME_COLORS["skipped"]))
    if total_error > 0:
        legend_parts.append(legend_item("Error", total_error, OUTCOME_COLORS["error"]))

    svg = f"""
    <svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
      <line x1="{left}" y1="{bottom}" x2="{width - 10}" y2="{bottom}" stroke="#ccd6dd" stroke-width="2"></line>
      {''.join(bars)}
    </svg>
    """
    return render_chart_card(title, svg, "".join(legend_parts), title_attr=title_attr)


def render_failure_insights(results: list[TestResult]) -> str:
    failing_modules = Counter()
    failure_signatures = Counter()
    skip_reasons = Counter()

    for result in results:
        bucket = normalize_outcome_bucket(result.outcome)
        if bucket in {"failed", "error"}:
            failing_modules[result.module_path] += 1
            failure_signatures[extract_failure_signature(result.longrepr)] += 1
        if bucket == "skipped":
            skip_reasons[extract_skip_reason(result.longrepr)] += 1

    failing_modules_items = [
        (trim_label(module, 48), count) for module, count in failing_modules.most_common(INSIGHT_LIST_LIMIT)
    ] or [("No failing modules captured.", 0)]
    failure_signature_items = [
        (trim_label(signature, 64), count) for signature, count in failure_signatures.most_common(INSIGHT_LIST_LIMIT)
    ] or [("No failure signatures captured.", 0)]
    skip_reason_items = [
        (trim_label(reason, 64), count) for reason, count in skip_reasons.most_common(INSIGHT_LIST_LIMIT)
    ] or [("No skip reasons captured.", 0)]

    return "".join(
        [
            render_insight_card("Top Failing Modules", failing_modules_items, "failures"),
            render_insight_card("Common Failure Signatures", failure_signature_items, "occurrences"),
            render_insight_card("Skip Reasons", skip_reason_items, "skips"),
        ]
    )


def render_insight_card(title: str, items: list[tuple[str, int]], unit: str) -> str:
    rows = "".join(
        f"<li><strong>{escape(str(count))}</strong> {escape(unit)} - {escape(label)}</li>"
        if count > 0
        else f"<li>{escape(label)}</li>"
        for label, count in items
    )
    return f"""
    <article class="table-card insight-card">
      <h2 class="insight-title">{escape(title)}</h2>
      <ol class="insight-list">
        {rows}
      </ol>
    </article>
    """


def normalize_outcome_bucket(outcome: str) -> str:
    if outcome in {"failed", "xpassed"}:
        return "failed"
    if outcome == "xfailed":
        return "skipped"
    if outcome in {"passed", "skipped", "error"}:
        return outcome
    return "error"


def extract_failure_signature(longrepr: str) -> str:
    if not longrepr:
        return "No traceback captured."

    for raw_line in longrepr.splitlines():
        line = raw_line.strip()
        if line.startswith("E   "):
            return line[4:].strip() or "Failure without message."
        if line.startswith("E "):
            return line[2:].strip() or "Failure without message."

    for raw_line in longrepr.splitlines():
        line = raw_line.strip()
        if line:
            return line
    return "Failure without message."


def extract_skip_reason(longrepr: str) -> str:
    if not longrepr:
        return "Skipped without reason."
    marker = "Skipped:"
    if marker in longrepr:
        reason = longrepr.split(marker, 1)[1].strip()
        return reason or "Skipped without reason."
    for raw_line in longrepr.splitlines():
        line = raw_line.strip()
        if line:
            return line
    return "Skipped without reason."


def legend_item(label: str, value: Any, color: str) -> str:
    return (
        f'<span class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
        f"{escape(trim_label(str(label), 24))}: {escape(str(value))}</span>"
    )


def format_seconds(value: float) -> str:
    return f"{value:.3f}s"


def trim_label(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1] + "…"
