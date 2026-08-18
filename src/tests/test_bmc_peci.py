"""
src/tests/test_bmc_peci.py

Hardware integration tests for PECI thermal protocol and SMLINK/Node Manager.

TC IDs covered:
    TC_BMC_0_0150 — SMLINK Node Manager manufacturer ID
    TC_BMC_0_0160 — PECI Ping CPU0
    TC_BMC_0_0161 — PECI Ping CPU1
    TC_BMC_0_0162 — PECI GetTemp CPU0
    TC_BMC_0_0163 — PECI GetTemp CPU1
    TC_BMC_0_0164 — PECI GetDIB CPU0
    TC_BMC_0_0165 — PECI GetDIB CPU1

These tests require:
    - Real BMC reachable at --bmc-ip
    - Host powered on and BIOS POST complete
    - Run with: pytest src/tests/test_bmc_peci.py --integration

PECI protocol notes:
    PECI is Intel's single-wire serial bus connecting BMC to CPUs.
    All PECI tests require BIOS POST to be complete — the CPU's thermal
    subsystem is not initialized until POST runs.
    CPU PECI addresses: CPU0=0x30, CPU1=0x31 (fixed by Intel spec)

SMLINK protocol notes:
    SMLINK is a dedicated SMBus channel between BMC and PCH.
    Node Manager firmware runs on the PCH at IPMB address 0x2C.
    Access requires ipmitool -b 0x06 -t 0x2c bridge flags.
"""

import pytest
from src.protocol.sdr_parser import SdrCheckResult
from src.transport.peci_client import (
    PeciClient, SmlinkClient,
    PECI_ADDR_CPU0, PECI_ADDR_CPU1, PECI_CC_PASS,
)
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration

# Temperature acceptance range for POST-complete state
# Below -50°C: sensor not initialized (should not happen post-POST)
# Above Tjmax (105°C): thermal emergency
_TEMP_RANGE = (-50.0, 105.0)


# ---------------------------------------------------------------------------
# Precondition helper
# ---------------------------------------------------------------------------

def _require_post_complete(ipmi_client):
    """
    Verify BIOS POST is complete before running PECI tests.
    PECI thermal data is only valid after POST initializes
    the CPU thermal management subsystem.

    Skips the test (not fails) if POST is not complete —
    this is a precondition failure, not a hardware defect.
    """
    try:
        response = ipmi_client.run_raw(0x00, 0x00, 0x00)  # replace with platform OEM NetFn/Cmd/param for POST complete check
        # platform OEM command: read BIOS POST complete status register
        # 0x00 = POST complete, non-zero = still in POST
        if len(response) > 0 and response[0] == 0x00:
            return  # POST complete — proceed
    except Exception:
        pass

    pytest.skip(
        'BIOS POST not complete. PECI thermal sensors are only '
        'valid after POST initializes the CPU thermal subsystem. '
        'Ensure host is fully booted before running PECI tests.'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0150 — SMLINK Node Manager
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0150_smlink_node_manager(
        smlink_client, ipmi_client, test_config):
    """
    TC_BMC_0_0150 — Verify Intel Node Manager is alive on SMLink bus.

    Protocol path:
        ipmitool -b 0x06 -t 0x2c mc info
        → IPMI LAN → BMC → SMLink bus 6 → PCH Node Manager (0x2C)

    Pass condition:
        Manufacturer Name contains 'Intel Corporation'

    Failure diagnosis:
        Empty response: PCH not powered or SMLink bus disconnected
        Wrong manufacturer: unexpected firmware on 0x2C address
    """
    is_authorized('TC_BMC_0_0150', test_config)

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    smlink_client.verify_node_manager(check)

    assert check.result_code == 0, (
        f'TC_BMC_0_0150 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0160 — PECI Ping CPU0
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0160_peci_ping_cpu0(
        peci_client, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0160 — Confirm CPU0 is alive on PECI bus.

    Ping uses PECI command code 0x01 with WrLen=0 RdLen=0.
    No data transfer — just an ACK pulse from the CPU.

    Pass condition:
        BMC successfully forwarded the Ping to CPU0 address 0x30
        without returning an error response.

    Failure diagnosis:
        No response: PECI bus disconnected, CPU not powered,
                     or BMC PECI controller not initialized.
    """
    is_authorized('TC_BMC_0_0160', test_config)

    if not sys_conf.get('CPU0', 0):
        pytest.skip('CPU0 not present per sys_conf.json')

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    alive = peci_client.ping(PECI_ADDR_CPU0)

    if alive:
        check.ok('CPU0 PECI Ping: responded at address 0x30')
    else:
        check.fail(
            'CPU0 PECI Ping failed at address 0x30. '
            'Check PECI bus connectivity and CPU power state.'
        )

    assert check.result_code == 0, (
        f'TC_BMC_0_0160 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0161 — PECI Ping CPU1
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0161_peci_ping_cpu1(
        peci_client, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0161 — Confirm CPU1 is alive on PECI bus at address 0x31.
    """
    is_authorized('TC_BMC_0_0161', test_config)

    if not sys_conf.get('CPU1', 0):
        pytest.skip('CPU1 not present per sys_conf.json')

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    alive = peci_client.ping(PECI_ADDR_CPU1)

    if alive:
        check.ok('CPU1 PECI Ping: responded at address 0x31')
    else:
        check.fail(
            'CPU1 PECI Ping failed at address 0x31. '
            'Check PECI bus connectivity and CPU1 power state.'
        )

    assert check.result_code == 0, (
        f'TC_BMC_0_0161 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0162 — PECI GetTemp CPU0
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0162_peci_gettemp_cpu0(
        peci_client, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0162 — Read CPU0 temperature via PECI GetTemp.

    Response format: [completion_code, LSB, MSB]
    Temperature formula: Tjmax + (signed_16bit / 64.0) degrees C

    Pass conditions:
        completion_code == 0x40 (PECI pass)
        temperature within (-50.0, 105.0) degrees C

    Failure diagnosis:
        completion 0x80: CPU in deep C-state (power management issue)
        completion 0x90: PECI bus timeout or error
        temp < -50°C:   thermal subsystem not initialized
        temp > Tjmax:   CPU in thermal emergency
    """
    is_authorized('TC_BMC_0_0162', test_config)

    if not sys_conf.get('CPU0', 0):
        pytest.skip('CPU0 not present per sys_conf.json')

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    peci_client.verify_cpu(
        cpu_index  = 0,
        check      = check,
        temp_range = _TEMP_RANGE,
    )

    assert check.result_code == 0, (
        f'TC_BMC_0_0162 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0163 — PECI GetTemp CPU1
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0163_peci_gettemp_cpu1(
        peci_client, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0163 — Read CPU1 temperature via PECI GetTemp.

    Same validation as CPU0 — applied to PECI address 0x31.
    """
    is_authorized('TC_BMC_0_0163', test_config)

    if not sys_conf.get('CPU1', 0):
        pytest.skip('CPU1 not present per sys_conf.json')

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    peci_client.verify_cpu(
        cpu_index  = 1,
        check      = check,
        temp_range = _TEMP_RANGE,
    )

    assert check.result_code == 0, (
        f'TC_BMC_0_0163 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0164 — PECI GetDIB CPU0
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0164_peci_getdib_cpu0(
        peci_client, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0164 — Read CPU0 Device Identification Block via PECI GetDIB.

    DIB response: 8 bytes after completion code.
    Byte 0 (dev_info): bits[7:4] = PECI revision (0x0=2.x, 0x1=3.1)
    Byte 1 (proc_num): number of processors at this PECI address

    Pass conditions:
        Response is 9+ bytes (completion + 8 DIB bytes)
        PECI revision is 0x0 or 0x1
        No ValueError on parse

    Failure diagnosis:
        Short response: PECI transaction failed
        Unknown revision: unexpected PECI protocol version
    """
    is_authorized('TC_BMC_0_0164', test_config)

    if not sys_conf.get('CPU0', 0):
        pytest.skip('CPU0 not present per sys_conf.json')

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    dib   = peci_client.get_dib(PECI_ADDR_CPU0)

    if dib is None:
        check.fail(
            'CPU0 PECI GetDIB: read failed or response too short. '
            'Check PECI bus connectivity and POST completion state.'
        )
    elif not dib.is_valid():
        check.fail(
            f'CPU0 DIB: PECI revision 0x{dib.peci_revision:X} unexpected. '
            f'Expected 0x0 (PECI 2.x) or 0x1 (PECI 3.1). '
            f'Raw DIB: {dib.raw_bytes.hex(" ")}'
        )
    else:
        check.ok(f'CPU0 DIB verified: {dib}')

    assert check.result_code == 0, (
        f'TC_BMC_0_0164 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0165 — PECI GetDIB CPU1
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0165_peci_getdib_cpu1(
        peci_client, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0165 — Read CPU1 Device Identification Block via PECI GetDIB.

    Same validation as CPU0 — applied to PECI address 0x31.
    """
    is_authorized('TC_BMC_0_0165', test_config)

    if not sys_conf.get('CPU1', 0):
        pytest.skip('CPU1 not present per sys_conf.json')

    _require_post_complete(ipmi_client)

    check = SdrCheckResult()
    dib   = peci_client.get_dib(PECI_ADDR_CPU1)

    if dib is None:
        check.fail(
            'CPU1 PECI GetDIB: read failed or response too short.'
        )
    elif not dib.is_valid():
        check.fail(
            f'CPU1 DIB: PECI revision 0x{dib.peci_revision:X} unexpected. '
            f'Expected 0x0 or 0x1.'
        )
    else:
        check.ok(f'CPU1 DIB verified: {dib}')

    assert check.result_code == 0, (
        f'TC_BMC_0_0165 FAILED:\n{check.summary()}'
    )
