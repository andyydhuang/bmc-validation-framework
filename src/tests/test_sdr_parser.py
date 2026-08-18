"""
src/tests/test_sdr_parser.py

Unit tests for src/protocol/sdr_parser.py

All tests run without hardware — the MockBmcClient provides canned
SDR output that exercises every code path in the parser.

Test categories:
    1. SdrEntry.from_line() — line parsing correctness and edge cases
    2. SdrEntry.parse_all() — full output parsing and dictionary build
    3. SdrCheckResult       — accumulator correctness
    4. verify_cpu_sdr()     — bidirectional CPU presence verification
    5. verify_psu_sdr()     — PSU presence and reading verification

Run with:
    python -m pytest src/tests/test_sdr_parser.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

import pytest
from src.protocol.sdr_parser import (
    SdrEntry,
    SdrCheckResult,
    PresenceState,
    verify_cpu_sdr,
    verify_psu_sdr,
)


# ---------------------------------------------------------------------------
# SdrEntry.from_line() tests
# ---------------------------------------------------------------------------

class TestSdrEntryFromLine:
    """Tests for the line parser — the foundation of all SDR verification."""

    def test_standard_five_field_line(self):
        """Normal pipe-delimited line with five fields parses correctly."""
        line  = "Temp_CPU0        | A7h | ok  |  3.1 | 52 degrees C"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert entry.name      == 'Temp_CPU0'
        assert entry.sensor_id == 'A7h'
        assert entry.status    == 'ok'
        assert entry.entity    == '3.1'
        assert entry.reading   == '52 degrees C'

    def test_extra_pipe_in_reading_field(self):
        """
        BMC firmware that adds extra pipe chars in reading field must not
        corrupt the parser — the reading field may itself contain
        pipe characters (e.g. "Presence detected | AC OK").
        Parsing uses '|'.join(parts[4:]) to join everything
        after the fourth field rather than splitting on the first pipe.
        The extra pipe caused 'Power Supply AC lost' to never be found
        in field[4], producing false passes on AC-lost PSUs.
        """
        line  = "PSU0_Status | C0h | ok | 8.1 | Presence detected | AC OK"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert entry.name    == 'PSU0_Status'
        assert entry.reading == 'Presence detected | AC OK'
        # critical: full reading preserved, not truncated at second pipe
        assert 'Presence detected' in entry.reading
        assert 'AC OK'             in entry.reading

    def test_no_reading_status(self):
        """Sensors for absent hardware correctly parse 'No Reading'."""
        line  = "Temp_CPU1  | A8h | ns  |  3.2 | No Reading"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert entry.status  == 'ns'
        assert entry.reading == 'No Reading'

    def test_disabled_voltage_sensor(self):
        """VR voltage sensors for absent CPUs report 'Disabled'."""
        line  = "Vol_PVCCIN_CPU1 | 21h | ns | 3.2 | Disabled"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert entry.reading == 'Disabled'

    def test_malformed_line_fewer_than_five_fields_returns_none(self):
        """
        Lines with fewer than 5 pipe-delimited fields return None.
        This prevents IndexError when accessing entry.reading later.
        Without a length guard — accessing stripped[4]
        on a short line would raise IndexError and the except block
        would set result=0 (PASS), producing a silent false positive.
        """
        line   = "incomplete_line | only | three"
        entry  = SdrEntry.from_line(line)
        assert entry is None

    def test_empty_line_returns_none(self):
        """Empty lines (common at start/end of ipmitool output) are skipped."""
        assert SdrEntry.from_line('')    is None
        assert SdrEntry.from_line('   ') is None

    def test_leading_and_trailing_whitespace_stripped(self):
        """ipmitool pads sensor names and values with spaces — all stripped."""
        line  = "  Fan_SYS0_0   |   20h   |   ok   |   7.1   |   3600 RPM  "
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert entry.name    == 'Fan_SYS0_0'
        assert entry.reading == '3600 RPM'

    def test_rpm_reading_parses_correctly(self):
        """Fan RPM readings used in fan speed verification tests."""
        line  = "Fan_SYS0_0 | 20h | ok | 7.1 | 3600 RPM"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        rpm_value = int(entry.reading.split()[0])
        assert rpm_value == 3600

    def test_degrees_reading_parses_correctly(self):
        """Temperature readings used in CPU/DIMM/PSU verification."""
        line  = "Temp_CPU0 | A7h | ok | 3.1 | 52 degrees C"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        temp_value = int(entry.reading.split()[0])
        assert temp_value == 52

    def test_presence_detected_reading(self):
        """Presence sensors — basis of bidirectional verification."""
        line  = "CPU0 | A7h | ok | 3.1 | Presence detected"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert 'Presence detected' in entry.reading

    def test_drive_present_reading(self):
        """NVMe SSD and other storage presence sensors."""
        line  = "NVMeSSD_3 | 33h | ok | 4.4 | Drive Present"
        entry = SdrEntry.from_line(line)

        assert entry is not None
        assert entry.reading == 'Drive Present'


# ---------------------------------------------------------------------------
# SdrEntry.parse_all() tests
# ---------------------------------------------------------------------------

class TestSdrEntryParseAll:
    """Tests for full SDR output parsing into a dictionary."""

    def test_parse_multi_line_output(self):
        """Standard multi-line ipmitool output builds correct dictionary."""
        raw = (
            "CPU0         | A7h | ok  |  3.1 | Presence detected\n"
            "Temp_CPU0    | 10h | ok  |  3.1 | 52 degrees C\n"
            "Fan_SYS0_0   | 20h | ok  |  7.1 | 3600 RPM\n"
        )
        sdrs = SdrEntry.parse_all(raw)

        assert len(sdrs) == 3
        assert 'CPU0'       in sdrs
        assert 'Temp_CPU0'  in sdrs
        assert 'Fan_SYS0_0' in sdrs

    def test_dictionary_key_is_sensor_name(self):
        """O(1) lookup by sensor name works correctly."""
        raw = "Temp_CPU0 | A7h | ok | 3.1 | 52 degrees C\n"
        sdrs = SdrEntry.parse_all(raw)

        entry = sdrs.get('Temp_CPU0')
        assert entry is not None
        assert entry.reading == '52 degrees C'

    def test_malformed_lines_skipped_cleanly(self):
        """Malformed lines do not crash parse_all — they are silently skipped."""
        raw = (
            "good_sensor | A7h | ok | 3.1 | value\n"
            "bad_line_with_no_pipes\n"
            "another_good | B0h | ok | 4.1 | reading\n"
        )
        sdrs = SdrEntry.parse_all(raw)

        # only the two valid lines should be in the dict
        assert 'good_sensor'  in sdrs
        assert 'another_good' in sdrs
        assert len(sdrs) == 2

    def test_empty_output_returns_empty_dict(self):
        """Empty string (e.g. BMC not responding) returns empty dict."""
        sdrs = SdrEntry.parse_all('')
        assert sdrs == {}

    def test_extra_pipe_handled_in_full_parse(self):
        """Extra pipes in reading field handled correctly in parse_all."""
        raw = (
            "PSU0_Status | C0h | ok | 8.1 | Presence detected | AC OK\n"
        )
        sdrs  = SdrEntry.parse_all(raw)
        entry = sdrs['PSU0_Status']

        assert entry.reading == 'Presence detected | AC OK'
        # the critical check: 'Power Supply AC lost' detection still works
        # because the full reading is preserved
        assert 'Presence detected' in entry.reading


# ---------------------------------------------------------------------------
# SdrCheckResult tests
# ---------------------------------------------------------------------------

class TestSdrCheckResult:
    """Tests for the pass/fail accumulator."""

    def test_initial_state(self):
        """Fresh SdrCheckResult has zero passes and no failures."""
        check = SdrCheckResult()
        assert check.passed         == 0
        assert check.failures       == []
        assert check.warnings       == []
        assert check.result_code    == 0

    def test_ok_increments_passed(self):
        """check.ok() increments passed counter."""
        check = SdrCheckResult()
        check.ok('test passed')
        assert check.passed      == 1
        assert check.result_code == 0

    def test_fail_adds_to_failures(self):
        """check.fail() adds message to failures list."""
        check = SdrCheckResult()
        check.fail('something broke')
        assert len(check.failures) == 1
        assert 'something broke'   in check.failures[0]
        assert check.result_code   == -1

    def test_multiple_failures_counted_correctly(self):
        """result_code equals negative number of failures."""
        check = SdrCheckResult()
        check.fail('failure 1')
        check.fail('failure 2')
        check.fail('failure 3')
        assert check.result_code == -3

    def test_warn_does_not_increment_result_code(self):
        """Warnings are recorded but do not count as failures."""
        check = SdrCheckResult()
        check.warn('something suspicious')
        assert check.result_code  == 0
        assert len(check.warnings) == 1

    def test_summary_includes_all_failures(self):
        """summary() lists all failure messages."""
        check = SdrCheckResult()
        check.ok('thing 1 passed')
        check.fail('thing 2 broke')
        check.fail('thing 3 broke')

        summary = check.summary()
        assert 'thing 2 broke' in summary
        assert 'thing 3 broke' in summary
        assert '1 passed'      in summary
        assert '2 failed'      in summary


# ---------------------------------------------------------------------------
# verify_cpu_sdr() tests — bidirectional verification
# ---------------------------------------------------------------------------

class TestVerifyCpuSdr:
    """
    Tests for bidirectional CPU sensor verification.

    These tests verify both failure directions:
        Direction 1: CPU in sys_conf → must appear in SDR (false negative)
        Direction 2: CPU absent in sys_conf → must NOT appear in SDR (false positive)
    """

    # Standard SDR output covering both CPU presence and reading sensors
    _BOTH_CPUS_SDR = (
        "CPU0             | A7h | ok  |  3.1 | Presence detected\n"
        "CPU1             | A8h | ok  |  3.2 | Presence detected\n"
        "Temp_CPU0        | 10h | ok  |  3.1 | 52 degrees C\n"
        "Temp_CPU0_VR     | 11h | ok  |  3.1 | 48 degrees C\n"
        "Temp_CPU1        | 12h | ok  |  3.2 | 48 degrees C\n"
        "Vol_PVCCIN_CPU0  | 20h | ok  |  3.1 | 1.8 Volts\n"
        "Vol_PVCCIN_CPU1  | 21h | ok  |  3.2 | 1.8 Volts\n"
        "T_CPU_Highest    | 30h | ok  |  3.1 | 52 degrees C\n"
        "Power_CPU        | 31h | ok  |  3.1 | 150 Watts\n"
    )

    _CPU_SDRS = frozenset([
        'Temp_CPU0', 'Temp_CPU0_VR', 'Temp_CPU1',
        'Vol_PVCCIN_CPU0', 'Vol_PVCCIN_CPU1',
        'T_CPU_Highest', 'Power_CPU',
    ])

    def test_both_cpus_present_and_match_sys_conf(self):
        """
        Both CPUs installed and detected — all checks should pass.
        This is the normal production state.
        """
        sys_conf = {'CPU0': 1, 'CPU1': 1}

        check = verify_cpu_sdr(
            self._BOTH_CPUS_SDR,
            power_state = 2,
            sys_conf    = sys_conf,
            cpu_sdrs    = self._CPU_SDRS,
        )

        assert check.result_code == 0, check.summary()
        assert check.passed > 0

    def test_power_state_not_2_fails_with_clear_message(self):
        """
        CPU sensors are only valid after BIOS POST (power_state=2).
        Calling before POST produces a fail with an actionable message,
        not a silent false result from uninitialized sensor values.
        """
        sys_conf = {'CPU0': 1, 'CPU1': 1}

        check = verify_cpu_sdr(
            self._BOTH_CPUS_SDR,
            power_state = 1,   # DC on but POST not complete
            sys_conf    = sys_conf,
            cpu_sdrs    = self._CPU_SDRS,
        )

        assert check.result_code < 0
        assert any('POST' in f for f in check.failures)

    def test_phantom_cpu_detected_by_bmc_but_absent_in_config(self):
        """
        BMC reports CPU1 present but sys_conf says CPU1 is absent.
        This is the phantom sensor case — a BMC firmware bug.
        Direction 1: populated slots must have valid readings.
        Direction 2: empty slots must not produce readings.
        This test verifies direction 2 — without it, phantom
        sensors on empty slots would go undetected.
        """
        sys_conf = {
            'CPU0': 1,
            'CPU1': 0,   # CPU1 not installed
        }

        sdr = (
            "CPU0 | A7h | ok | 3.1 | Presence detected\n"
            "CPU1 | A8h | ok | 3.2 | Presence detected\n"  # phantom
            "Temp_CPU0 | 10h | ok | 3.1 | 52 degrees C\n"
            "Temp_CPU1 | 12h | ns | 3.2 | No Reading\n"
        )

        check = verify_cpu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            cpu_sdrs    = frozenset(['Temp_CPU0', 'Temp_CPU1']),
        )

        assert check.result_code < 0, (
            'Should have failed: CPU1 is phantom (sys_conf=0 but BMC detects it)'
        )
        assert any('phantom' in f.lower() or 'sys_conf=0' in f
                   for f in check.failures)

    def test_missing_cpu_not_detected_by_bmc(self):
        """
        sys_conf says CPU1 should be installed but BMC does not detect it.
        This catches hardware installation errors or BMC detection failures.
        """
        sys_conf = {
            'CPU0': 1,
            'CPU1': 1,   # CPU1 should be here
        }

        sdr = (
            "CPU0 | A7h | ok | 3.1 | Presence detected\n"
            "CPU1 | A8h | ns | 3.2 | No Reading\n"   # not detected
        )

        check = verify_cpu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            cpu_sdrs    = frozenset(),
        )

        assert check.result_code < 0
        assert any('not detected' in f.lower() or 'UNEXPECTED_ABSENT' in f
                   or 'sys_conf=1' in f
                   for f in check.failures)

    def test_both_cpus_absent_and_correctly_no_reading(self):
        """
        Both CPUs absent (development unit with no CPUs).
        All sensors correctly show No Reading or Disabled.
        """
        sys_conf = {'CPU0': 0, 'CPU1': 0}

        sdr = (
            "CPU0             | A7h | ns | 3.1 | No Reading\n"
            "CPU1             | A8h | ns | 3.2 | No Reading\n"
            "Temp_CPU0        | 10h | ns | 3.1 | No Reading\n"
            "Temp_CPU1        | 12h | ns | 3.2 | No Reading\n"
            "Vol_PVCCIN_CPU0  | 20h | ns | 3.1 | Disabled\n"
            "Vol_PVCCIN_CPU1  | 21h | ns | 3.2 | Disabled\n"
            "T_CPU_Highest    | 30h | ns | 3.1 | No Reading\n"
            "Power_CPU        | 31h | ns | 3.1 | No Reading\n"
        )

        check = verify_cpu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            cpu_sdrs    = frozenset([
                'Temp_CPU0', 'Temp_CPU1',
                'Vol_PVCCIN_CPU0', 'Vol_PVCCIN_CPU1',
                'T_CPU_Highest', 'Power_CPU',
            ]),
        )

        assert check.result_code == 0, check.summary()

    def test_cpu_present_but_temp_sensor_no_reading(self):
        """
        CPU is physically installed and detected, but temperature sensor
        reports No Reading. This catches thermal subsystem initialization
        failures — a real failure mode separate from CPU presence.
        """
        sys_conf = {'CPU0': 1, 'CPU1': 0}

        sdr = (
            "CPU0      | A7h | ok | 3.1 | Presence detected\n"
            "Temp_CPU0 | 10h | ns | 3.1 | No Reading\n"
        )

        check = verify_cpu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            cpu_sdrs    = frozenset(['Temp_CPU0']),
        )

        assert check.result_code < 0
        assert any('uninitialized' in f.lower() or 'No Reading' in f
                   for f in check.failures)

    def test_extra_pipe_in_psu_reading_does_not_break_cpu_check(self):
        """
        Extra pipe chars in PSU reading field (unrelated sensor) should
        not affect CPU sensor parsing. Tests parser isolation.
        """
        sys_conf = {'CPU0': 1, 'CPU1': 0}

        sdr = (
            "PSU0_Status | C0h | ok | 8.1 | Presence detected | AC OK\n"
            "CPU0        | A7h | ok | 3.1 | Presence detected\n"
            "Temp_CPU0   | 10h | ok | 3.1 | 52 degrees C\n"
        )

        check = verify_cpu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            cpu_sdrs    = frozenset(['Temp_CPU0']),
        )

        assert check.result_code == 0, check.summary()


# ---------------------------------------------------------------------------
# verify_psu_sdr() tests
# ---------------------------------------------------------------------------

class TestVerifyPsuSdr:
    """Tests for PSU sensor verification including AC-lost state."""

    _PSU_SDRS = frozenset([
        'PSU0_Status', 'Fan_PSU0_0', 'PSU0_Current', 'PSU0_Input',
        'Temp_PSU0_Inlet',
    ])

    def test_psu_present_ac_supplied_all_readings_valid(self):
        """PSU installed with AC power — all readings should be non-zero."""
        sys_conf = {'PSU0': 1, 'PSU1': 0, 'PSU2': 0,
                    'PSU3': 0, 'PSU4': 0, 'PSU5': 0}

        sdr = (
            "PSU0_Status   | C0h | ok | 8.1 | Presence detected\n"
            "Fan_PSU0_0    | C1h | ok | 8.1 | 3200 RPM\n"
            "PSU0_Current  | C2h | ok | 8.1 | 12 Amps\n"
            "PSU0_Input    | C3h | ok | 8.1 | 400 Watts\n"
        )

        check = verify_psu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            psu_sdrs    = frozenset([
                'PSU0_Status', 'Fan_PSU0_0', 'PSU0_Current', 'PSU0_Input'
            ]),
        )

        assert check.result_code == 0, check.summary()

    def test_psu_present_ac_lost_zero_readings_acceptable(self):
        """
        PSU installed but AC power lost — 0 RPM and 0 Watts are EXPECTED.
        A parser without this fix would fail this case if the reading field
        was split by extra pipe chars — this tests that 'Power Supply
        AC lost' is correctly detected in the full reading string.
        """
        sys_conf = {'PSU0': 1, 'PSU1': 0, 'PSU2': 0,
                    'PSU3': 0, 'PSU4': 0, 'PSU5': 0}

        sdr = (
            "PSU0_Status  | C0h | ok | 8.1 | Presence detected | Power Supply AC lost\n"
            "Fan_PSU0_0   | C1h | ns | 8.1 | 0 RPM\n"
            "PSU0_Current | C2h | ns | 8.1 | 0 Amps\n"
            "PSU0_Input   | C3h | ns | 8.1 | 0 Watts\n"
        )

        check = verify_psu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            psu_sdrs    = frozenset([
                'PSU0_Status', 'Fan_PSU0_0', 'PSU0_Current', 'PSU0_Input'
            ]),
        )

        assert check.result_code == 0, (
            f'AC-lost PSU with 0 RPM/Amps/Watts should PASS: {check.summary()}'
        )

    def test_phantom_psu_detected_by_bmc_not_in_config(self):
        """
        BMC detects PSU0 but sys_conf says PSU0 is absent.
        Classic false positive — bidirectional check catches it.
        """
        sys_conf = {'PSU0': 0, 'PSU1': 0, 'PSU2': 0,
                    'PSU3': 0, 'PSU4': 0, 'PSU5': 0}

        sdr = "PSU0_Status | C0h | ok | 8.1 | Presence detected\n"

        check = verify_psu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            psu_sdrs    = frozenset(['PSU0_Status']),
        )

        assert check.result_code < 0
        assert any('phantom' in f.lower() or 'sys_conf=0' in f
                   for f in check.failures)

    def test_psu_absent_no_reading_is_pass(self):
        """
        PSU slot empty — No Reading on all sensors is correct behavior.
        Verifies the CONFIRMED_ABSENT → No Reading path.
        """
        sys_conf = {'PSU0': 0, 'PSU1': 0, 'PSU2': 0,
                    'PSU3': 0, 'PSU4': 0, 'PSU5': 0}

        sdr = "PSU0_Status | C0h | ns | 8.1 | No Reading\n"

        check = verify_psu_sdr(
            sdr,
            power_state = 2,
            sys_conf    = sys_conf,
            psu_sdrs    = frozenset(['PSU0_Status']),
        )

        assert check.result_code == 0, check.summary()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
