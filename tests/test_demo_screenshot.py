import base64
import os
import struct
import time
import zlib

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOMATION_REPORT_DEMO") != "1",
    reason="Manual demo tests are only enabled when AUTOMATION_REPORT_DEMO=1",
)


def build_demo_png_base64():
    width = 180
    height = 96
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if y < 24:
                row.extend((20, 98, 163))
            elif y < 48:
                row.extend((39, 174, 96))
            elif y < 72:
                row.extend((243, 156, 18))
            else:
                row.extend((192, 57, 43))

            if 48 < x < 132 and 32 < y < 64:
                row[-3:] = bytes((245, 245, 245))
        rows.append(bytes(row))

    raw = b"".join(rows)
    compressed = zlib.compress(raw, level=9)

    def chunk(chunk_type, data):
        return (
            struct.pack(">I", len(data))
            + chunk_type
            + data
            + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        )

    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            chunk(b"IDAT", compressed),
            chunk(b"IEND", b""),
        ]
    )
    return base64.b64encode(png).decode("ascii")


DEMO_PNG_BASE64 = build_demo_png_base64()


class DemoDriver:
    def get_screenshot_as_base64(self):
        return DEMO_PNG_BASE64


@pytest.fixture
def driver():
    return DemoDriver()


PASSING_DEMO_CASES = [
    ("login_smoke", 0.03),
    ("checkout_smoke", 0.04),
    ("account_profile", 0.05),
    ("search_results", 0.03),
    ("wishlist_sync", 0.04),
    ("inventory_refresh", 0.06),
    ("billing_summary", 0.05),
    ("saved_cards", 0.04),
    ("notifications_panel", 0.03),
    ("analytics_dashboard", 0.08),
    ("user_directory", 0.07),
    ("team_permissions", 0.06),
    ("audit_history", 0.09),
    ("theme_preferences", 0.04),
    ("session_timeout", 0.05),
]

FAILING_DEMO_CASES = [
    ("checkout_banner", 0.14),
    ("payment_modal", 0.17),
    ("refund_status", 0.12),
    ("invoice_download", 0.16),
    ("shipping_estimate", 0.20),
    ("team_invite", 0.15),
]

SKIPPED_DEMO_CASES = [
    ("legacy_import", 0.03),
    ("beta_feature_gate", 0.02),
]


def test_demo_selenium_failure_with_screenshot(driver):
    time.sleep(0.24)
    assert "Checkout complete" == "Checkout failed"


def test_demo_manual_attachment(driver, automation_report):
    time.sleep(0.18)
    automation_report(
        image_base64=driver.get_screenshot_as_base64(),
        name="Manual demo screenshot",
    )
    assert False, "Intentional demo failure so the screenshot appears in the failure section"


@pytest.mark.parametrize(("case_name", "duration_seconds"), PASSING_DEMO_CASES)
def test_demo_passing_cases(case_name, duration_seconds):
    time.sleep(duration_seconds)
    assert case_name


@pytest.mark.parametrize(("case_name", "duration_seconds"), FAILING_DEMO_CASES)
def test_demo_failure_cases(case_name, duration_seconds, driver, automation_report):
    time.sleep(duration_seconds)
    automation_report(
        image_base64=driver.get_screenshot_as_base64(),
        name=f"Failure state: {case_name}",
    )
    pytest.fail(f"Intentional demo failure for pagination coverage: {case_name}")


@pytest.mark.parametrize(("case_name", "duration_seconds"), SKIPPED_DEMO_CASES)
def test_demo_skipped_cases(case_name, duration_seconds):
    time.sleep(duration_seconds)
    pytest.skip(f"Intentional demo skip for report variety: {case_name}")
