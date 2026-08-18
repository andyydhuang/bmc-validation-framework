"""
src/tests/test_bmc_fru.py

Hardware integration tests for FRU inventory validation.

TC IDs covered:
    TC_BMC_0_0400 — Front panel FRU (IPMI path)
    TC_BMC_0_0401 — Motherboard FRU (IPMI path)
    TC_BMC_0_0402 — UBB FRU (IPMI path)
    TC_BMC_0_0403 — IO board NIC FRU (IPMI path)
    TC_BMC_0_0404 — MB OCP NIC FRU (IPMI path)

What these tests validate:
    FRU EEPROM content is read via 'ipmitool fru print <device_id>'
    and verified against expected field values.

    Two fields checked per FRU device:
        Chassis Type  — must contain expected chassis string
        Board Product — must contain expected product name string

    NIC FRU tests check manufacturer name (Intel/Mellanox/NVIDIA)
    instead of product name because different NIC vendors may be
    installed in any OCP slot.

    The IOBU/IOBD bug fix is active in verify_io_nic_fru() —
    the second loop checks IOBD_OCP presence, not IOBU_OCP presence.

Run with:
    pytest src/tests/test_bmc_fru.py --integration
           --bmc-ip <ip> --bmc-user admin --bmc-password <pw>
           --sys-conf config/sys_conf.json
"""

import pytest
from src.protocol.fru_validator import (
    FruDevice,
    FruIpmiVerifier,
    verify_io_nic_fru,
    VALID_NIC_MANUFACTURERS,
)
from src.protocol.sdr_parser import SdrCheckResult
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Platform FRU device definitions
# Replace expected_product strings with actual values for your platform.
# ---------------------------------------------------------------------------

FRU_FRONT_PANEL = FruDevice(
    device_id        = 1,
    expected_product = 'Generic-Server-FP',   # replace with actual value
    expected_chassis = 'Rack Mount Chassis',
    web_device_name  = 'FP_FRU',
)

FRU_MOTHERBOARD = FruDevice(
    device_id        = 0,
    expected_product = 'Generic-Server-MB',   # replace with actual value
    expected_chassis = 'Rack Mount Chassis',
    web_device_name  = 'MB_FRU',
)

# MB OCP NIC slot FRU device IDs
# Values are platform-specific — replace if your platform differs
MB_OCP_FRU_IDS = {
    'MB_OCP1': 0x0A,
    'MB_OCP2': 0x0B,
}


# ---------------------------------------------------------------------------
# TC_BMC_0_0400 — Front panel FRU
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0400_front_panel_fru(
        fru_verifier, test_config):
    """
    TC_BMC_0_0400 — Verify front panel FRU via IPMI fru print.

    Reads FRU device ID 1 (front panel board EEPROM).
    Checks Chassis Type and Board Product fields.

    Failure diagnosis:
        Empty output: FRU EEPROM not accessible or blank
        Field mismatch: wrong board installed or EEPROM corrupt
    """
    is_authorized('TC_BMC_0_0400', test_config)

    result = fru_verifier.verify(FRU_FRONT_PANEL, board_label='FP')

    assert result.result_code == 0, (
        f'TC_BMC_0_0400 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0401 — Motherboard FRU
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0401_motherboard_fru(
        fru_verifier, test_config):
    """
    TC_BMC_0_0401 — Verify motherboard FRU via IPMI fru print.

    Reads FRU device ID 0 (motherboard EEPROM).
    Checks Chassis Type and Board Product fields.
    """
    is_authorized('TC_BMC_0_0401', test_config)

    result = fru_verifier.verify(FRU_MOTHERBOARD, board_label='MB')

    assert result.result_code == 0, (
        f'TC_BMC_0_0401 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0402 — UBB FRU (IPMI path)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0402_ubb_fru(
        fru_verifier, test_config):
    """
    TC_BMC_0_0402 — Verify UBB (Universal Baseboard) FRU via IPMI fru print.

    Reads FRU device ID 34 (0x22) — the UBB board EEPROM.
    Checks Chassis Type and Board Product fields.

    The UBB is a daughterboard that carries OAM accelerator modules.
    Its FRU EEPROM is on a separate I2C bus from the main motherboard.

    Failure diagnosis:
        Empty output: UBB FRU EEPROM not accessible
                      (check I2C bus 9 connectivity)
        Field mismatch: wrong UBB board installed or EEPROM corrupt
    """
    is_authorized('TC_BMC_0_0402', test_config)

    FRU_UBB = FruDevice(
        device_id        = 0x22,              # 34 decimal
        expected_product = 'Generic-Server-UBB',   # replace with actual
        expected_chassis = 'Rack Mount Chassis',
        web_device_name  = 'UBB_FRU',
    )

    result = fru_verifier.verify(FRU_UBB, board_label='UBB')

    assert result.result_code == 0, (
        f'TC_BMC_0_0402 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0403 — IO board NIC FRU (IOBU and IOBD slots)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0403_io_nic_fru(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0403 — Verify FRU for all populated IO board NIC slots.

    Checks IOBU_OCP1-5 and IOBD_OCP1-5 slots per sys_conf.json.
    Only installed slots are verified — absent slots are skipped.

    Uses verify_io_nic_fru() which contains the IOBU/IOBD prefix
    bug fix: the IOBD loop checks sys_conf[IOBD_OCP{n}] not
    sys_conf[IOBU_OCP{n}]. See fru_validator.py for details.

    NIC manufacturer check: Board Mfg and Board Product fields
    must contain one of VALID_NIC_MANUFACTURERS (Intel/Mellanox/NVIDIA).
    """
    is_authorized('TC_BMC_0_0403', test_config)

    any_io_nic = any(
        sys_conf.get(f'IOBU_OCP{i}', 0) or
        sys_conf.get(f'IOBD_OCP{i}', 0)
        for i in range(1, 6)
    )
    if not any_io_nic:
        pytest.skip(
            'No IO board NIC cards present per sys_conf.json. '
            'Set IOBU_OCP1-5 or IOBD_OCP1-5 to 1 in sys_conf.json '
            'to enable this test.'
        )

    result = verify_io_nic_fru(ipmi_client, sys_conf)

    assert result.result_code == 0, (
        f'TC_BMC_0_0403 FAILED:\n{result.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0404 — MB OCP NIC FRU
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0404_mb_ocp_nic_fru(
        fru_verifier, sys_conf, test_config):
    """
    TC_BMC_0_0404 — Verify FRU for MB-mounted OCP NIC slots.

    MB_OCP1 = FRU device 0x0A, MB_OCP2 = FRU device 0x0B.
    Only slots marked present in sys_conf are checked.

    Same NIC manufacturer check as TC_BMC_0_0403.
    """
    is_authorized('TC_BMC_0_0404', test_config)

    any_mb_ocp = any(
        sys_conf.get(key, 0) for key in MB_OCP_FRU_IDS
    )
    if not any_mb_ocp:
        pytest.skip(
            'No MB OCP NIC cards present per sys_conf.json. '
            'Set MB_OCP1 or MB_OCP2 to 1 to enable this test.'
        )

    overall = SdrCheckResult()

    for slot_key, device_id in MB_OCP_FRU_IDS.items():
        if not sys_conf.get(slot_key, 0):
            continue

        slot_result = fru_verifier.verify_nic(
            device_id           = device_id,
            valid_manufacturers = VALID_NIC_MANUFACTURERS,
            board_label         = slot_key,
        )
        overall.passed   += slot_result.passed
        overall.failures += slot_result.failures

    assert overall.result_code == 0, (
        f'TC_BMC_0_0404 FAILED:\n{overall.summary()}'
    )
