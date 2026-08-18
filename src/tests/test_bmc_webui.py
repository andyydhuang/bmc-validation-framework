"""
src/tests/test_bmc_webui.py

Hardware integration tests for BMC Web UI FRU validation via Selenium.

TC IDs covered:
    TC_BMC_2_0400 — Front panel FRU via Web UI
    TC_BMC_2_0401 — Motherboard FRU via Web UI
    TC_BMC_2_0402 — UBB FRU via Web UI
    TC_BMC_2_0403 — IO board NIC FRU via Web UI
    TC_BMC_2_0404 — MB OCP NIC FRU via Web UI

These tests complement the IPMI-path FRU tests (TC_BMC_0_0400/0401)
by verifying the SAME physical FRU EEPROM data through a completely
different BMC firmware code path — the HTTPS web interface.

Dual-path design rationale:
    IPMI path:  ipmitool fru print → NetFn 0x0A Cmd 0x11 → EEPROM
    Web UI path: Chrome → HTTPS → BMC web server → same EEPROM

Both paths read from the same physical EEPROM but through separate
BMC firmware modules. A bug in one path is caught by the other:
    - IPMI parser bug  → Web UI test passes, IPMI test fails
    - Web UI render bug → IPMI test passes, Web UI test fails

Requirements:
    - Chrome browser installed on the test machine
    - ChromeDriver matching the installed Chrome version
      Download from: https://chromedriver.chromium.org/downloads
      Place in project root or add to PATH
    - BMC web UI reachable at https://<bmc-ip>
    - BMC uses a self-signed TLS certificate (handled via --ignore-certificate-errors)

Additional CLI flags used by these tests:
    --bmc-url:         Base URL for BMC web UI (default: https://<bmc-ip>)
    --chromedriver:    Path to ChromeDriver binary (default: ./chromedriver)

Run with:
    pytest src/tests/test_bmc_webui.py --integration \\
           --bmc-ip 192.168.1.100 \\
           --bmc-user admin \\
           --bmc-password yourpassword \\
           --chromedriver ./chromedriver
"""

import pytest
from src.protocol.fru_validator import FruDevice, FruWebUiVerifier
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Additional CLI options for Web UI tests
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    """
    Additional Web UI specific options.
    These extend the options already registered in conftest.py.
    """
    try:
        parser.addoption(
            '--bmc-url',
            default = '',
            help    = 'BMC web UI base URL '
                      '(default: https://<bmc-ip>)',
        )
        parser.addoption(
            '--chromedriver',
            default = './chromedriver',
            help    = 'Path to ChromeDriver binary '
                      '(default: ./chromedriver)',
        )
    except ValueError:
        # options already registered — ignore
        pass


# ---------------------------------------------------------------------------
# Web UI session fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def webui_verifier(request, bmc_credentials):
    """
    Create a FruWebUiVerifier for the test module.

    Module-scoped: one Chrome session shared across all Web UI tests
    in this file. This avoids repeated browser launch/login overhead.

    The FruWebUiVerifier is used as a context manager — it launches
    Chrome on __enter__ and closes it on __exit__, guaranteeing the
    browser is always closed even if a test raises an exception.
    """
    bmc_url = request.config.getoption('--bmc-url')
    if not bmc_url:
        bmc_url = f"https://{bmc_credentials['ip']}"

    chromedriver = request.config.getoption('--chromedriver')

    verifier = FruWebUiVerifier(
        bmc_url      = bmc_url,
        username     = bmc_credentials['user'],
        password     = bmc_credentials['password'],
        chromedriver = chromedriver,
    )

    # launch browser and log in once for all tests in this module
    try:
        verifier._launch_browser()
    except RuntimeError as exc:
        pytest.skip(
            f'Cannot launch Chrome for Web UI tests: {exc}. '
            f'Ensure Chrome and ChromeDriver are installed and '
            f'ChromeDriver version matches Chrome.'
        )

    yield verifier

    # close browser after all tests in this module complete
    verifier._close_browser()


# ---------------------------------------------------------------------------
# Platform FRU device definitions (Web UI path)
# The web_device_name must match the label in the BMC web UI FRU dropdown.
# Replace product strings with actual values for your platform.
# ---------------------------------------------------------------------------

FRU_FRONT_PANEL_WEBUI = FruDevice(
    device_id        = 1,
    expected_product = 'Generic-Server-FP',   # replace with actual value
    expected_chassis = 'Rack Mount Chassis',
    web_device_name  = 'FP_FRU',             # must match web UI dropdown
)

FRU_MOTHERBOARD_WEBUI = FruDevice(
    device_id        = 0,
    expected_product = 'Generic-Server-MB',   # replace with actual value
    expected_chassis = 'Rack Mount Chassis',
    web_device_name  = 'MB_FRU',
)


# ---------------------------------------------------------------------------
# TC_BMC_2_0400 — Front panel FRU via Web UI
# ---------------------------------------------------------------------------

def test_tc_bmc_2_0400_front_panel_fru_webui(
        webui_verifier, test_config):
    """
    TC_BMC_2_0400 — Verify front panel FRU via BMC web UI.

    Navigates to the FRU page in the BMC web UI, selects
    FRU device 1 (front panel board) from the dropdown,
    and verifies three fields in the rendered page:
        - FRU Device Name (shown in dropdown — 'FP_FRU')
        - Chassis Type    ('Rack Mount Chassis')
        - Board Product Name ('Generic-Server-FP')

    Note: web UI labels 'Board Product Name' — IPMI labels 'Board Product'
    Both refer to the same EEPROM field. This naming difference is
    exactly what the dual-path test is designed to catch.

    Pass condition: all three fields found with expected values.

    Failure diagnosis:
        FRU Device Name mismatch: web UI dropdown label differs from spec
        Chassis Type mismatch: EEPROM content or web UI parse error
        Board Product Name mismatch: wrong board or EEPROM corrupt
        Selenium error: web UI not reachable or page structure changed
    """
    is_authorized('TC_BMC_2_0400', test_config)

    result = webui_verifier.verify(
        FRU_FRONT_PANEL_WEBUI,
        board_label = 'FP_WebUI',
    )

    assert result.result_code == 0, (
        f'TC_BMC_2_0400 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_2_0401 — Motherboard FRU via Web UI
# ---------------------------------------------------------------------------

def test_tc_bmc_2_0401_motherboard_fru_webui(
        webui_verifier, test_config):
    """
    TC_BMC_2_0401 — Verify motherboard FRU via BMC web UI.

    Navigates to FRU page, selects FRU device 0 (motherboard),
    and verifies three fields:
        - FRU Device Name ('MB_FRU')
        - Chassis Type    ('Rack Mount Chassis')
        - Board Product Name ('Generic-Server-MB')

    This test and TC_BMC_0_0401 (IPMI path) read from the same
    physical EEPROM chip on the motherboard. Both must pass.

    If TC_BMC_0_0401 passes but this test fails:
        → Web UI rendering bug — BMC web firmware is parsing
          or displaying the FRU EEPROM incorrectly.

    If this test passes but TC_BMC_0_0401 fails:
        → IPMI FRU parser bug — ipmitool or BMC IPMI handler
          is not correctly processing the raw FRU read command.
    """
    is_authorized('TC_BMC_2_0401', test_config)

    result = webui_verifier.verify(
        FRU_MOTHERBOARD_WEBUI,
        board_label = 'MB_WebUI',
    )

    assert result.result_code == 0, (
        f'TC_BMC_2_0401 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_2_0402 — UBB FRU via Web UI
# ---------------------------------------------------------------------------

def test_tc_bmc_2_0402_ubb_fru_webui(
        webui_verifier, test_config):
    """
    TC_BMC_2_0402 — Verify UBB FRU via BMC web UI.

    Reads FRU device 34 (0x22) from the web UI FRU page.
    Verifies FRU Device Name, Chassis Type, and Board Product Name.

    Complements TC_BMC_0_0402 (IPMI path) — both read the same
    UBB EEPROM through independent BMC firmware code paths.
    """
    is_authorized('TC_BMC_2_0402', test_config)

    FRU_UBB_WEBUI = FruDevice(
        device_id        = 0x22,
        expected_product = 'Generic-Server-UBB',   # replace with actual
        expected_chassis = 'Rack Mount Chassis',
        web_device_name  = 'UBB_FRU',
    )

    result = webui_verifier.verify(
        FRU_UBB_WEBUI,
        board_label = 'UBB_WebUI',
    )

    assert result.result_code == 0, (
        f'TC_BMC_2_0402 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_2_0403 — IO board NIC FRU via Web UI
# ---------------------------------------------------------------------------

def test_tc_bmc_2_0403_io_nic_fru_webui(
        webui_verifier, sys_conf, test_config):
    """
    TC_BMC_2_0403 — Verify IO board NIC FRU entries via BMC web UI.

    The web UI FRU page allows selecting individual NIC FRU device IDs.
    This test verifies each installed IOBU/IOBD NIC card by selecting
    its FRU device in the web UI dropdown and checking the page content.

    FRU device IDs for IOBU/IOBD NIC slots:
        IOBU slot 1 → device 0x0C,  IOBD slot 1 → device 0x15
        IOBU slot 2 → device 0x0D,  IOBD slot 2 → device 0x13
        ... (non-sequential per BMC firmware FRU repository assignment)

    Only installed slots (per sys_conf.json) are verified.
    """
    is_authorized('TC_BMC_2_0403', test_config)

    any_io_nic = any(
        sys_conf.get(f'IOBU_OCP{i}', 0) or
        sys_conf.get(f'IOBD_OCP{i}', 0)
        for i in range(1, 6)
    )
    if not any_io_nic:
        pytest.skip(
            'No IO board NIC cards present per sys_conf.json.'
        )

    from src.protocol.fru_validator import (
        FRU_DEV_IDS_IOBU, FRU_DEV_IDS_IOBD, VALID_NIC_MANUFACTURERS
    )
    from src.protocol.sdr_parser import SdrCheckResult
    overall = SdrCheckResult()

    # check IOBU slots
    for slot in range(1, 6):
        if not sys_conf.get(f'IOBU_OCP{slot}', 0):
            continue
        dev = FruDevice(
            device_id        = FRU_DEV_IDS_IOBU[slot],
            expected_product = next(iter(VALID_NIC_MANUFACTURERS)),
            expected_chassis = 'Rack Mount Chassis',
            web_device_name  = f'IOBU_OCP{slot}_FRU',
        )
        r = webui_verifier.verify(dev, board_label=f'IOBU_OCP{slot}_WebUI')
        overall.passed   += r.passed
        overall.failures += r.failures

    # check IOBD slots
    for slot in range(1, 6):
        if not sys_conf.get(f'IOBD_OCP{slot}', 0):
            continue
        dev = FruDevice(
            device_id        = FRU_DEV_IDS_IOBD[slot],
            expected_product = next(iter(VALID_NIC_MANUFACTURERS)),
            expected_chassis = 'Rack Mount Chassis',
            web_device_name  = f'IOBD_OCP{slot}_FRU',
        )
        r = webui_verifier.verify(dev, board_label=f'IOBD_OCP{slot}_WebUI')
        overall.passed   += r.passed
        overall.failures += r.failures

    assert overall.result_code == 0, (
        f'TC_BMC_2_0403 FAILED:\n{overall.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_2_0404 — MB OCP NIC FRU via Web UI
# ---------------------------------------------------------------------------

def test_tc_bmc_2_0404_mb_ocp_nic_fru_webui(
        webui_verifier, sys_conf, test_config):
    """
    TC_BMC_2_0404 — Verify MB OCP NIC FRU entries via BMC web UI.

    MB_OCP1 → FRU device 0x0A
    MB_OCP2 → FRU device 0x0B

    Complements TC_BMC_0_0404 (IPMI path).
    Only installed slots (per sys_conf.json) are verified.
    """
    is_authorized('TC_BMC_2_0404', test_config)

    any_mb_ocp = any(
        sys_conf.get(f'MB_OCP{i}', 0) for i in range(1, 3)
    )
    if not any_mb_ocp:
        pytest.skip('No MB OCP NIC cards present per sys_conf.json.')

    from src.protocol.fru_validator import VALID_NIC_MANUFACTURERS
    from src.protocol.sdr_parser import SdrCheckResult
    overall = SdrCheckResult()

    mb_ocp_devices = {
        'MB_OCP1': (0x0A, 'MB_OCP1_FRU'),
        'MB_OCP2': (0x0B, 'MB_OCP2_FRU'),
    }

    for slot_key, (device_id, web_name) in mb_ocp_devices.items():
        if not sys_conf.get(slot_key, 0):
            continue
        dev = FruDevice(
            device_id        = device_id,
            expected_product = next(iter(VALID_NIC_MANUFACTURERS)),
            expected_chassis = 'Rack Mount Chassis',
            web_device_name  = web_name,
        )
        r = webui_verifier.verify(
            dev, board_label=f'{slot_key}_WebUI'
        )
        overall.passed   += r.passed
        overall.failures += r.failures

    assert overall.result_code == 0, (
        f'TC_BMC_2_0404 FAILED:\n{overall.summary()}'
    )
