import os
import time

import pytest

from test_demo_screenshot import DemoDriver


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOMATION_REPORT_DEMO") != "1",
    reason="Manual demo tests are only enabled when AUTOMATION_REPORT_DEMO=1",
)


@pytest.fixture
def driver():
    return DemoDriver()


def test_admin_user_list(driver, automation_report):
    time.sleep(0.07)
    automation_report(image_base64=driver.get_screenshot_as_base64(), name="Admin user list")
    assert "admin" in "admin user list"


def test_admin_role_sync(driver, automation_report):
    time.sleep(0.1)
    automation_report(image_base64=driver.get_screenshot_as_base64(), name="Role sync failed")
    pytest.fail("Intentional admin failure for suite coverage")


def test_admin_audit_history():
    time.sleep(0.04)
    pytest.skip("Intentional skipped admin flow")
