import base64
import os
import struct
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


def test_demo_selenium_failure_with_screenshot(driver):
    assert "Checkout complete" == "Checkout failed"


def test_demo_manual_attachment(driver, automation_report):
    automation_report(
        image_base64=driver.get_screenshot_as_base64(),
        name="Manual demo screenshot",
    )
    assert False, "Intentional demo failure so the screenshot appears in the failure section"
