import os
import time

import pytest

from test_demo_screenshot import DEMO_PNG_BASE64, DemoDriver


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOMATION_REPORT_DEMO") != "1",
    reason="Manual demo tests are only enabled when AUTOMATION_REPORT_DEMO=1",
)


@pytest.fixture
def driver():
    return DemoDriver()


def test_checkout_happy_path(driver, automation_report):
    time.sleep(0.08)
    automation_report(image_base64=driver.get_screenshot_as_base64(), name="Checkout dashboard")
    assert "checkout" in "checkout dashboard"


def test_checkout_payment_retry(driver, automation_report):
    time.sleep(0.11)
    automation_report(image_base64=DEMO_PNG_BASE64, name="Checkout retry state")
    assert False, "Intentional checkout failure for suite coverage"


def test_checkout_archived_flow():
    time.sleep(0.03)
    pytest.skip("Intentional skipped checkout flow")
