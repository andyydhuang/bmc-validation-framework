"""
FRU inventory validation via IPMI and Web UI.

FRU EEPROMs hold board identity data (chassis type, board product, manufacturer).
This module validates that data through two paths: ipmitool fru print and
Selenium against the BMC web interface. Both paths read the same physical EEPROM
but through separate BMC firmware handlers, so a bug in one is caught by the other.

verify_io_nic_fru() handles IOBU and IOBD slots separately with injectable
prefixes — both loops must check their own sys_conf key independently.
VALID_NIC_MANUFACTURERS is a set rather than a hardcoded string so it works
regardless of which vendor is installed in a given slot.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FRU device mapping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FruDevice:
    """
    Maps a logical board name to its IPMI FRU device ID and expected fields.

    device_id:          integer FRU device ID passed to 'fru print'
    expected_product:   expected Board Product field value
    expected_chassis:   expected Chassis Type field value
    web_device_name:    name shown in BMC web UI FRU device dropdown
                        (only used for Web UI path verification)
    """
    device_id:        int
    expected_product: str
    expected_chassis: str
    web_device_name:  str = ''


# ---------------------------------------------------------------------------
# Check result accumulator (mirrors SdrCheckResult pattern)
# ---------------------------------------------------------------------------

@dataclass
class FruCheckResult:
    """Accumulates named pass/fail outcomes for one FRU verification call."""
    board:    str
    passed:   int       = 0
    failures: List[str] = field(default_factory=list)

    def fail(self, message: str):
        self.failures.append(message)
        logger.error('  **FAIL** [%s] %s', self.board, message)

    def ok(self, message: str):
        self.passed += 1
        logger.debug('  ==PASS== [%s] %s', self.board, message)

    @property
    def result_code(self) -> int:
        return -len(self.failures)

    def summary(self) -> str:
        lines = [
            f'[{self.board}] {self.passed} passed, '
            f'{len(self.failures)} failed'
        ]
        for f in self.failures:
            lines.append(f'  FAIL: {f}')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# IPMI path verification
# ---------------------------------------------------------------------------

class FruIpmiVerifier:
    """
    Verifies FRU field content through the IPMI fru print command.

    Usage:
        verifier = FruIpmiVerifier(ipmi_client)
        result = verifier.verify(fru_device, board_label='MB')
        assert result.result_code == 0, result.summary()
    """

    def __init__(self, ipmi_client):
        """
        Args:
            ipmi_client: IpmiClient instance or MockBmcClient for testing
        """
        self._ipmi = ipmi_client

    def verify(self,
               device:      'FruDevice',
               board_label: str = '') -> FruCheckResult:
        """
        Read FRU data via IPMI and verify chassis type + board product.

        The verification checks two fields:
            - Chassis Type:  must contain device.expected_chassis
            - Board Product: must contain device.expected_product

        Both checks use substring matching (in operator) so minor
        formatting differences between BMC firmware versions do not
        cause false failures.

        Args:
            device:      FruDevice with device_id and expected field values
            board_label: human-readable label for log messages e.g. 'MB'

        Returns:
            FruCheckResult with individual field pass/fail records
        """
        label  = board_label or f'FRU_0x{device.device_id:02X}'
        result = FruCheckResult(board=label)

        device_id_str = hex(device.device_id)
        output = self._ipmi.run('fru', 'print', device_id_str)

        if not output.strip():
            result.fail(
                f'fru print {device_id_str} returned empty output. '
                f'Device may not be installed or FRU EEPROM is blank.'
            )
            return result

        chassis_found = False
        product_found = False

        for line in output.splitlines():
            if 'Chassis Type' in line:
                if device.expected_chassis in line:
                    result.ok(
                        f'Chassis Type: found "{device.expected_chassis}"'
                    )
                    chassis_found = True
                else:
                    result.fail(
                        f'Chassis Type mismatch: '
                        f'expected "{device.expected_chassis}" in [{line.strip()}]'
                    )

            if 'Board Product' in line:
                if device.expected_product in line:
                    result.ok(
                        f'Board Product: found "{device.expected_product}"'
                    )
                    product_found = True
                else:
                    result.fail(
                        f'Board Product mismatch: '
                        f'expected "{device.expected_product}" '
                        f'in [{line.strip()}]'
                    )

        if not chassis_found and not any('Chassis Type' in f
                                         for f in result.failures):
            result.fail(
                f'Chassis Type field not found in fru print output. '
                f'Check FRU EEPROM content or ipmitool parsing.'
            )

        if not product_found and not any('Board Product' in f
                                         for f in result.failures):
            result.fail(
                f'Board Product field not found in fru print output.'
            )

        logger.info('FRU IPMI check: %s', result.summary())
        return result

    def verify_nic(self,
                   device_id:             int,
                   valid_manufacturers:   Set[str],
                   board_label:           str = '') -> FruCheckResult:
        """
        Verify NIC card FRU — checks manufacturer rather than product name.

        NIC cards from different vendors (e.g. Intel, Mellanox/NVIDIA)
        may be installed in any OCP slot. The test accepts any manufacturer
        in valid_manufacturers rather than requiring a specific product name.

        Checks both 'Board Mfg' and 'Board Product' fields for the
        manufacturer name — different NIC vendors encode identity
        differently across these two fields.

        Args:
            device_id:           IPMI FRU device ID integer
            valid_manufacturers: set of acceptable manufacturer strings
                                 e.g. {'Intel', 'Mellanox', 'NVIDIA'}
            board_label:         label for log messages

        Returns:
            FruCheckResult — expects score of 2 (both fields verified)
        """
        label  = board_label or f'NIC_FRU_0x{device_id:02X}'
        result = FruCheckResult(board=label)

        output = self._ipmi.run('fru', 'print', hex(device_id))

        if not output.strip():
            result.fail(
                f'fru print {hex(device_id)} returned empty output. '
                f'NIC card may not be installed.'
            )
            return result

        mfg_field_ok     = False
        product_field_ok = False

        for line in output.splitlines():
            if 'Board Mfg' in line and 'Date' not in line:
                if any(mfr in line for mfr in valid_manufacturers):
                    result.ok(f'Board Mfg: found valid manufacturer')
                    mfg_field_ok = True
                else:
                    result.fail(
                        f'Board Mfg: none of {valid_manufacturers} '
                        f'found in [{line.strip()}]'
                    )

            if 'Board Product' in line:
                if any(mfr in line for mfr in valid_manufacturers):
                    result.ok(f'Board Product: contains manufacturer name')
                    product_field_ok = True
                else:
                    result.fail(
                        f'Board Product: none of {valid_manufacturers} '
                        f'found in [{line.strip()}]'
                    )

        if not mfg_field_ok and not any('Board Mfg' in f
                                        for f in result.failures):
            result.fail('Board Mfg field not found in fru print output.')

        if not product_field_ok and not any('Board Product' in f
                                            for f in result.failures):
            result.fail('Board Product field not found in fru print output.')

        logger.info('NIC FRU IPMI check: %s', result.summary())
        return result


# ---------------------------------------------------------------------------
# IO NIC FRU validation — IOBU and IOBD each have separate prefixes
# ---------------------------------------------------------------------------

# FRU device ID lookup tables for IO board NIC slots
# Index 0 is unused (slots are 1-based, index directly = slot number)
# Non-sequential ordering matches the BMC's internal FRU repository assignment
FRU_DEV_IDS_IOBU: List[int] = [-1, 0x0C, 0x0D, 0x0E, 0x10, 0x0F]
FRU_DEV_IDS_IOBD: List[int] = [-1, 0x15, 0x13, 0x12, 0x11, 0x14]

VALID_NIC_MANUFACTURERS: Set[str] = {'Intel', 'Mellanox', 'NVIDIA'}


def verify_io_nic_fru(ipmi_client,
                      sys_conf:    dict,
                      iobu_prefix: str = 'IOBU_OCP',
                      iobd_prefix: str = 'IOBD_OCP') -> FruCheckResult:
    """
    Verify FRU for all populated IO board NIC slots.

    Each loop checks its own prefix independently: the IOBU loop
    uses iobu_prefix, the IOBD loop uses iobd_prefix.

    Args:
        ipmi_client:  IpmiClient or MockBmcClient
        sys_conf:     hardware presence bitmap from sys_conf.json
        iobu_prefix:  sys_conf key prefix for IOBU slots (default 'IOBU_OCP')
        iobd_prefix:  sys_conf key prefix for IOBD slots (default 'IOBD_OCP')

    Returns:
        FruCheckResult accumulating results for all verified NIC slots
    """
    result    = FruCheckResult(board='IO_NIC_FRU')
    verifier  = FruIpmiVerifier(ipmi_client)

    # ── IOBU slots 1-5 ────────────────────────────────────────────────
    for slot in range(1, 6):
        config_key = f'{iobu_prefix}{slot}'
        configured = sys_conf.get(config_key, 0)

        if not configured:
            logger.debug('Skipping %s — not present in sys_conf', config_key)
            continue

        device_id  = FRU_DEV_IDS_IOBU[slot]
        slot_label = f'IOBU_OCP{slot}'

        slot_result = verifier.verify_nic(
            device_id           = device_id,
            valid_manufacturers = VALID_NIC_MANUFACTURERS,
            board_label         = slot_label,
        )
        result.passed   += slot_result.passed
        result.failures += slot_result.failures

    # ── IOBD slots 1-5 ────────────────────────────────────────────────
    # IOBD loop uses iobd_prefix so absent IOBU slots do not
    # cause present IOBD slots to be skipped.
    for slot in range(1, 6):
        config_key = f'{iobd_prefix}{slot}'
        configured = sys_conf.get(config_key, 0)

        if not configured:
            logger.debug('Skipping %s — not present in sys_conf', config_key)
            continue

        device_id  = FRU_DEV_IDS_IOBD[slot]
        slot_label = f'IOBD_OCP{slot}'

        slot_result = verifier.verify_nic(
            device_id           = device_id,
            valid_manufacturers = VALID_NIC_MANUFACTURERS,
            board_label         = slot_label,
        )
        result.passed   += slot_result.passed
        result.failures += slot_result.failures

    logger.info('IO NIC FRU overall: %s', result.summary())
    return result


# ---------------------------------------------------------------------------
# Web UI path verification
# ---------------------------------------------------------------------------

class FruWebUiVerifier:
    """
    Verifies FRU field content through the BMC web UI via Selenium.

    The web UI renders FRU data through a completely separate BMC firmware
    code path from the IPMI fru print command. Running both verifiers
    against the same physical FRU EEPROM catches:
        - IPMI parser bugs (web UI test passes, IPMI test fails)
        - Web UI rendering bugs (IPMI test passes, web UI test fails)

    Requires: selenium, ChromeDriver matching installed Chrome version

    Usage:
        with FruWebUiVerifier(bmc_url, username, password) as verifier:
            result = verifier.verify(device, board_label='MB')
    """

    def __init__(self, bmc_url:  str,
                 username:       str,
                 password:       str,
                 chromedriver:   str = './chromedriver'):
        """
        Args:
            bmc_url:      BMC web interface base URL e.g. 'https://192.168.1.100'
            username:     BMC web UI username
            password:     BMC web UI password
            chromedriver: path to ChromeDriver binary
        """
        self._url    = bmc_url
        self._user   = username
        self._pass   = password
        self._driver_path = chromedriver
        self._driver = None

    def __enter__(self):
        self._launch_browser()
        return self

    def __exit__(self, *args):
        self._close_browser()

    def _launch_browser(self):
        """Launch Chrome with certificate errors suppressed for self-signed BMC TLS."""
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument('--ignore-certificate-errors')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            # suppress certificate errors — BMC uses self-signed TLS cert
            # that Chrome would normally refuse

            self._driver = webdriver.Chrome(
                self._driver_path, options=options
            )
            self._driver.get(self._url)
            self._driver.maximize_window()

            import time
            time.sleep(10)
            # allow React/Angular SPA to fully render login page
            # before Selenium attempts to find form elements

            self._login()

        except ImportError:
            raise RuntimeError(
                'selenium is required for Web UI tests. '
                'Install with: pip install selenium'
            )

    def _login(self):
        """Perform BMC web UI login."""
        import time
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select

        # wait for login page to render (up to 10 seconds)
        for _ in range(10):
            try:
                lang_select = Select(
                    self._driver.find_element(By.ID, 'select_lang')
                )
                lang_select.select_by_value('en-us')
                break
            except Exception:
                time.sleep(1)

        self._driver.find_element(By.ID, 'userid').send_keys(self._user)
        self._driver.find_element(By.ID, 'password').send_keys(self._pass)
        self._driver.find_element(By.ID, 'btn-login').click()
        time.sleep(3)

    def _close_browser(self):
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def verify(self,
               device:      'FruDevice',
               board_label: str = '') -> FruCheckResult:
        """
        Navigate to FRU page, select device, verify three fields.

        Web UI checks three fields (vs IPMI which checks two):
            - FRU Device Name  (unique to web UI — identifies device in dropdown)
            - Chassis Type
            - Board Product Name  (note: web UI adds 'Name' suffix vs IPMI)

        Args:
            device:      FruDevice with device_id, expected_product,
                         expected_chassis, and web_device_name
            board_label: label for log messages

        Returns:
            FruCheckResult — expects score of 3 (all three fields)
        """
        import time
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import Select

        label  = board_label or f'WebUI_FRU_0x{device.device_id:02X}'
        result = FruCheckResult(board=label)

        if not self._driver:
            result.fail('Browser not initialized. Use as context manager.')
            return result

        try:
            # navigate to FRU tab in BMC single-page app
            fru_tab = self._driver.find_element(
                By.XPATH, '//a[@href="#fru"]'
            )
            fru_tab.click()
            time.sleep(3)

            # select the target FRU device from dropdown
            fru_select = Select(
                self._driver.find_element(By.ID, 'fru_device_id')
            )
            time.sleep(0.5)
            fru_select.select_by_value(str(device.device_id))
            time.sleep(0.5)

            # get all visible text on the page
            page_text = self._driver.find_element(
                By.XPATH, '/html/body'
            ).text

        except Exception as exc:
            result.fail(
                f'Selenium error navigating to FRU page: {exc}. '
                f'Check BMC web UI is accessible and ChromeDriver version '
                f'matches installed Chrome.'
            )
            return result

        # verify three fields in page text
        for line in page_text.splitlines():

            if 'FRU Device Name' in line:
                if device.web_device_name and \
                   device.web_device_name in line:
                    result.ok(
                        f'FRU Device Name: found "{device.web_device_name}"'
                    )
                elif not device.web_device_name:
                    result.ok('FRU Device Name: field present (name not specified)')

            if 'Chassis Type' in line:
                if device.expected_chassis in line:
                    result.ok(
                        f'Chassis Type: found "{device.expected_chassis}"'
                    )
                else:
                    result.fail(
                        f'Chassis Type mismatch: '
                        f'expected "{device.expected_chassis}" '
                        f'in [{line.strip()}]'
                    )

            # web UI label is 'Board Product Name' (not just 'Board Product')
            if 'Board Product Name' in line:
                if device.expected_product in line:
                    result.ok(
                        f'Board Product Name: found "{device.expected_product}"'
                    )
                else:
                    result.fail(
                        f'Board Product Name mismatch: '
                        f'expected "{device.expected_product}" '
                        f'in [{line.strip()}]'
                    )

        logger.info('FRU Web UI check: %s', result.summary())
        return result
