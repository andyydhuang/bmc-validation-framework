"""
src/tests/test_bmc_jtag.py

Hardware integration tests for JTAG/ASD interface.

TC IDs covered:
    TC_BMC_0_0100 — JTAG CPU0 IDCODE (jtagtest)
    TC_BMC_0_0101 — JTAG CPU1 IDCODE (jtagtest)
    TC_BMC_0_0102 — JTAG CPU0 ASD software path
    TC_BMC_0_0103 — JTAG CPU1 ASD software path
    TC_BMC_0_0104 — JTAG accelerator modules OAM[7:0]
    TC_BMC_0_0105 — JTAG PCH
    TC_BMC_0_0106 — MB MIPI-60 JTAG CPU0 <Fixture>
    TC_BMC_0_0107 — MB MIPI-60 JTAG CPU1 <Fixture>
    TC_BMC_0_0108 — MB MIPI-60 JTAG PCH <Fixture>
    TC_BMC_0_0109 — UBB MIPI-60 JTAG OAM[7:0] <Fixture>

These tests require:
    - Real BMC reachable at --bmc-ip
    - ipmitool installed and on PATH
    - Host powered on and POST complete (for CPU/PCH JTAG)
    - Run with: pytest src/tests/test_bmc_jtag.py --integration
                        --bmc-ip <ip> --bmc-user admin --bmc-password <pw>

What these tests validate:
    The BMC's JTAG master infrastructure can route to each target silicon
    via the hardware MUX and read the mandatory IEEE 1149.1 IDCODE register.

    A wrong IDCODE, all-zeros, or all-ones response indicates:
        - Wrong silicon stepping installed
        - Broken JTAG scan chain (trace open, connector missing)
        - MUX routing failure (wrong target responds)
        - ASD mode not setting correctly
"""

import pytest
from src.protocol.sdr_parser import SdrCheckResult
from src.transport.jtag_client import (
    JtagClient, JtagVerifier, KNOWN_IDCODES, VALID_ACCEL_IDCODES
)
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# TC_BMC_0_0102 — BMC JTAG to CPU0 via ASD software path
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0102_jtag_cpu0_asd_software(
        jtag_client, sys_conf, test_config):
    """
    TC_BMC_0_0102 — Verify CPU0 JTAG via ASD (At-Scale Debug) software path.

    The ASD path explicitly sets ASD mode on the BMC JTAG controller before
    reading the IDCODE. This exercises the ASD init sequence separately from
    the basic jtagtest path (TC_BMC_0_0100), verifying that the BMC ASD
    firmware module initialises correctly and does not corrupt JTAG state.

    If TC_BMC_0_0100 passes but this test fails, the ASD mode-set command
    is leaving the JTAG controller in a bad state.
    """
    is_authorized('TC_BMC_0_0102', test_config)

    if not sys_conf.get('CPU0', 0):
        pytest.skip('CPU0 not present per sys_conf.json')

    # explicitly set ASD mode before reading IDCODE
    jtag_client._set_asd_mode()

    check    = SdrCheckResult()
    verifier = JtagVerifier(jtag_client)
    verifier.verify_cpu(cpu_index=0, check=check)

    assert check.result_code == 0, (
        f'TC_BMC_0_0102 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0103 — BMC JTAG to CPU1 via ASD software path
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0103_jtag_cpu1_asd_software(
        jtag_client, sys_conf, test_config):
    """
    TC_BMC_0_0103 — Verify CPU1 JTAG via ASD software path.

    Same ASD mode-set verification as TC_BMC_0_0102 applied to CPU1.
    Both CPUs share one daisy-chained scan chain.
    """
    is_authorized('TC_BMC_0_0103', test_config)

    if not sys_conf.get('CPU1', 0):
        pytest.skip('CPU1 not present per sys_conf.json')

    jtag_client._set_asd_mode()

    check    = SdrCheckResult()
    verifier = JtagVerifier(jtag_client)
    verifier.verify_cpu(cpu_index=1, check=check)

    assert check.result_code == 0, (
        f'TC_BMC_0_0103 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0106 — MB MIPI-60 JTAG to CPU0 <Fixture>
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0106_mipi60_cpu0(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0106 — Verify CPU0 JTAG via MB MIPI-60 debug connector.

    <Fixture> means this test requires an external JTAG debug probe
    physically connected to the MIPI-60 header on the motherboard.
    This is an independent JTAG path that bypasses the BMC JTAG controller.

    Implement fixture_probe_read_idcode() with your lab probe tool
    (OpenOCD, Lauterbach, XDS, etc.) and remove the skip below.
    """
    is_authorized('TC_BMC_0_0106', test_config)

    if not sys_conf.get('CPU0', 0):
        pytest.skip('CPU0 not present per sys_conf.json')

    pytest.skip(
        'TC_BMC_0_0106 requires a physical MIPI-60 probe on MB header. '
        'Implement with your lab probe tool and remove this skip.'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0107 — MB MIPI-60 JTAG to CPU1 <Fixture>
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0107_mipi60_cpu1(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0107 — Verify CPU1 JTAG via MB MIPI-60 debug connector.
    Requires physical MIPI-60 probe on motherboard header.
    """
    is_authorized('TC_BMC_0_0107', test_config)

    if not sys_conf.get('CPU1', 0):
        pytest.skip('CPU1 not present per sys_conf.json')

    pytest.skip(
        'TC_BMC_0_0107 requires a physical MIPI-60 probe on MB header. '
        'Implement with your lab probe tool and remove this skip.'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0108 — MB MIPI-60 JTAG to PCH <Fixture>
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0108_mipi60_pch(
        ipmi_client, test_config):
    """
    TC_BMC_0_0108 — Verify PCH JTAG via MB MIPI-60 debug connector.
    Requires physical MIPI-60 probe on motherboard header.
    """
    is_authorized('TC_BMC_0_0108', test_config)

    pytest.skip(
        'TC_BMC_0_0108 requires a physical MIPI-60 probe on MB header. '
        'Implement with your lab probe tool and remove this skip.'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0109 — UBB MIPI-60 JTAG OAM[7:0] <Fixture>
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0109_ubb_mipi60_oam(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0109 — Verify OAM[7:0] JTAG via UBB MIPI-60 debug connector.

    The UBB has its own MIPI-60 header providing independent JTAG access
    to OAM accelerator modules — separate from the BMC JTAG MUX path
    used in TC_BMC_0_0104.
    Requires physical MIPI-60 probe on UBB header.
    """
    is_authorized('TC_BMC_0_0109', test_config)

    any_oam = any(sys_conf.get(f'OAM{i}', 0) for i in range(8))
    if not any_oam:
        pytest.skip('No OAM modules present per sys_conf.json')

    pytest.skip(
        'TC_BMC_0_0109 requires a physical MIPI-60 probe on UBB header. '
        'Implement with your lab probe tool and remove this skip.'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0100 — JTAG CPU0 IDCODE
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0100_jtag_cpu0_idcode(
        jtag_client, sys_conf, test_config):
    """
    TC_BMC_0_0100 — Read and verify CPU0 JTAG IDCODE.

    Three-step sequence:
        1. Set ASD mode on BMC JTAG controller
        2. Route hardware MUX to CPU chain (target 0x09)
        3. Read 32-bit IDCODE for device index 0 (CPU0)

    Pass condition:
        IDCODE matches KNOWN_IDCODES['CPU_PRIMARY'].raw_value
        AND bit[0] == 1 (IEEE 1149.1 compliance)

    Failure diagnosis:
        0x00000000 = TDO line floating or stuck low
        0xFFFFFFFF = BYPASS register loaded (CPU not in scan chain)
        Wrong value = unexpected silicon stepping
    """
    is_authorized('TC_BMC_0_0100', test_config)

    if not sys_conf.get('CPU0', 0):
        pytest.skip('CPU0 not present per sys_conf.json')

    check    = SdrCheckResult()
    verifier = JtagVerifier(jtag_client)
    verifier.verify_cpu(cpu_index=0, check=check)

    assert check.result_code == 0, (
        f'TC_BMC_0_0100 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0101 — JTAG CPU1 IDCODE
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0101_jtag_cpu1_idcode(
        jtag_client, sys_conf, test_config):
    """
    TC_BMC_0_0101 — Read and verify CPU1 JTAG IDCODE.

    Same scan chain as CPU0 (daisy-chained). Device index 1 = CPU1.

    Note on scan chain ordering:
        The BMC's JTAG engine extracts the correct IDCODE per device_index.
        CPU1 is physically closest to BMC TDO, CPU0 is furthest.
        device_index=1 requests CPU1's IDCODE from the chain.
    """
    is_authorized('TC_BMC_0_0101', test_config)

    if not sys_conf.get('CPU1', 0):
        pytest.skip('CPU1 not present per sys_conf.json')

    check    = SdrCheckResult()
    verifier = JtagVerifier(jtag_client)
    verifier.verify_cpu(cpu_index=1, check=check)

    assert check.result_code == 0, (
        f'TC_BMC_0_0101 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0104 — JTAG accelerator modules (all slots)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0104_jtag_all_accel_slots(
        jtag_client, sys_conf, test_config):
    """
    TC_BMC_0_0104 — Read and verify JTAG IDCODE for all accelerator slots.

    Each accelerator slot has an independent scan chain.
    The hardware MUX routes to each slot separately.
    Only slots marked present in sys_conf are verified.
    Absent slots are skipped (not failed).

    Valid IDCODEs: two known variants (different silicon revisions
    of the same accelerator component are both acceptable).
    """
    is_authorized('TC_BMC_0_0104', test_config)

    any_present = any(sys_conf.get(f'OAM{i}', 0) for i in range(8))
    if not any_present:
        pytest.skip('No accelerator modules present per sys_conf.json')

    check    = SdrCheckResult()
    verifier = JtagVerifier(jtag_client)

    for slot in range(8):
        if not sys_conf.get(f'OAM{slot}', 0):
            continue

        idcode = jtag_client.read_accel_idcode(slot)

        if idcode is None:
            check.fail(
                f'Slot {slot}: IDCODE read returned no data. '
                f'Check physical installation and MUX routing.'
            )
            continue

        if not idcode.is_valid():
            check.fail(
                f'Slot {slot}: IDCODE 0x{idcode.raw_value:08X} '
                f'violates IEEE 1149.1 (bit[0]={idcode.lsb}). '
                f'TDO line may be floating.'
            )
            continue

        if idcode.raw_value in VALID_ACCEL_IDCODES:
            check.ok(f'Slot {slot}: IDCODE verified {idcode}')
        else:
            check.fail(
                f'Slot {slot}: IDCODE 0x{idcode.raw_value:08X} '
                f'not in valid set '
                f'{[hex(v) for v in VALID_ACCEL_IDCODES]}'
            )

    assert check.result_code == 0, (
        f'TC_BMC_0_0104 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0105 — JTAG PCH
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0105_jtag_pch_idcode(
        jtag_client, sys_conf, test_config):
    """
    TC_BMC_0_0105 — Read and verify PCH JTAG IDCODE.

    PCH has a dedicated scan chain independent from the CPU chain.
    MUX routes to PCH target (0x0A) before reading.

    Requires host to be powered on — PCH JTAG is not accessible
    in standby power state.
    """
    is_authorized('TC_BMC_0_0105', test_config)

    check    = SdrCheckResult()
    verifier = JtagVerifier(jtag_client)
    verifier.verify_pch(check=check)

    assert check.result_code == 0, (
        f'TC_BMC_0_0105 FAILED:\n{check.summary()}'
    )
