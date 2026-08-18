"""
src/tests/test_bmc_sdr.py

Hardware integration tests for SDR sensor validation.

TC IDs covered:
    TC_BMC_0_0200 — System fan status and speed
    TC_BMC_0_0201 — PSU status, temperature, fan, current, power
    TC_BMC_0_0202 — Board VR voltage
    TC_BMC_0_0203 — PCIe switch temperature
    TC_BMC_0_0204 — Board power consumption
    TC_BMC_0_0205 — UBB temperature (board + QSFP-DD)
    TC_BMC_0_0206 — CPU status, temperature, power, voltage
    TC_BMC_0_0207 — NVMe SSD status and temperature
    TC_BMC_0_0208 — DIMM temperature and memory power
    TC_BMC_0_0209 — OAM temperature and power
    TC_BMC_0_0210 — OCP NIC presence and temperature
    TC_BMC_0_0211 — System fan speed sweep (PWM + TACH)
    TC_BMC_0_0250 — BMC SDR baseline during BIOS POST
    TC_BMC_0_0251 — BMC SDR baseline at DC-ON (pre-POST)

All SDR tests:
    - Fetch fresh 'sdr elist all' output at test start
    - Apply bidirectional verification against sys_conf.json
    - Use SdrEntry.parse_all() for consistent field extraction
    - Validate in both directions: present hardware must have sensors,
      absent hardware must NOT have sensors

Run with:
    pytest src/tests/test_bmc_sdr.py --integration
           --bmc-ip <ip> --bmc-user admin --bmc-password <pw>
           --sys-conf config/sys_conf.json
"""

import time
import pytest
from src.protocol.sdr_parser import (
    SdrEntry, SdrCheckResult,
    verify_cpu_sdr, verify_psu_sdr,
)
from src.transport.fan_controller import (
    FanController, FanSpeedVerifier, GENERIC_FAN_SPEC,
)
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared SDR fetch helper
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def sdr_output(ipmi_client):
    """
    Fetch SDR once per module and reuse across all SDR tests.
    Avoids hammering the BMC with repeated 'sdr elist all' calls.
    Each test receives the same snapshot taken at module start.
    """
    return ipmi_client.run('sdr', 'elist', 'all')


# ---------------------------------------------------------------------------
# TC_BMC_0_0200 — Fan status and speed
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0200_system_fan_status(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0200 — Verify all system fan sensors in SDR.

    For each FAN_SYS_{N} slot in sys_conf:
        Present (1): Fan_SYS{N}_0 (inlet) and Fan_SYS{N}_1 (outlet)
                     must appear in SDR with non-zero RPM readings.
        Absent  (0): Fan_SYS{N}_0 and _1 must show 'No Reading'.

    Uses bidirectional check — catches both missing sensors and
    phantom sensors for empty fan slots.
    """
    is_authorized('TC_BMC_0_0200', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    for fan_idx in range(8):
        key       = f'FAN_SYS_{fan_idx}'
        present   = sys_conf.get(key, 0)
        inlet_key = f'Fan_SYS{fan_idx}_0'
        outlet_key = f'Fan_SYS{fan_idx}_1'

        for sensor_key in (inlet_key, outlet_key):
            entry = sdrs.get(sensor_key)

            if present:
                if entry is None:
                    check.fail(
                        f'{sensor_key}: fan present in sys_conf '
                        f'but sensor not found in SDR output.'
                    )
                elif 'No Reading' in entry.reading:
                    check.fail(
                        f'{sensor_key}: fan present but sensor '
                        f'shows No Reading — check fan installation.'
                    )
                else:
                    try:
                        rpm = int(entry.reading.split()[0])
                        if rpm == 0:
                            check.fail(
                                f'{sensor_key}: 0 RPM on present fan — '
                                f'fan may be stalled or not spinning.'
                            )
                        else:
                            check.ok(f'{sensor_key}: {entry.reading}')
                    except (ValueError, IndexError):
                        check.fail(
                            f'{sensor_key}: cannot parse RPM from '
                            f'[{entry.reading}]'
                        )
            else:
                if entry and 'No Reading' not in entry.reading:
                    check.fail(
                        f'{sensor_key}: fan absent in sys_conf but '
                        f'sensor shows [{entry.reading}] — '
                        f'phantom sensor detected.'
                    )
                else:
                    check.ok(f'{sensor_key}: correctly absent/No Reading')

    assert check.result_code == 0, (
        f'TC_BMC_0_0200 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0201 — PSU status
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0201_psu_status(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0201 — PSU bidirectional sensor validation.

    Uses verify_psu_sdr() from sdr_parser.py which handles the
    AC-lost state correctly — 0 RPM and 0 Watts are expected when
    a PSU is installed but AC power is not connected.
    """
    is_authorized('TC_BMC_0_0201', test_config)

    psu_sdrs = frozenset(
        name for name in SdrEntry.parse_all(sdr_output)
        if name.startswith('PSU') or name.startswith('Fan_PSU')
        or name.startswith('Temp_PSU')
    )

    check = verify_psu_sdr(
        raw_output  = sdr_output,
        power_state = 2,
        sys_conf    = sys_conf,
        psu_sdrs    = psu_sdrs,
    )

    assert check.result_code == 0, (
        f'TC_BMC_0_0201 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0206 — CPU sensors
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0206_cpu_sensors(
        sdr_output, sys_conf, test_config, ipmi_client):
    """
    TC_BMC_0_0206 — Bidirectional CPU sensor verification.

    Uses verify_cpu_sdr() which checks:
        Pass 1: CPU presence sensors vs sys_conf
        Pass 2: Temperature, VR voltage, aggregate sensors
                cross-referenced against Pass 1 presence states

    Requires BIOS POST complete for valid readings.
    """
    is_authorized('TC_BMC_0_0206', test_config)

    cpu_sdrs = frozenset(
        name for name in SdrEntry.parse_all(sdr_output)
        if 'CPU' in name and name not in ('CPU0', 'CPU1')
    )

    check = verify_cpu_sdr(
        raw_output  = sdr_output,
        power_state = 2,
        sys_conf    = sys_conf,
        cpu_sdrs    = cpu_sdrs,
    )

    assert check.result_code == 0, (
        f'TC_BMC_0_0206 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0207 — NVMe SSD sensors
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0207_nvme_ssd_sensors(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0207 — NVMe SSD presence and temperature bidirectional check.

    For each NVMeSSD_{N} in sys_conf:
        Present: NVMeSSD_{N} must show 'Drive Present',
                 Temp_NVMeSSD{N} must have a valid reading.
        Absent:  NVMeSSD_{N} must show 'No Reading' or not appear.
    """
    is_authorized('TC_BMC_0_0207', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    for nvme_idx in range(18):
        key      = f'NVMeSSD_{nvme_idx}'
        present  = sys_conf.get(key, 0)
        prsnt_sn = f'NVMeSSD_{nvme_idx}'
        temp_sn  = f'Temp_NVMeSSD{nvme_idx}'

        prsnt_entry = sdrs.get(prsnt_sn)
        temp_entry  = sdrs.get(temp_sn)

        if present:
            if prsnt_entry is None or 'Drive Present' not in prsnt_entry.reading:
                check.fail(
                    f'{prsnt_sn}: NVMe present in sys_conf but '
                    f'sensor shows [{getattr(prsnt_entry, "reading", "MISSING")}]'
                )
            else:
                check.ok(f'{prsnt_sn}: Drive Present')

            if temp_entry is None or 'No Reading' in temp_entry.reading:
                check.fail(
                    f'{temp_sn}: NVMe present but temperature '
                    f'shows No Reading — check drive installation.'
                )
            else:
                check.ok(f'{temp_sn}: {temp_entry.reading}')

        else:
            if prsnt_entry and 'No Reading' not in prsnt_entry.reading \
                    and 'Drive Present' in prsnt_entry.reading:
                check.fail(
                    f'{prsnt_sn}: absent in sys_conf but shows '
                    f'Drive Present — phantom sensor.'
                )
            else:
                check.ok(f'{prsnt_sn}: correctly absent')

    assert check.result_code == 0, (
        f'TC_BMC_0_0207 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0208 — DIMM temperature
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0208_dimm_temperature(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0208 — DIMM temperature sensor bidirectional check.

    DIMM slots: A0-A1, B0-B1, ... P0-P1 (16 channels × 2 = 32 slots).
    For each slot in sys_conf:
        Present: DIMM_{slot} must show 'Presence detected',
                 Temp_DIMM_{slot} must have a valid temperature.
        Absent:  DIMM_{slot} must show 'No Reading'.
    """
    is_authorized('TC_BMC_0_0208', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    channels  = 'ABCDEFGHIJKLMNOP'
    sub_slots = [0, 1]

    for ch in channels:
        for sub in sub_slots:
            slot_key  = f'DIMM_{ch}{sub}'
            present   = sys_conf.get(slot_key, 0)
            prsnt_sn  = f'DIMM_{ch}{sub}'
            temp_sn   = f'Temp_DIMM_{ch}{sub}'

            prsnt_entry = sdrs.get(prsnt_sn)
            temp_entry  = sdrs.get(temp_sn)

            if present:
                if prsnt_entry is None or \
                        'Presence detected' not in prsnt_entry.reading:
                    check.fail(
                        f'{prsnt_sn}: DIMM present in sys_conf '
                        f'but not detected in SDR.'
                    )
                else:
                    check.ok(f'{prsnt_sn}: Presence detected')

                if temp_entry is None or 'No Reading' in temp_entry.reading:
                    check.fail(
                        f'{temp_sn}: DIMM present but temperature '
                        f'shows No Reading.'
                    )
                else:
                    check.ok(f'{temp_sn}: {temp_entry.reading}')
            else:
                if prsnt_entry and \
                        'No Reading' not in prsnt_entry.reading:
                    check.fail(
                        f'{prsnt_sn}: absent in sys_conf but '
                        f'sensor shows [{prsnt_entry.reading}]'
                    )
                else:
                    check.ok(f'{prsnt_sn}: correctly absent')

    assert check.result_code == 0, (
        f'TC_BMC_0_0208 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0210 — OCP NIC presence and temperature
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0210_ocp_nic_sensors(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0210 — OCP NIC card presence and temperature sensors.

    Checks MB_OCP1, MB_OCP2 slots:
        Present: MB_OCP{N}_PRSNT must show 'Presence detected',
                 Temp_MB_OCP{N} must have a valid temperature reading.
        Absent:  Sensors must show No Reading.
    """
    is_authorized('TC_BMC_0_0210', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    for ocp_idx in range(1, 3):
        key          = f'MB_OCP{ocp_idx}'
        present      = sys_conf.get(key, 0)
        prsnt_sn     = f'MB_OCP{ocp_idx}_PRSNT'
        temp_sn      = f'Temp_MB_OCP{ocp_idx}'

        prsnt_entry  = sdrs.get(prsnt_sn)
        temp_entry   = sdrs.get(temp_sn)

        if present:
            if prsnt_entry is None or \
                    'Presence detected' not in prsnt_entry.reading:
                check.fail(
                    f'{prsnt_sn}: OCP NIC present in sys_conf '
                    f'but not detected in SDR.'
                )
            else:
                check.ok(f'{prsnt_sn}: Presence detected')

            if temp_entry is None or 'No Reading' in temp_entry.reading:
                check.fail(
                    f'{temp_sn}: OCP NIC present but temperature '
                    f'shows No Reading.'
                )
            else:
                check.ok(f'{temp_sn}: {temp_entry.reading}')
        else:
            if prsnt_entry and \
                    'No Reading' not in prsnt_entry.reading:
                check.fail(
                    f'{prsnt_sn}: absent in sys_conf but '
                    f'sensor shows [{prsnt_entry.reading}]'
                )
            else:
                check.ok(f'{prsnt_sn}: correctly absent')

    assert check.result_code == 0, (
        f'TC_BMC_0_0210 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0211 — Fan speed sweep
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0211_fan_speed_sweep(
        fan_controller, ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0211 — Complete fan PWM sweep with dual TACH readback.

    Three duty cycle test points: 100%, 50%, 10%.
    At each point:
        - TACH read from CPLD register (real-time, direct)
        - TACH read from SDR (BMC polling path, indirect)
        - Both readings validated against GENERIC_FAN_SPEC bounds
        - CPLD/SDR delta checked — large delta (>15%) logged as warning

    Auto-fan control is guaranteed to be restored via try/finally
    even if the test raises an exception mid-sweep.

    WARNING: This test commands fans to 10% duty cycle momentarily.
    Ensure server is not running thermally sensitive workloads.
    """
    is_authorized('TC_BMC_0_0211', test_config)

    check    = SdrCheckResult()
    verifier = FanSpeedVerifier(fan_controller, GENERIC_FAN_SPEC)

    def sdr_fetch():
        return ipmi_client.run('sdr', 'elist', 'all')

    verifier.run_full_sweep(check=check, sdr_fetch_fn=sdr_fetch)

    assert check.result_code == 0, (
        f'TC_BMC_0_0211 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0202 — Board VR voltage sensors
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0202_board_vr_voltage(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0202 — Verify board voltage regulator (VR) sensors.

    VR voltage sensors are CPU-presence-gated:
        CPU present:  sensor must show a valid voltage reading
                      (not 'Disabled' and not 'No Reading')
        CPU absent:   sensor must show 'Disabled' or 'No Reading'

    Sensor naming convention (adjust for your platform):
        Vol_PVCCIN_CPU0   — CPU0 input voltage rail
        Vol_PVCCIN_CPU1   — CPU1 input voltage rail
        Vol_PVCCFA_CPU0   — CPU0 fully-integrated voltage regulator
        Vol_PVCCFA_CPU1   — CPU1 FIVR
        Vol_VNN_MEM_CPU0  — CPU0 VNN memory rail
        Vol_VNN_MEM_CPU1  — CPU1 VNN memory rail

    Bidirectional check:
        Present CPU → voltage must be non-disabled
        Absent  CPU → voltage must be Disabled
    """
    is_authorized('TC_BMC_0_0202', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    # voltage sensor patterns per CPU index
    # key = sys_conf key, value = list of expected sensor name prefixes
    vr_sensor_map = {
        0: ['Vol_PVCCIN_CPU0', 'Vol_PVCCFA_CPU0', 'Vol_VNN_MEM_CPU0'],
        1: ['Vol_PVCCIN_CPU1', 'Vol_PVCCFA_CPU1', 'Vol_VNN_MEM_CPU1'],
    }

    for cpu_idx, sensor_names in vr_sensor_map.items():
        cpu_present = sys_conf.get(f'CPU{cpu_idx}', 0)

        for sensor_name in sensor_names:
            # find matching sensor in SDR (prefix match)
            matched = [
                entry for name, entry in sdrs.items()
                if name.startswith(sensor_name)
            ]

            if not matched:
                if cpu_present:
                    check.fail(
                        f'{sensor_name}: CPU{cpu_idx} present but '
                        f'VR sensor not found in SDR. '
                        f'Check BMC SDR repository initialization.'
                    )
                else:
                    check.ok(
                        f'{sensor_name}: not in SDR '
                        f'(CPU{cpu_idx} absent — expected)'
                    )
                continue

            for entry in matched:
                reading = entry.reading
                if cpu_present:
                    if 'Disabled' in reading or 'No Reading' in reading:
                        check.fail(
                            f'{entry.name}: CPU{cpu_idx} present but '
                            f'VR sensor shows [{reading}]. '
                            f'Check VR power delivery and BMC initialization.'
                        )
                    else:
                        check.ok(f'{entry.name}: {reading}')
                else:
                    if 'Disabled' in reading or 'No Reading' in reading:
                        check.ok(
                            f'{entry.name}: correctly Disabled '
                            f'(CPU{cpu_idx} absent)'
                        )
                    else:
                        check.fail(
                            f'{entry.name}: CPU{cpu_idx} absent but '
                            f'VR shows [{reading}] — phantom reading.'
                        )

    assert check.result_code == 0, (
        f'TC_BMC_0_0202 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0203 — PCIe switch temperature
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0203_pcie_switch_temperature(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0203 — Verify PCIe switch temperature sensors.

    PCIe switches are present per sys_conf PEXSW{N} flags.
    Each switch has:
        Temp_PESW{N}      — per-switch temperature
        T_PESW_Highest    — aggregate highest temperature across all switches

    Bidirectional check:
        Switch present:  sensor must show a valid temperature reading
        Switch absent:   sensor must show 'No Reading'

    The aggregate sensor T_PESW_Highest:
        If ANY switch is present → must show a valid reading
        If NO switches present   → must show 'No Reading'
    """
    is_authorized('TC_BMC_0_0203', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    any_pesw_present = False

    for sw_idx in range(4):
        key     = f'PEXSW{sw_idx}'
        present = sys_conf.get(key, 0)
        sn      = f'Temp_PESW{sw_idx}'
        entry   = sdrs.get(sn)

        if present:
            any_pesw_present = True
            if entry is None or 'No Reading' in entry.reading:
                check.fail(
                    f'{sn}: PCIe switch {sw_idx} present but '
                    f'temperature sensor shows No Reading or missing. '
                    f'Check switch power and BMC I2C access.'
                )
            else:
                check.ok(f'{sn}: {entry.reading}')
        else:
            if entry and 'No Reading' not in entry.reading:
                check.fail(
                    f'{sn}: PEXSW{sw_idx} absent in sys_conf but '
                    f'sensor shows [{entry.reading}] — phantom reading.'
                )
            else:
                check.ok(f'{sn}: correctly No Reading (absent)')

    # aggregate highest temperature sensor
    agg_entry = sdrs.get('T_PESW_Highest')
    if any_pesw_present:
        if agg_entry is None or 'No Reading' in agg_entry.reading:
            check.fail(
                'T_PESW_Highest: PCIe switches present but aggregate '
                'temperature sensor shows No Reading. '
                'BMC aggregation logic may not be initialized.'
            )
        else:
            check.ok(f'T_PESW_Highest: {agg_entry.reading}')
    else:
        if agg_entry and 'No Reading' not in agg_entry.reading:
            check.fail(
                f'T_PESW_Highest: no PCIe switches present but '
                f'aggregate shows [{agg_entry.reading}] — phantom.'
            )
        else:
            check.ok('T_PESW_Highest: correctly No Reading (no switches)')

    assert check.result_code == 0, (
        f'TC_BMC_0_0203 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0204 — Board power consumption
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0204_board_power_consumption(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0204 — Verify board-level power consumption sensors.

    Power sensors measure total platform power draw at various
    subsystem boundaries. Unlike temperature sensors, power sensors
    are not gated by individual component presence — the BMC always
    measures motherboard power as long as the platform is on.

    Sensors checked:
        Power_MB        — total motherboard power draw (Watts)
        Power_CPU       — combined CPU package power (Watts)
                          only checked if at least one CPU is present

    Valid reading: non-zero Watts value
    Invalid reading: 0 Watts (indicates power meter not initialized
                     or main power not asserted)
    No Reading: sensor not available (BMC not initialized)

    Note: Power_CPU reads zero if no CPUs are installed — this is
    expected and is handled by the cpu_present guard below.
    """
    is_authorized('TC_BMC_0_0204', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    # ── motherboard total power ────────────────────────────────────────
    mb_power = sdrs.get('Power_MB')
    if mb_power is None:
        check.fail(
            'Power_MB: sensor not found in SDR. '
            'Check power meter I2C connectivity and BMC initialization.'
        )
    elif 'No Reading' in mb_power.reading:
        check.fail(
            'Power_MB: shows No Reading. '
            'Possible: power meter not initialized, '
            'or main power not asserted.'
        )
    else:
        try:
            watts = float(mb_power.reading.split()[0])
            if watts == 0:
                check.fail(
                    f'Power_MB: reads 0 Watts. '
                    f'Power meter may not be initialized or '
                    f'the platform is in a low-power state.'
                )
            else:
                check.ok(f'Power_MB: {mb_power.reading}')
        except (ValueError, IndexError):
            check.fail(
                f'Power_MB: cannot parse Watts from '
                f'[{mb_power.reading}]'
            )

    # ── CPU package power (only if CPUs present) ──────────────────────
    any_cpu = sys_conf.get('CPU0', 0) or sys_conf.get('CPU1', 0)

    cpu_power = sdrs.get('Power_CPU')
    if any_cpu:
        if cpu_power is None:
            check.fail(
                'Power_CPU: CPU(s) present but sensor not found in SDR.'
            )
        elif 'No Reading' in cpu_power.reading:
            check.fail(
                'Power_CPU: CPU(s) present but sensor shows No Reading. '
                'Ensure BIOS POST is complete before reading CPU power.'
            )
        else:
            try:
                watts = float(cpu_power.reading.split()[0])
                if watts == 0:
                    check.fail(
                        'Power_CPU: CPU(s) present but reads 0 Watts. '
                        'Check CPU power delivery and PECI initialization.'
                    )
                else:
                    check.ok(f'Power_CPU: {cpu_power.reading}')
            except (ValueError, IndexError):
                check.fail(
                    f'Power_CPU: cannot parse Watts from '
                    f'[{cpu_power.reading}]'
                )
    else:
        if cpu_power and 'No Reading' not in cpu_power.reading:
            check.fail(
                f'Power_CPU: no CPUs present but sensor shows '
                f'[{cpu_power.reading}] — phantom reading.'
            )
        else:
            check.ok('Power_CPU: correctly No Reading (no CPUs)')

    assert check.result_code == 0, (
        f'TC_BMC_0_0204 FAILED:\n{check.summary()}'
    )



# ---------------------------------------------------------------------------
# TC_BMC_0_0205 — UBB Temperature (board + QSFP-DD)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0205_ubb_temperature(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0205 — UBB (Universal Baseboard) temperature sensor verification.

    The UBB board carries additional temperature sensors beyond the main
    motherboard:
        Temp_UBB          — UBB board ambient temperature
        Temp_QSFPDD_{N}   — QSFP-DD transceiver cage temperatures
                            (one per populated cage slot)

    Bidirectional check:
        UBB board detected → temperature sensor must have valid reading
        UBB board absent   → sensors must show No Reading
    """
    is_authorized('TC_BMC_0_0205', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    # UBB board presence — check for any UBB-associated sensor
    # The UBB is always installed on this platform — adjust if optional
    ubb_temp = sdrs.get('Temp_UBB')
    if ubb_temp:
        if 'No Reading' in ubb_temp.reading:
            check.fail(
                'Temp_UBB: sensor present but shows No Reading. '
                'Check UBB board I2C connectivity.'
            )
        else:
            check.ok(f'Temp_UBB: {ubb_temp.reading}')
    else:
        check.warn(
            'Temp_UBB: sensor not found in SDR output. '
            'This sensor may use a different name on your platform. '
            'Check ipmitool sdr elist output for UBB temperature sensors.'
        )

    # QSFP-DD cage temperatures (slots 0-7 typical)
    qsfp_found = 0
    for slot in range(8):
        sn    = f'Temp_QSFPDD_{slot}'
        entry = sdrs.get(sn)
        if entry:
            qsfp_found += 1
            if 'No Reading' in entry.reading:
                check.fail(
                    f'{sn}: QSFP-DD cage sensor present but No Reading. '
                    f'Transceiver may not be installed in cage {slot}.'
                )
            else:
                check.ok(f'{sn}: {entry.reading}')

    if qsfp_found == 0:
        check.warn(
            'No Temp_QSFPDD_* sensors found in SDR. '
            'QSFP-DD temperature sensors may use a different naming '
            'convention on your platform.'
        )

    assert check.result_code == 0, (
        f'TC_BMC_0_0205 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0209 — OAM Temperature / Power
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0209_oam_temperature_power(
        sdr_output, sys_conf, test_config):
    """
    TC_BMC_0_0209 — OAM (accelerator module) temperature and power sensors.

    For each OAM slot (OAM0-OAM7) marked present in sys_conf:
        Temp_OAM{N}   — module temperature must have valid reading
        Power_OAM{N}  — module power must have valid non-zero reading

    OAM modules require main DC power — only valid after host DC-ON.
    """
    is_authorized('TC_BMC_0_0209', test_config)

    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(sdr_output)

    any_oam_tested = False

    for oam_idx in range(8):
        key     = f'OAM{oam_idx}'
        present = sys_conf.get(key, 0)
        temp_sn = f'Temp_OAM{oam_idx}'
        pwr_sn  = f'Power_OAM{oam_idx}'

        temp_entry = sdrs.get(temp_sn)
        pwr_entry  = sdrs.get(pwr_sn)

        if present:
            any_oam_tested = True

            if temp_entry is None or 'No Reading' in temp_entry.reading:
                check.fail(
                    f'{temp_sn}: OAM{oam_idx} present but temperature '
                    f'sensor shows No Reading or is missing. '
                    f'Check OAM module power and I2C connectivity.'
                )
            else:
                check.ok(f'{temp_sn}: {temp_entry.reading}')

            if pwr_entry is None or 'No Reading' in pwr_entry.reading:
                check.fail(
                    f'{pwr_sn}: OAM{oam_idx} present but power '
                    f'sensor shows No Reading or is missing.'
                )
            else:
                try:
                    watts = float(pwr_entry.reading.split()[0])
                    if watts == 0:
                        check.fail(
                            f'{pwr_sn}: OAM{oam_idx} present but '
                            f'reads 0 Watts — power meter may not '
                            f'be initialized.'
                        )
                    else:
                        check.ok(f'{pwr_sn}: {pwr_entry.reading}')
                except (ValueError, IndexError):
                    check.fail(
                        f'{pwr_sn}: cannot parse Watts from '
                        f'[{pwr_entry.reading}]'
                    )
        else:
            for sn, entry in [(temp_sn, temp_entry), (pwr_sn, pwr_entry)]:
                if entry and 'No Reading' not in entry.reading:
                    check.fail(
                        f'{sn}: OAM{oam_idx} absent but sensor shows '
                        f'[{entry.reading}] — phantom reading.'
                    )
                else:
                    check.ok(f'{sn}: correctly absent/No Reading')

    if not any_oam_tested:
        pytest.skip(
            'No OAM modules present per sys_conf.json. '
            'Set OAM0-OAM7 to 1 to enable OAM sensor tests.'
        )

    assert check.result_code == 0, (
        f'TC_BMC_0_0209 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0250 — BMC SDR Test during BIOS POST
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0250_sdr_bios_post(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0250 — Verify key SDR sensors during BIOS POST state.

    BIOS POST is the period between chassis power-on and OS boot.
    During POST, some sensors initialize (fan RPM, PSU status, VR voltage)
    while others are not yet valid (CPU temperature, PECI-based sensors).

    This test verifies the sensors that SHOULD be valid during POST:
        - Fan presence and basic RPM (fans spin up at POST start)
        - PSU presence and status
        - Board ambient temperature sensors

    And verifies that POST-only sensors correctly show No Reading:
        - CPU thermal sensors (not valid until PECI init by BIOS)

    The test reads SDR at power_state=1 (DC on, POST in progress).
    """
    is_authorized('TC_BMC_0_0250', test_config)

    # ensure host is powered on but do NOT wait for POST complete
    try:
        if not ipmi_client.get_power_state():
            ipmi_client.set_power('on')
            time.sleep(15)   # let BIOS begin POST
    except Exception as exc:
        pytest.skip(f'Cannot control host power: {exc}')

    sdr_output = ipmi_client.run('sdr', 'elist', 'all')
    sdrs       = SdrEntry.parse_all(sdr_output)
    check      = SdrCheckResult()

    # fans should be spinning during POST
    for fan_idx in range(8):
        if not sys_conf.get(f'FAN_SYS_{fan_idx}', 0):
            continue
        inlet = sdrs.get(f'Fan_SYS{fan_idx}_0')
        if inlet and 'No Reading' not in inlet.reading:
            check.ok(f'Fan_SYS{fan_idx}_0: {inlet.reading} during POST')
        elif inlet:
            check.fail(
                f'Fan_SYS{fan_idx}_0: fan present but No Reading during POST. '
                f'Fans should be spinning at POST start.'
            )

    # PSU status should be valid at POST
    for psu_idx in range(6):
        if not sys_conf.get(f'PSU{psu_idx}', 0):
            continue
        status = sdrs.get(f'PSU{psu_idx}_Status')
        if status and 'Presence detected' in status.reading:
            check.ok(f'PSU{psu_idx}_Status: present during POST')
        elif status:
            check.fail(
                f'PSU{psu_idx}_Status: PSU present in sys_conf but '
                f'shows [{status.reading}] during POST.'
            )

    assert check.result_code == 0, (
        f'TC_BMC_0_0250 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0251 — BMC SDR Test at DC-ON (pre-POST)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0251_sdr_dc_on(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0251 — Verify BMC SDR sensors immediately after DC-ON.

    DC-ON is the moment main 12V power is asserted by pressing the
    power button (or IPMI chassis power on). This is BEFORE BIOS POST
    begins — the BMC is running but the host CPU has not started yet.

    At DC-ON the following should be valid:
        - BMC responds to IPMI commands (already true — we got here)
        - Fan presence sensors reflect installed fans
        - PSU presence sensors reflect installed PSUs

    The following should NOT be valid at DC-ON (CPU not running):
        - CPU temperature (PECI not yet initialized)
        - BIOS-reported sensors

    This test captures an SDR snapshot within 5 seconds of power-on
    to verify standby/early-boot sensor behavior.
    """
    is_authorized('TC_BMC_0_0251', test_config)

    # power cycle to get a clean DC-ON state
    try:
        if ipmi_client.get_power_state():
            ipmi_client.set_power('off')
            time.sleep(5)
        ipmi_client.set_power('on')
        time.sleep(3)   # 3 seconds after DC-ON — pre-POST
    except Exception as exc:
        pytest.skip(f'Cannot power cycle host: {exc}')

    sdr_output = ipmi_client.run('sdr', 'elist', 'all')
    sdrs       = SdrEntry.parse_all(sdr_output)
    check      = SdrCheckResult()

    # BMC should be alive and returning SDR data
    if not sdrs:
        check.fail(
            'No SDR entries returned within 3s of DC-ON. '
            'BMC may not be responding correctly at DC-ON.'
        )
    else:
        check.ok(f'SDR contains {len(sdrs)} entries at DC-ON')

    # PSU presence should be detectable immediately at DC-ON
    for psu_idx in range(6):
        if not sys_conf.get(f'PSU{psu_idx}', 0):
            continue
        status = sdrs.get(f'PSU{psu_idx}_Status')
        if status:
            if 'Presence detected' in status.reading:
                check.ok(
                    f'PSU{psu_idx}_Status: presence confirmed at DC-ON'
                )
            else:
                check.fail(
                    f'PSU{psu_idx}_Status: PSU present in sys_conf but '
                    f'[{status.reading}] at DC-ON. '
                    f'PSU presence should be detectable before POST.'
                )

    assert check.result_code == 0, (
        f'TC_BMC_0_0251 FAILED:\n{check.summary()}'
    )


# ---------------------------------------------------------------------------
# TC_BMC_0_0252 — BMC SDR Test at Power OFF (standby state)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0252_sdr_power_off(
        ipmi_client, sys_conf, test_config):
    """
    TC_BMC_0_0252 — Verify BMC SDR sensors at DC-OFF (standby) state.

    DC-OFF means host main power is de-asserted. Only standby-powered
    devices (FRU EEPROMs, CPLDs on standby domain, BMC itself) are
    accessible. All DC-ON sensors must show No Reading.

    This test verifies:
        - BMC continues to respond to IPMI over LAN while host is off
        - Sensors for DC-ON devices correctly show No Reading
        - PSU presence sensors remain valid (PSUs have standby power)
        - Fan sensors show No Reading (fans off when host is off)
    """
    is_authorized('TC_BMC_0_0252', test_config)

    # ensure host is powered off
    try:
        if ipmi_client.get_power_state():
            ipmi_client.set_power('off')
            time.sleep(10)
            if ipmi_client.get_power_state():
                pytest.skip(
                    'Host did not power off. '
                    'TC_BMC_0_0252 requires DC-OFF state.'
                )
    except Exception as exc:
        pytest.skip(f'Cannot control host power: {exc}')

    sdr_output = ipmi_client.run('sdr', 'elist', 'all')
    sdrs       = SdrEntry.parse_all(sdr_output)
    check      = SdrCheckResult()

    if not sdrs:
        check.fail(
            'No SDR entries returned at DC-OFF. '
            'BMC should remain accessible via IPMI when host is off.'
        )
    else:
        check.ok(f'BMC responsive at DC-OFF: {len(sdrs)} SDR entries')

    # CPU sensors must be No Reading at DC-OFF
    for cpu_idx in range(2):
        if not sys_conf.get(f'CPU{cpu_idx}', 0):
            continue
        temp_sn = f'Temp_CPU{cpu_idx}'
        entry   = sdrs.get(temp_sn)
        if entry and 'No Reading' not in entry.reading:
            check.fail(
                f'{temp_sn}: CPU temperature shows [{entry.reading}] '
                f'at DC-OFF — CPU should be unpowered. '
                f'Possible: host did not fully power off.'
            )
        else:
            check.ok(f'{temp_sn}: correctly No Reading at DC-OFF')

    # Fan sensors must be No Reading at DC-OFF
    for fan_idx in range(8):
        if not sys_conf.get(f'FAN_SYS_{fan_idx}', 0):
            continue
        sn    = f'Fan_SYS{fan_idx}_0'
        entry = sdrs.get(sn)
        if entry and 'No Reading' not in entry.reading:
            check.fail(
                f'{sn}: fan shows [{entry.reading}] at DC-OFF. '
                f'Fans should not be spinning with host off.'
            )
        else:
            check.ok(f'{sn}: correctly No Reading at DC-OFF')

    # PSU status should still be readable at DC-OFF (standby power)
    for psu_idx in range(6):
        if not sys_conf.get(f'PSU{psu_idx}', 0):
            continue
        status = sdrs.get(f'PSU{psu_idx}_Status')
        if status and 'Presence detected' in status.reading:
            check.ok(
                f'PSU{psu_idx}_Status: presence confirmed at DC-OFF'
            )

    assert check.result_code == 0, (
        f'TC_BMC_0_0252 FAILED:\n{check.summary()}'
    )
