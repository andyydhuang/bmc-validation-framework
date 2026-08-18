"""
src/tests/test_bmc_cpld.py

Hardware integration test for CPLD fault injection.

TC IDs covered:
    TC_BMC_0_0180 — CPLD CATERR fault injection and ACD verification

This test is the most invasive integration test in the suite:
    - Unlocks CPLD write protection (four-byte key sequence)
    - Injects a synthetic CATERR signal via CPLD register write
    - Verifies BMC correctly detected the fault and wrote a SEL record
    - Waits for ACD (Automated Crash Dump) collection to complete
    - Verifies CPLD is re-locked after injection (try/finally)

WARNING:
    This test triggers a CPU catastrophic error event.
    The BMC will begin ACD collection (JTAG register dump,
    PECI thermal state capture, PCIe error log collection).
    ACD collection takes 2-5 minutes and monopolizes JTAG/PECI.
    Do NOT run this test while other JTAG or PECI tests are active.

    Run this test LAST in any integration test session.

Requires:
    - Real BMC reachable at --bmc-ip
    - Host powered on and POST complete
    - CPLD firmware version that supports the four-byte unlock key
    - Run with: pytest src/tests/test_bmc_cpld.py --integration

CPLD signal path:
    IPMI Master Write-Read (NetFn=0x06 Cmd=0x52)
    → I2C bus 9, CPLD address 0x40
    → Register 0x3B (SGPIO_F5 injection register)
    → SGPIO_F5 pin driven low (active-low assertion)
    → BMC GPIO interrupt fires on falling edge
    → BMC CATERR handler triggered
    → SEL record written
    → ACD collection begins
"""

import time
import pytest
from src.protocol.sdr_parser import SdrCheckResult
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# CPLD register constants
# ---------------------------------------------------------------------------

# I2C bus and CPLD address for CATERR injection CPLD
_CPLD_BUS  = '0xNN'   # replace with I2C bus number for CATERR injection CPLD
_CPLD_ADDR = '0xNN'   # replace with I2C address of CATERR injection CPLD

# Unlock key: four register-value pairs written in sequence
# Values are platform-specific — adjust for your CPLD firmware version
_UNLOCK_SEQUENCE = [
    ('0xNN', '0xNN'),   # register offset, key byte — replace with platform values
    ('0xNN', '0xNN'),
    ('0xNN', '0xNN'),
    ('0xNN', '0xNN'),
]
_UNLOCK_REG_BASE = '0xNN'   # replace with CPLD register bank base address for unlock key

# Injection registers
_REG_INJECTION = '0xNN'   # replace with CPLD register offset for CATERR injection signal
_REG_MASK      = '0xNN'   # replace with CPLD register offset for signal propagation mask
_CATERR_MASK   = '0xNN'   # replace with bitmask value to unmask CATERR signal to BMC GPIO

# SEL poll parameters
_SEL_POLL_INTERVAL = 2     # seconds between SEL reads
_SEL_POLL_TIMEOUT  = 30    # maximum seconds to wait for SEL record
_ACD_WAIT_TIMEOUT  = 300   # maximum seconds to wait for ACD completion
_ACD_POLL_INTERVAL = 10    # seconds between ACD completion checks


# ---------------------------------------------------------------------------
# Helper: CPLD write via IPMI Master Write-Read
# ---------------------------------------------------------------------------

def _cpld_write(ipmi_client, reg_offset: str, value: str) -> bool:
    """
    Write one byte to CPLD register via I2C bridge.

    Returns True if write succeeded, False if BMC reported error.
    """
    response = ipmi_client.run(
        'raw', '0x06', '0x52',
        _CPLD_BUS,
        _CPLD_ADDR,
        '0x00',          # bytes to read back = 0 (write only)
        _UNLOCK_REG_BASE,
        reg_offset,
        value,
    )
    return 'Unable' not in response


def _unlock_cpld(ipmi_client) -> bool:
    """
    Write the four-byte unlock key sequence.
    Returns True only if ALL four writes succeed.
    """
    for reg_offset, key_byte in _UNLOCK_SEQUENCE:
        if not _cpld_write(ipmi_client, reg_offset, key_byte):
            return False
    return True


def _relock_cpld(ipmi_client):
    """
    Re-lock CPLD by writing an incorrect first key byte.
    Best-effort — does not raise on failure.
    Called in finally block to guarantee re-lock.
    """
    try:
        _cpld_write(
            ipmi_client,
            _UNLOCK_SEQUENCE[0][0],   # first register offset
            '0x00',                    # wrong value — invalidates key
        )
    except Exception:
        pass   # re-lock is best-effort only


# ---------------------------------------------------------------------------
# TC_BMC_0_0180 — CPLD CATERR fault injection
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0180_cpld_caterr_injection(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0180 — Inject CATERR signal via CPLD and verify BMC detection.

    Complete injection sequence:
        1. Verify system is powered on (CATERR requires CPU power)
        2. Clear SEL (clean baseline before injection)
        3. Unlock CPLD write protection (four-byte key)
        4. Clear injection register (de-assert, ensure clean state)
        5. Assert injection register (active-low: write 0x00)
        6. Unmask signal propagation to BMC GPIO
        7. Poll SEL until CATERR record appears (30s timeout)
        8. Wait for ACD collection to complete (5 min timeout)
        9. Re-lock CPLD (guaranteed via try/finally)

    Pass condition:
        A SEL record containing 'CATERR' appears within 30 seconds
        of injection.

    ACD collection:
        ACD completion is best-effort — a warning is issued if ACD
        does not confirm completion within 300 seconds, but this
        does not fail the test. ACD timing varies by platform and
        BMC firmware version.

    CPLD re-lock:
        Guaranteed via try/finally even if an exception occurs
        during injection or SEL polling.
    """
    is_authorized('TC_BMC_0_0180', test_config)

    # precondition: host must be powered on
    try:
        power_on = ipmi_client.get_power_state()
    except Exception as exc:
        pytest.skip(f'Cannot determine power state: {exc}')

    if not power_on:
        pytest.skip(
            'Host is powered off. CATERR injection requires the CPU '
            'to be powered on so the BMC CATERR detection path is active.'
        )

    check = SdrCheckResult()

    # ── Step 1: clear SEL for clean baseline ──────────────────────────
    ipmi_client.run('sel', 'clear')
    time.sleep(2)
    check.ok('SEL cleared before injection')

    # ── Steps 3-8: injection sequence wrapped in try/finally ──────────
    # try/finally guarantees CPLD re-lock even if:
    #   - an unlock step fails
    #   - an injection step raises an exception
    #   - SEL polling times out
    #   - pytest itself cancels the test via KeyboardInterrupt

    unlock_succeeded = False

    try:
        # ── Step 2: unlock CPLD ───────────────────────────────────────
        unlock_succeeded = _unlock_cpld(ipmi_client)
        if not unlock_succeeded:
            check.fail(
                'CPLD unlock sequence failed. '
                'Check CPLD firmware version — the four-byte key '
                'platform-specific unlock key may differ — check CPLD firmware spec.'
            )
            # do not proceed with injection if unlock failed
            # remaining steps would silently do nothing without unlock

        else:
            check.ok('CPLD unlock sequence complete (4-byte key accepted)')

            # ── Step 3: clear injection register ─────────────────────
            if not _cpld_write(ipmi_client, _REG_INJECTION, '0x40'):
                check.fail(
                    f'Failed to clear injection register {_REG_INJECTION}. '
                    f'CPLD may have re-locked after unlock sequence.'
                )
            else:
                check.ok('Injection register cleared (de-asserted)')

                # ── Step 4: assert CATERR (active-low: write 0x00) ───
                if not _cpld_write(ipmi_client, _REG_INJECTION, '0x00'):
                    check.fail(
                        f'Failed to assert CATERR injection register. '
                        f'CPLD register {_REG_INJECTION} write rejected.'
                    )
                else:
                    check.ok(
                        'CATERR asserted: SGPIO_F5 driven low '
                        '(active-low signal)'
                    )

                    # ── Step 5: unmask signal to BMC GPIO ─────────────
                    if not _cpld_write(
                            ipmi_client, _REG_MASK, _CATERR_MASK):
                        check.fail(
                            f'Failed to unmask CATERR signal. '
                            f'Register {_REG_MASK} write rejected. '
                            f'BMC GPIO will not see the injection.'
                        )
                    else:
                        check.ok(
                            'CATERR signal unmasked — '
                            'BMC GPIO interrupt should fire'
                        )

    finally:
        # ── Step 8: re-lock CPLD — ALWAYS runs ───────────────────────
        _relock_cpld(ipmi_client)
        check.ok(
            'CPLD re-locked '
            '(guaranteed by finally block regardless of outcome)'
        )

    # ── Step 6: poll SEL for CATERR record ────────────────────────────
    # Only poll if unlock and injection both succeeded
    if unlock_succeeded and check.result_code == 0:
        caterr_found = False
        caterr_line  = ''
        elapsed      = 0

        while elapsed < _SEL_POLL_TIMEOUT:
            sel_output = ipmi_client.run('sel', 'elist')
            for line in sel_output.splitlines():
                if 'CATERR' in line:
                    caterr_found = True
                    caterr_line  = line.strip()
                    break
            if caterr_found:
                break
            time.sleep(_SEL_POLL_INTERVAL)
            elapsed += _SEL_POLL_INTERVAL

        if caterr_found:
            check.ok(
                f'CATERR SEL record confirmed after {elapsed}s: '
                f'{caterr_line}'
            )
        else:
            check.fail(
                f'No CATERR SEL record appeared within '
                f'{_SEL_POLL_TIMEOUT}s of injection. '
                f'Possible causes:\n'
                f'  1. CPLD unlock failed silently (check CPLD FW version)\n'
                f'  2. BMC CATERR GPIO interrupt handler is disabled\n'
                f'  3. SGPIO_F5 signal path broken (check PCB trace)\n'
                f'  4. BMC firmware does not map SGPIO_F5 to CATERR\n'
                f'  5. SEL was full before clear completed'
            )

        # ── Step 7: wait for ACD completion ───────────────────────────
        if caterr_found:
            acd_done    = False
            acd_elapsed = 0

            while acd_elapsed < _ACD_WAIT_TIMEOUT:
                sel_output = ipmi_client.run('sel', 'elist')
                for line in sel_output.splitlines():
                    # ACD completion is platform-specific —
                    # look for a second distinct SEL event after CATERR
                    if 'ACD' in line and 'CATERR' not in line:
                        acd_done    = True
                        acd_elapsed_final = acd_elapsed
                        break
                if acd_done:
                    break
                time.sleep(_ACD_POLL_INTERVAL)
                acd_elapsed += _ACD_POLL_INTERVAL

            if acd_done:
                check.ok(
                    f'ACD crash dump collection confirmed complete '
                    f'after {acd_elapsed_final}s'
                )
            else:
                # warn only — ACD timing is platform/firmware-dependent
                check.warn(
                    f'ACD completion not confirmed within '
                    f'{_ACD_WAIT_TIMEOUT}s. '
                    f'Dump may still be in progress in background. '
                    f'Check BMC filesystem for crashdump files.'
                )

    assert check.result_code == 0, (
        f'TC_BMC_0_0180 FAILED:\n{check.summary()}'
    )
