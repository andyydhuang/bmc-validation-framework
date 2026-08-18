"""
src/tests/test_bmc_i2c.py

Hardware integration tests for I2C bus stress testing.

TC IDs covered:
    TC_BMC_0_0001 — I2C stress test (DC-ON state)
    TC_BMC_0_0002 — I2C stress test (DC-OFF state)

These tests require:
    - Real BMC reachable at --bmc-ip
    - The 'i2ctest' binary present at --i2ctest-binary path
    - config/i2c_devices.ini populated with your platform's I2C device map
      (template at config/i2c_devices_example.ini)
    - sys_conf.json with accurate hardware presence flags
    - Run with: pytest src/tests/test_bmc_i2c.py --integration

Why I2C stress tests run first (TC 0001/0002 have lowest TC numbers):
    I2C bus failures — stuck SCL, intermittent NACK, address collision,
    MUX channel not switching — produce intermittent failures across ALL
    other test categories. Running I2C stress first as a baseline confirms
    the bus infrastructure is healthy before any functional tests begin.

The i2ctest binary:
    Not a Python tool. It is a compiled binary that sends I2C transactions
    to the BMC via IPMI LAN (using the same lanplus credentials as ipmitool)
    and the BMC forwards them to the physical I2C buses via Master Write-Read.

    Command format:
        i2ctest -H <bmc_ip> -I lanplus -U admin -P <password>
                -F <ini_file> -L <log_file>
                -C <count_per_device> -R <loop_count>

    Each line in the INI file defines one I2C transaction. The tool runs
    each transaction C times per loop, R loops total, and logs results.

    Download or build i2ctest separately — it is not included in this
    repository because it is platform-specific compiled code.

INI file generation:
    The full device list is filtered by sys_conf.json to produce
    two test vectors:
        i2c_stress_test.ini         — DC-ON  (all present hardware)
        i2c_stress_test_dc_off.ini  — DC-OFF (standby-powered only)

    gen_i2ctest_ini() and gen_i2ctest_ini_dc_off() perform this filtering.
    The output files are written to the project root for i2ctest to read.

Log parsing:
    i2ctest writes a log file with per-transaction pass/fail results.
    parser_i2ctest_result() reads this log and returns the failure count.
"""

import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Additional CLI options for I2C stress tests
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    """I2C stress test specific options."""
    try:
        parser.addoption(
            '--i2ctest-binary',
            default = './i2ctest',
            help    = 'Path to i2ctest binary (default: ./i2ctest)',
        )
        parser.addoption(
            '--i2c-ini',
            default = 'config/i2c_devices.ini',
            help    = 'Path to full I2C device list INI file '
                      '(default: config/i2c_devices.ini)',
        )
        parser.addoption(
            '--i2c-count',
            default = 10,
            type    = int,
            help    = 'Transactions per device per loop (default: 10)',
        )
        parser.addoption(
            '--i2c-loops',
            default = 1,
            type    = int,
            help    = 'Number of complete test loops (default: 1)',
        )
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# INI file generation
# ---------------------------------------------------------------------------

# Device name tokens that appear in INI comment fields for DC-ON-only
# devices. Lines containing these tokens are excluded from the DC-OFF
# test vector because these devices require main DC power.
_DC_ON_ONLY_TOKENS = [
    'CLKBUF',      # clock buffers on main power domain
    'Retimer',     # PCIe retimers require 0.8V from VR
    'OCP',         # OCP NIC cards require PCIe slot power
    'PEX',         # PCIe switch token (matches 'PEX' in i2c_devices.ini comments)
    'DCDC',        # DCDC converter PMBus on main power
    'MB_HSC',      # hot-swap controller on main rail
    'OAM',         # accelerator modules on main DC
    'NVMe',        # NVMe SSDs require 3.3V main
]


def gen_i2ctest_ini(ini_source: str,
                    ini_output: str,
                    sys_conf:   dict) -> int:
    """
    Filter the full I2C device INI by sys_conf hardware presence.

    Lines are excluded if their comment field contains a device name
    token that maps to a sys_conf key with value 0 (absent hardware).

    Args:
        ini_source: path to full i2c_devices.ini
        ini_output: path to write filtered output
        sys_conf:   hardware presence bitmap

    Returns:
        number of lines written to output file
    """
    # build exclusion list: tokens for absent hardware
    excluded_tokens = []
    for key, present in sys_conf.items():
        if not present:
            # strip numeric suffix to get device class token
            # e.g. 'IOBU_OCP1' → check for 'IOBU_OCP' in comment
            token = re.sub(r'\d+$', '', key)
            excluded_tokens.append(token)

    lines_written = 0
    with open(ini_source) as src, open(ini_output, 'w') as dst:
        for line in src:
            stripped = line.strip()
            # always include blank lines and comment-only lines
            if not stripped or stripped.startswith('#'):
                dst.write(line)
                continue
            # exclude if any absent-device token appears in the line
            if any(token in line for token in excluded_tokens):
                continue
            dst.write(line)
            lines_written += 1

    return lines_written


def gen_i2ctest_ini_dc_off(ini_source: str,
                            ini_output: str,
                            sys_conf:   dict) -> int:
    """
    Generate DC-OFF test vector by additionally excluding devices
    that require main DC power.

    First applies gen_i2ctest_ini() hardware presence filter,
    then additionally removes DC-ON-only device lines.

    Args:
        ini_source: path to full i2c_devices.ini
        ini_output: path to write DC-OFF filtered output
        sys_conf:   hardware presence bitmap

    Returns:
        number of lines written to output file
    """
    # intermediate file — presence-filtered, not yet dc-filtered
    intermediate = ini_output.replace('.ini', '_intermediate.ini')

    gen_i2ctest_ini(ini_source, intermediate, sys_conf)

    lines_written = 0
    with open(intermediate) as src, open(ini_output, 'w') as dst:
        for line in src:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                dst.write(line)
                continue
            if any(token in line for token in _DC_ON_ONLY_TOKENS):
                continue
            dst.write(line)
            lines_written += 1

    # clean up intermediate file
    try:
        os.remove(intermediate)
    except OSError:
        pass

    return lines_written


# ---------------------------------------------------------------------------
# Log parser
# ---------------------------------------------------------------------------

def parser_i2ctest_result(log_path: str) -> tuple:
    """
    Parse i2ctest log file and return (result_code, failure_details).

    i2ctest log format:
        Loop 1 of 1
        Testing bus 1 addr 0xA8 ... PASS
        Testing bus 1 addr 0x40 ... FAIL: expected 0x55, got 0x00
        ...

    Args:
        log_path: path to i2ctest output log

    Returns:
        Tuple of (result_code, failure_list)
        result_code = 0 if no failures, -1 if any failures
        failure_list = list of failure line strings
    """
    failures = []
    loop_counts = {}

    with open(log_path) as f:
        current_loop = None
        for line in f:
            line = line.rstrip()
            if 'Loop' in line:
                current_loop = line
                loop_counts[current_loop] = 0
            elif 'Testing bus' in line:
                if 'FAIL' in line:
                    failures.append(line)
                    if current_loop:
                        loop_counts[current_loop] += 1

    total_loops    = len(loop_counts)
    total_failures = len(failures)

    result_code = 0 if total_failures == 0 else -1
    return result_code, failures, total_loops


# ---------------------------------------------------------------------------
# i2ctest runner
# ---------------------------------------------------------------------------

def run_i2ctest(bmc_credentials: dict,
                ini_file:        str,
                log_file:        str,
                binary_path:     str,
                count:           int,
                loops:           int) -> subprocess.CompletedProcess:
    """
    Execute the i2ctest binary as a subprocess.

    Args:
        bmc_credentials: dict with 'ip', 'user', 'password'
        ini_file:        path to filtered I2C test vector INI
        log_file:        path for i2ctest to write results
        binary_path:     path to i2ctest binary
        count:           transactions per device per loop
        loops:           number of complete loops

    Returns:
        CompletedProcess result from subprocess.run()
    """
    cmd = (
        f'{binary_path}'
        f' -H {bmc_credentials["ip"]}'
        f' -I lanplus'
        f' -U {bmc_credentials["user"]}'
        f' -P {bmc_credentials["password"]}'
        f' -F {ini_file}'
        f' -L {log_file}'
        f' -C {count}'
        f' -R {loops}'
    )
    return subprocess.run(
        shlex.split(cmd),
        capture_output = True,
        text           = True,
        timeout        = 600,   # 10 min max — large device lists
    )


# ---------------------------------------------------------------------------
# Helper: stop and restart BMC sensor polling
# ---------------------------------------------------------------------------

def _stop_sensor_polling(ipmi_client):
    """
    Stop BMC sensor polling before I2C stress test.

    BMC polling generates I2C traffic on its own schedule.
    Concurrent i2ctest + BMC polling = bus contention,
    which causes legitimate NACKs that look like test failures.
    Stopping polling gives i2ctest exclusive I2C bus access.
    """
    ipmi_client.run('raw', '0xNN', '0xNN', '0x00')  # replace with platform OEM command to stop sensor polling


def _start_sensor_polling(ipmi_client):
    """
    Restart BMC sensor polling after I2C stress test.

    MUST be called after i2ctest completes, otherwise SDR readings
    go stale and all subsequent sensor tests will see 'No Reading'.
    Called in finally block to guarantee restart.
    """
    ipmi_client.run('raw', '0xNN', '0xNN', '0x01')  # replace with platform OEM command to start sensor polling


# ---------------------------------------------------------------------------
# TC_BMC_0_0001 — I2C stress test DC-ON
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0001_i2c_stress_test_dc_on(
        ipmi_client, bmc_credentials, sys_conf, test_config, request):
    """
    TC_BMC_0_0001 — I2C bus stress test with host DC power ON.

    Test vector includes all I2C devices present per sys_conf.json.
    Host must be powered on and POST complete — some devices
    (retimers, OCP NICs, NVMe) are only accessible after DC power.

    Test sequence:
        1. Verify host is powered on and POST complete
        2. Generate filtered I2C test vector (i2c_stress_test.ini)
        3. Stop BMC sensor polling (prevents bus contention)
        4. Run i2ctest binary against all present devices
        5. Parse log for failures
        6. Restart BMC sensor polling (guaranteed via finally)

    Pass condition:
        Zero FAIL lines in i2ctest log across all devices and loops.

    Failure diagnosis:
        Single device FAIL: that device has a hardware issue
                            (bad solder joint, I2C address conflict)
        Multiple devices on same bus: bus-level issue (stuck SCL,
            missing pull-up resistor, MUX not routing correctly)
        All devices FAIL: IPMI LAN connection to BMC lost during test
    """
    is_authorized('TC_BMC_0_0001', test_config)

    binary = request.config.getoption('--i2ctest-binary')
    if not os.path.exists(binary):
        pytest.skip(
            f'i2ctest binary not found at {binary}. '
            f'Pass --i2ctest-binary <path> or place i2ctest in '
            f'the project root directory.'
        )

    ini_source = request.config.getoption('--i2c-ini')
    if not os.path.exists(ini_source):
        pytest.skip(
            f'I2C device INI not found at {ini_source}. '
            f'Copy config/i2c_devices_example.ini to {ini_source} '
            f'and populate it with your platform device map.'
        )

    count = request.config.getoption('--i2c-count')
    loops = request.config.getoption('--i2c-loops')

    # verify host is powered on and POST complete
    try:
        if not ipmi_client.get_power_state():
            pytest.skip(
                'Host is powered off. DC-ON I2C stress test requires '
                'host power to be asserted (some I2C devices are only '
                'accessible with main DC power on).'
            )
    except Exception as exc:
        pytest.skip(f'Cannot determine power state: {exc}')

    # generate filtered INI for DC-ON state
    ini_filtered = 'i2c_stress_test.ini'
    device_count = gen_i2ctest_ini(ini_source, ini_filtered, sys_conf)

    if device_count == 0:
        pytest.skip(
            f'No I2C devices to test after filtering by sys_conf. '
            f'Check that sys_conf.json has at least one device set to 1 '
            f'and that {ini_source} contains matching device entries.'
        )

    # timestamped log file
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    log_file  = f'i2c_test_dcon_{timestamp}.log'

    result_code = -1
    failures    = []

    try:
        _stop_sensor_polling(ipmi_client)
        time.sleep(5)  # allow in-flight polling transactions to complete

        proc = run_i2ctest(
            bmc_credentials = bmc_credentials,
            ini_file        = ini_filtered,
            log_file        = log_file,
            binary_path     = binary,
            count           = count,
            loops           = loops,
        )

        if proc.returncode != 0:
            pytest.fail(
                f'i2ctest binary exited with code {proc.returncode}. '
                f'stderr: {proc.stderr[:500]}'
            )

        result_code, failures, loop_count = parser_i2ctest_result(log_file)

    finally:
        _start_sensor_polling(ipmi_client)

    if failures:
        failure_summary = '\n'.join(failures[:20])  # show first 20
        if len(failures) > 20:
            failure_summary += f'\n... and {len(failures) - 20} more'
        pytest.fail(
            f'TC_BMC_0_0001 FAILED: {len(failures)} I2C failures '
            f'across {loop_count} loops:\n{failure_summary}\n'
            f'Log file: {log_file}'
        )


# ---------------------------------------------------------------------------
# TC_BMC_0_0002 — I2C stress test DC-OFF
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0002_i2c_stress_test_dc_off(
        ipmi_client, bmc_credentials, sys_conf, test_config, request):
    """
    TC_BMC_0_0002 — I2C bus stress test with host DC power OFF.

    Test vector excludes devices that require main DC power.
    This tests the standby power domain I2C bus integrity:
        - FRU EEPROMs (on 3.3V standby)
        - CPLDs in standby domain
        - BMC-local sensors
        - Voltage supervisor chips on standby rail

    DC-ON-only devices excluded:
        - Clock buffers (require main 3.3V)
        - PCIe retimers (require VR 0.8V)
        - OCP NIC cards (require PCIe slot power)
        - PCIe switches (require main 3.3V)
        - NVMe SSDs (require main 3.3V and 12V)

    Test sequence:
        1. Power off host (if not already off)
        2. Wait for power-off confirmation
        3. Generate DC-OFF filtered test vector
        4. Stop BMC sensor polling
        5. Run i2ctest
        6. Parse results
        7. Restart BMC sensor polling (guaranteed via finally)

    Note: this test powers the host OFF. If you need the host running
    for subsequent tests, power it back on after this test completes.
    """
    is_authorized('TC_BMC_0_0002', test_config)

    binary = request.config.getoption('--i2ctest-binary')
    if not os.path.exists(binary):
        pytest.skip(
            f'i2ctest binary not found at {binary}. '
            f'Pass --i2ctest-binary <path>.'
        )

    ini_source = request.config.getoption('--i2c-ini')
    if not os.path.exists(ini_source):
        pytest.skip(
            f'I2C device INI not found at {ini_source}.'
        )

    count = request.config.getoption('--i2c-count')
    loops = request.config.getoption('--i2c-loops')

    # power off host for DC-OFF test
    try:
        if ipmi_client.get_power_state():
            ipmi_client.set_power('off')
            # wait for power-off (max 30 seconds)
            for _ in range(30):
                time.sleep(1)
                if not ipmi_client.get_power_state():
                    break
            else:
                pytest.skip(
                    'Host did not power off within 30 seconds. '
                    'Cannot run DC-OFF I2C stress test.'
                )
    except Exception as exc:
        pytest.skip(f'Cannot control host power: {exc}')

    # generate DC-OFF filtered INI
    ini_filtered = 'i2c_stress_test_dc_off.ini'
    device_count = gen_i2ctest_ini_dc_off(
        ini_source, ini_filtered, sys_conf
    )

    if device_count == 0:
        pytest.skip(
            'No I2C devices remain after DC-OFF filtering. '
            'Check i2c_devices.ini has standby-accessible devices.'
        )

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    log_file  = f'i2c_test_dcoff_{timestamp}.log'

    result_code = -1
    failures    = []

    try:
        _stop_sensor_polling(ipmi_client)
        time.sleep(5)

        proc = run_i2ctest(
            bmc_credentials = bmc_credentials,
            ini_file        = ini_filtered,
            log_file        = log_file,
            binary_path     = binary,
            count           = count,
            loops           = loops,
        )

        if proc.returncode != 0:
            pytest.fail(
                f'i2ctest binary exited with code {proc.returncode}. '
                f'stderr: {proc.stderr[:500]}'
            )

        result_code, failures, loop_count = parser_i2ctest_result(log_file)

    finally:
        _start_sensor_polling(ipmi_client)

    if failures:
        failure_summary = '\n'.join(failures[:20])
        if len(failures) > 20:
            failure_summary += f'\n... and {len(failures) - 20} more'
        pytest.fail(
            f'TC_BMC_0_0002 FAILED: {len(failures)} I2C failures '
            f'across {loop_count} loops:\n{failure_summary}\n'
            f'Log file: {log_file}'
        )
