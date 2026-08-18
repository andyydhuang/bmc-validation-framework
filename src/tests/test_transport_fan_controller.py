"""
src/tests/test_transport_fan_controller.py

Unit tests for src/transport/fan_controller.py

Strategy:
    - FanRpmSpec and FanReading dataclasses: pure Python, tested directly.
    - FanController: tested via MockBmcClient with custom RPM responses.
    - FanSpeedVerifier: tested with FanController backed by MockBmcClient,
      and with a controlled measure_fan_at_duty override.

Tests cover:
    - FanRpmSpec.check_inlet / check_outlet: boundary conditions
    - FanReading.cpld_sdr_delta_pct: delta calculation, zero-division guard
    - FanTray: enum values and get_tray_for_fan mapping
    - FanController.get_tray_for_fan(): even=BOTTOM, odd=UPPER
    - FanController.get_presence(): bit decoding, active-low, ValueError
    - FanController.set_duty(): input validation, command format
    - FanController.read_tach_cpld(): byte parsing, ×60 conversion
    - FanController.read_tach_sdr(): sensor name lookup, RPM extraction
    - FanSpeedVerifier: try/finally auto-mode guarantee

Run with:
    python -m pytest src/tests/test_transport_fan_controller.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

from unittest.mock import MagicMock, patch, call
import pytest

from mock.mock_bmc import MockBmcClient
from src.protocol.sdr_parser import SdrCheckResult
from src.transport.fan_controller import (
    FanTray,
    FanRpmSpec,
    FanReading,
    FanController,
    FanControlError,
    FanSpeedVerifier,
    GENERIC_FAN_SPEC,
    _TRAY_CPLD_ADDR,
    _TACH_REG,
)


# ---------------------------------------------------------------------------
# FanRpmSpec — dataclass and check methods
# ---------------------------------------------------------------------------

class TestFanRpmSpec:

    @pytest.fixture
    def spec(self):
        return FanRpmSpec(
            duty_percent = 50,
            inlet_min    = 7335,
            inlet_max    = 8965,
            outlet_min   = 6570,
            outlet_max   = 8030,
        )

    def test_check_inlet_passes_within_range(self, spec):
        assert spec.check_inlet(8000) is True

    def test_check_inlet_passes_at_lower_bound(self, spec):
        assert spec.check_inlet(7335) is True

    def test_check_inlet_passes_at_upper_bound(self, spec):
        assert spec.check_inlet(8965) is True

    def test_check_inlet_fails_below_min(self, spec):
        assert spec.check_inlet(7334) is False

    def test_check_inlet_fails_above_max(self, spec):
        assert spec.check_inlet(8966) is False

    def test_check_outlet_passes_within_range(self, spec):
        assert spec.check_outlet(7000) is True

    def test_check_outlet_fails_below_min(self, spec):
        assert spec.check_outlet(6569) is False

    def test_check_outlet_fails_above_max(self, spec):
        assert spec.check_outlet(8031) is False

    def test_spec_is_frozen(self, spec):
        """FanRpmSpec is immutable — field modification must raise."""
        with pytest.raises(Exception):
            spec.duty_percent = 100

    def test_generic_fan_spec_has_three_entries(self):
        """GENERIC_FAN_SPEC covers 100%, 50%, and 10% duty points."""
        assert len(GENERIC_FAN_SPEC) == 3
        duties = [s.duty_percent for s in GENERIC_FAN_SPEC]
        assert 100 in duties
        assert 50  in duties
        assert 10  in duties


# ---------------------------------------------------------------------------
# FanReading — cpld_sdr_delta_pct
# ---------------------------------------------------------------------------

class TestFanReading:

    def test_delta_zero_when_cpld_equals_sdr(self):
        reading = FanReading(0, 50, 3600, 3300, 3600, 3300)
        inlet_d, outlet_d = reading.cpld_sdr_delta_pct()
        assert inlet_d  == pytest.approx(0.0)
        assert outlet_d == pytest.approx(0.0)

    def test_delta_calculated_as_percentage(self):
        """
        inlet_cpld=3600, inlet_sdr=3540
        delta = abs(3600-3540)/3600 * 100 = 60/3600*100 = 1.667%
        """
        reading = FanReading(0, 50, 3600, 3300, 3540, 3300)
        inlet_d, _ = reading.cpld_sdr_delta_pct()
        assert inlet_d == pytest.approx(1.667, abs=0.01)

    def test_delta_above_15_indicates_polling_lag(self):
        """16.7% delta exceeds the 15% warning threshold."""
        reading = FanReading(0, 50, 3600, 3300, 3000, 3300)
        inlet_d, _ = reading.cpld_sdr_delta_pct()
        assert inlet_d > 15.0

    def test_zero_cpld_value_uses_max_divisor_of_1(self):
        """max(0, 1) prevents ZeroDivisionError when CPLD reads 0."""
        reading = FanReading(0, 10, 0, 0, 0, 0)
        inlet_d, outlet_d = reading.cpld_sdr_delta_pct()
        assert inlet_d  == pytest.approx(0.0)
        assert outlet_d == pytest.approx(0.0)

    def test_both_deltas_returned_as_tuple(self):
        reading = FanReading(0, 50, 3600, 3300, 3540, 3200)
        result  = reading.cpld_sdr_delta_pct()
        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# FanTray — enum and CPLD address mapping
# ---------------------------------------------------------------------------

class TestFanTray:

    def test_bottom_value(self):
        assert FanTray.BOTTOM.value == 'bot'

    def test_upper_value(self):
        assert FanTray.UPPER.value == 'up'

    def test_tray_cpld_addr_bottom(self):
        assert _TRAY_CPLD_ADDR[FanTray.BOTTOM] == '0x40'

    def test_tray_cpld_addr_upper(self):
        assert _TRAY_CPLD_ADDR[FanTray.UPPER] == '0x42'

    def test_tach_reg_fan_pairs_share_register(self):
        """FAN0 and FAN1 share the same TACH register (same physical slot)."""
        assert _TACH_REG[0] == _TACH_REG[1]
        assert _TACH_REG[2] == _TACH_REG[3]
        assert _TACH_REG[4] == _TACH_REG[5]
        assert _TACH_REG[6] == _TACH_REG[7]

    def test_tach_reg_different_pairs_have_different_registers(self):
        """
        Different fan pairs use different TACH registers.

        NOTE: _TACH_REG values are '0xNN' placeholders in the published
        version. Replace with actual platform register addresses before
        running this test — at that point all four pairs should have
        distinct register values.
        """
        # skip structural check when values are placeholders
        if all(v == '0xNN' for v in _TACH_REG.values()):
            pytest.skip(
                '_TACH_REG contains 0xNN placeholders — replace with '
                'actual platform register addresses to enable this check.'
            )
        assert _TACH_REG[0] != _TACH_REG[2]
        assert _TACH_REG[2] != _TACH_REG[4]
        assert _TACH_REG[4] != _TACH_REG[6]


# ---------------------------------------------------------------------------
# FanController.get_tray_for_fan()
# ---------------------------------------------------------------------------

class TestFanControllerGetTray:

    @pytest.fixture
    def controller(self):
        return FanController(MockBmcClient())

    def test_even_fans_are_bottom_tray(self, controller):
        for fan_idx in [0, 2, 4, 6]:
            assert controller.get_tray_for_fan(fan_idx) == FanTray.BOTTOM

    def test_odd_fans_are_upper_tray(self, controller):
        for fan_idx in [1, 3, 5, 7]:
            assert controller.get_tray_for_fan(fan_idx) == FanTray.UPPER


# ---------------------------------------------------------------------------
# FanController.get_presence() — bit decoding
# ---------------------------------------------------------------------------

class TestFanControllerGetPresence:

    @pytest.fixture
    def controller(self):
        return FanController(MockBmcClient())

    def _mock_presence_response(self, controller, hex_byte: str):
        """Patch MockBmcClient.run to return a specific presence byte."""
        original_run = controller._ipmi.run
        def patched_run(*args):
            if '0x10' in args:
                return hex_byte
            return original_run(*args)
        controller._ipmi.run = patched_run

    def test_all_present_0x55(self, controller):
        """
        0x55 = 0b01010101 — all odd bits are 0 (active-low: 0=present).
        Should return [True, True, True, True].
        """
        self._mock_presence_response(controller, '55')
        result = controller.get_presence(FanTray.BOTTOM)
        assert result == [True, True, True, True]

    def test_all_absent_0xaa(self, controller):
        """
        0xAA = 0b10101010 — all odd bits are 1 (active-low: 1=absent).
        Should return [False, False, False, False].
        """
        self._mock_presence_response(controller, 'aa')
        result = controller.get_presence(FanTray.BOTTOM)
        assert result == [False, False, False, False]

    def test_first_pair_present_only(self, controller):
        """
        bit 1 = 0 (present), bits 3,5,7 = 1 (absent)
        0xAA | 0x02 ≠ target... let's compute:
        bit1=0, bit3=1, bit5=1, bit7=1 → 0b10101010 & ~0b00000010 = 0xA8
        0xA8 = 0b10101000
        """
        self._mock_presence_response(controller, 'a8')
        result = controller.get_presence(FanTray.BOTTOM)
        # bit 1 = 0 → pair 0 present; bits 3,5,7 all non-zero → absent
        assert result[0] is True
        assert result[1] is False
        assert result[2] is False
        assert result[3] is False

    def test_non_hex_response_raises_fan_control_error(self, controller):
        """Non-hex response from CPLD → FanControlError."""
        self._mock_presence_response(controller, 'Unable to send RAW command')
        with pytest.raises((FanControlError, ValueError)):
            controller.get_presence(FanTray.BOTTOM)

    def test_returns_list_of_four_booleans(self, controller):
        """get_presence() always returns exactly 4 elements."""
        self._mock_presence_response(controller, '55')
        result = controller.get_presence(FanTray.UPPER)
        assert len(result) == 4
        assert all(isinstance(v, bool) for v in result)


# ---------------------------------------------------------------------------
# FanController.set_duty() — input validation and command format
# ---------------------------------------------------------------------------

class TestFanControllerSetDuty:

    @pytest.fixture
    def controller(self):
        return FanController(MockBmcClient())

    def test_duty_100_accepted(self, controller):
        """100% duty is valid — no exception."""
        controller.set_duty(fan_index=0, duty_percent=100)

    def test_duty_0_accepted(self, controller):
        """0% duty is valid — no exception."""
        controller.set_duty(fan_index=0, duty_percent=0)

    def test_duty_50_accepted(self, controller):
        controller.set_duty(fan_index=3, duty_percent=50)

    def test_duty_above_100_raises_value_error(self, controller):
        with pytest.raises(ValueError, match='duty_percent must be 0-100'):
            controller.set_duty(fan_index=0, duty_percent=101)

    def test_duty_negative_raises_value_error(self, controller):
        with pytest.raises(ValueError):
            controller.set_duty(fan_index=0, duty_percent=-1)

    def test_set_duty_calls_ipmi_with_fan_index(self, controller):
        """Fan index is included in the IPMI command."""
        called_args = []
        original_run = controller._ipmi.run
        def capture(*args):
            called_args.extend(args)
            return ''
        controller._ipmi.run = capture
        controller.set_duty(fan_index=3, duty_percent=50)
        assert '0x03' in called_args or any('03' in str(a) for a in called_args)

    def test_set_duty_includes_oem_auth_bytes(self, controller):
        """OEM authentication bytes are included in the fan control command."""
        from src.transport.fan_controller import _OEM_AUTH_BYTES
        called_args = []
        controller._ipmi.run = lambda *args: called_args.extend(args) or ''
        controller.set_duty(fan_index=0, duty_percent=100)
        cmd_str = ' '.join(str(a) for a in called_args)
        # verify all three auth bytes appear in the command
        assert all(b in cmd_str for b in _OEM_AUTH_BYTES)


# ---------------------------------------------------------------------------
# FanController.read_tach_cpld() — byte parsing and ×60 conversion
# ---------------------------------------------------------------------------

class TestFanControllerReadTachCpld:

    @pytest.fixture
    def controller(self):
        return FanController(MockBmcClient())

    def _mock_tach_response(self, controller, response: str):
        """Patch run to return a specific TACH byte pair."""
        controller._ipmi.run = lambda *args: response

    def test_tach_bytes_multiplied_by_60(self, controller):
        """
        Response '3c 38':
        inlet  = int('3c', 16) * 60 = 60 * 60 = 3600 RPM
        outlet = int('38', 16) * 60 = 56 * 60 = 3360 RPM
        """
        self._mock_tach_response(controller, '3c 38')
        inlet, outlet = controller.read_tach_cpld(fan_index=0)
        assert inlet  == 3600
        assert outlet == 3360

    def test_zero_tach_returns_zero_rpm(self, controller):
        """0x00 bytes → 0 RPM (fan stalled)."""
        self._mock_tach_response(controller, '00 00')
        inlet, outlet = controller.read_tach_cpld(fan_index=0)
        assert inlet  == 0
        assert outlet == 0

    def test_max_byte_value_ff(self, controller):
        """0xFF → 255 * 60 = 15300 RPM."""
        self._mock_tach_response(controller, 'ff ff')
        inlet, outlet = controller.read_tach_cpld(fan_index=0)
        assert inlet  == 15300
        assert outlet == 15300

    def test_single_byte_response_raises_fan_control_error(self, controller):
        """Only one byte returned → FanControlError (need 2)."""
        self._mock_tach_response(controller, '3c')
        with pytest.raises(FanControlError, match='insufficient'):
            controller.read_tach_cpld(fan_index=0)

    def test_empty_response_raises_fan_control_error(self, controller):
        self._mock_tach_response(controller, '')
        with pytest.raises(FanControlError):
            controller.read_tach_cpld(fan_index=0)

    def test_fan1_uses_same_register_as_fan0(self, controller):
        """FAN0 and FAN1 share register 0x20 — both should work."""
        self._mock_tach_response(controller, '3c 38')
        inlet0, _ = controller.read_tach_cpld(fan_index=0)
        inlet1, _ = controller.read_tach_cpld(fan_index=1)
        assert inlet0 == inlet1 == 3600

    def test_returns_tuple_of_two_integers(self, controller):
        self._mock_tach_response(controller, '3c 38')
        result = controller.read_tach_cpld(fan_index=0)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(v, int) for v in result)


# ---------------------------------------------------------------------------
# FanController.read_tach_sdr() — sensor name lookup and RPM extraction
# ---------------------------------------------------------------------------

class TestFanControllerReadTachSdr:

    @pytest.fixture
    def controller(self):
        return FanController(MockBmcClient())

    _SDR_OUTPUT = (
        "Fan_SYS0_0 | 20h | ok | 7.1 | 3600 RPM\n"
        "Fan_SYS0_1 | 21h | ok | 7.1 | 3300 RPM\n"
        "Fan_SYS1_0 | 22h | ok | 7.2 | 3650 RPM\n"
        "Fan_SYS1_1 | 23h | ok | 7.2 | 3350 RPM\n"
    )

    def test_inlet_and_outlet_extracted_for_fan0(self, controller):
        inlet, outlet = controller.read_tach_sdr(0, self._SDR_OUTPUT)
        assert inlet  == 3600
        assert outlet == 3300

    def test_inlet_and_outlet_extracted_for_fan1(self, controller):
        """FAN1 uses sensors Fan_SYS1_0 and Fan_SYS1_1."""
        inlet, outlet = controller.read_tach_sdr(1, self._SDR_OUTPUT)
        assert inlet  == 3650
        assert outlet == 3350

    def test_missing_sensor_returns_zero(self, controller):
        """Fan index with no matching sensor returns 0."""
        inlet, outlet = controller.read_tach_sdr(7, self._SDR_OUTPUT)
        assert inlet  == 0
        assert outlet == 0

    def test_no_reading_sensor_returns_zero(self, controller):
        """'No Reading' sensor cannot be parsed → returns 0."""
        sdr = "Fan_SYS0_0 | 20h | ns | 7.1 | No Reading\n"
        inlet, _ = controller.read_tach_sdr(0, sdr)
        assert inlet == 0

    def test_extra_pipe_in_reading_field_handled(self, controller):
        """
        Extra pipe in reading field: '3600 RPM | extra' should still parse
        as 3600 (first token after split).
        """
        sdr = "Fan_SYS0_0 | 20h | ok | 7.1 | 3600 RPM | extra\n"
        inlet, _ = controller.read_tach_sdr(0, sdr)
        assert inlet == 3600

    def test_empty_sdr_output_returns_zero_for_both(self, controller):
        inlet, outlet = controller.read_tach_sdr(0, '')
        assert inlet  == 0
        assert outlet == 0


# ---------------------------------------------------------------------------
# FanSpeedVerifier — try/finally auto-mode guarantee
# ---------------------------------------------------------------------------

class TestFanSpeedVerifierAutoMode:

    def test_auto_mode_restored_after_normal_sweep(self):
        """
        Auto-mode must be re-enabled after a successful sweep.
        We track set_auto_mode calls via a list.
        """
        mock  = MockBmcClient()
        ctrl  = FanController(mock)
        calls = []

        original_set_auto = ctrl.set_auto_mode
        def tracking_set_auto(enabled):
            calls.append(enabled)
            # bypass actual IPMI call — just track
        ctrl.set_auto_mode = tracking_set_auto

        # presence: all fans absent (skips all measurement calls)
        ctrl.get_presence = lambda tray: [False, False, False, False]

        verifier = FanSpeedVerifier(ctrl, GENERIC_FAN_SPEC)
        check    = SdrCheckResult()
        verifier.run_full_sweep(check, sdr_fetch_fn=lambda: '')

        # auto-mode should have been disabled then re-enabled
        assert False in calls   # disabled at start
        assert True  in calls   # re-enabled at end
        assert calls[-1] is True  # last call is always re-enable

    def test_auto_mode_restored_even_when_exception_occurs(self):
        """
        try/finally guarantees auto-mode is restored even when
        an exception is raised inside the sweep loop.
        """
        mock  = MockBmcClient()
        ctrl  = FanController(mock)
        calls = []

        ctrl.set_auto_mode = lambda enabled: calls.append(enabled)
        ctrl.get_presence  = lambda tray: [False, False, False, False]

        # Force an exception inside the try block
        ctrl.set_duty = MagicMock(side_effect=RuntimeError('simulated crash'))

        verifier = FanSpeedVerifier(ctrl, GENERIC_FAN_SPEC)
        check    = SdrCheckResult()

        # The exception propagates — but finally still runs
        # get_presence returns all False so set_duty never called
        # Test with at least one fan present to trigger the crash
        ctrl.get_presence = lambda tray: [True, False, False, False]

        with pytest.raises(RuntimeError, match='simulated crash'):
            verifier.run_full_sweep(check, sdr_fetch_fn=lambda: '')

        # Auto-mode must have been re-enabled in finally
        assert True in calls, (
            'Auto-mode was not restored after exception. '
            'The try/finally block is missing or broken.'
        )
        assert calls[-1] is True

    def test_verify_reading_ok_when_all_in_spec(self):
        """_verify_reading records ok for both paths when in spec."""
        mock  = MockBmcClient()
        ctrl  = FanController(mock)
        verifier = FanSpeedVerifier(ctrl, GENERIC_FAN_SPEC)

        spec = GENERIC_FAN_SPEC[1]   # 50% duty
        reading = FanReading(
            fan_index    = 0,
            duty_percent = 50,
            inlet_cpld   = 8000,   # within 7335-8965
            outlet_cpld  = 7000,   # within 6570-8030
            inlet_sdr    = 8000,
            outlet_sdr   = 7000,
        )
        check = SdrCheckResult()
        verifier._verify_reading(reading, spec, check)
        assert check.result_code == 0
        assert check.passed == 4   # 2 CPLD + 2 SDR

    def test_verify_reading_fail_when_cpld_out_of_spec(self):
        """CPLD reading below spec → check.fail() called."""
        mock  = MockBmcClient()
        ctrl  = FanController(mock)
        verifier = FanSpeedVerifier(ctrl, GENERIC_FAN_SPEC)

        spec = GENERIC_FAN_SPEC[1]   # 50% duty, inlet min=7335
        reading = FanReading(
            fan_index    = 0,
            duty_percent = 50,
            inlet_cpld   = 3000,   # BELOW 7335 — fail
            outlet_cpld  = 7000,
            inlet_sdr    = 8000,
            outlet_sdr   = 7000,
        )
        check = SdrCheckResult()
        verifier._verify_reading(reading, spec, check)
        assert check.result_code < 0
        assert any('INLET' in f and 'CPLD' in f for f in check.failures)

    def test_verify_reading_warn_when_cpld_sdr_delta_exceeds_15pct(self):
        """Large CPLD/SDR delta → check.warn() (not fail)."""
        mock  = MockBmcClient()
        ctrl  = FanController(mock)
        verifier = FanSpeedVerifier(ctrl, GENERIC_FAN_SPEC)

        spec = GENERIC_FAN_SPEC[0]   # 100% duty
        reading = FanReading(
            fan_index    = 0,
            duty_percent = 100,
            inlet_cpld   = 14000,   # in spec
            outlet_cpld  = 12000,   # in spec
            inlet_sdr    = 11500,   # delta = (14000-11500)/14000 = 17.9% > 15%
            outlet_sdr   = 12000,
        )
        check = SdrCheckResult()
        verifier._verify_reading(reading, spec, check)
        # result_code should be 0 (no hard failures from delta alone)
        # but warnings should be recorded
        assert len(check.warnings) > 0
        assert any('delta' in w.lower() or 'lag' in w.lower()
                   for w in check.warnings)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
