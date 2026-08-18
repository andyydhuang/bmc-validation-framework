"""
src/tests/test_transport_peci_client.py

Unit tests for src/transport/peci_client.py

Strategy:
    - PeciTempReading and PeciDib dataclasses: tested directly with
      raw bytes — pure Python, no I/O required.
    - PeciClient and SmlinkClient: tested via MockBmcClient which
      provides canned responses for PECI OEM commands.

Tests cover:
    - PeciTempReading.from_bytes(): parse 3-byte GetTemp response
    - PeciTempReading.celsius: temperature formula (Tjmax + raw/64.0)
    - PeciTempReading.is_valid: completion code check
    - PeciDib.from_bytes(): parse 8-byte DIB response
    - PeciDib.peci_revision: bits[7:4] extraction
    - PeciDib.is_valid(): revision 0x0 and 0x1 are valid
    - PeciClient.ping(): True on response, False on exception
    - PeciClient.get_temperature(): delegates to from_bytes correctly
    - PeciClient.verify_cpu(): full pass/fail scenarios
    - SmlinkClient.verify_node_manager(): Intel match and mismatch

Run with:
    python -m pytest src/tests/test_transport_peci_client.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

import struct
import pytest
from mock.mock_bmc import MockBmcClient
from src.protocol.sdr_parser import SdrCheckResult
from src.transport.peci_client import (
    PeciTempReading,
    PeciDib,
    PeciClient,
    SmlinkClient,
    PECI_ADDR_CPU0,
    PECI_ADDR_CPU1,
    PECI_CC_PASS,
    PECI_CC_ABORT,
    PECI_CC_ERROR,
)


# ---------------------------------------------------------------------------
# PeciTempReading.from_bytes() — 3-byte response parsing
# ---------------------------------------------------------------------------

class TestPeciTempReadingFromBytes:

    def test_valid_response_parses_completion_code(self):
        """Byte 0 = completion code."""
        raw   = bytes([PECI_CC_PASS, 0xC0, 0xFE])
        temp  = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.completion_code == PECI_CC_PASS

    def test_valid_response_parses_raw_value_little_endian(self):
        """
        Bytes 1-2 = signed 16-bit LE temperature.
        [0xC0, 0xFE] → struct.unpack('<h', b'\xC0\xFE') = -320
        """
        raw      = bytes([PECI_CC_PASS, 0xC0, 0xFE])
        temp     = PeciTempReading.from_bytes(raw, tjmax=105)
        raw_val, = struct.unpack('<h', bytes([0xC0, 0xFE]))
        assert temp.raw_value == raw_val  # -320

    def test_celsius_formula_correct(self):
        """
        Temperature = Tjmax + (raw_value / 64.0)
        raw = -320 → celsius = 105 + (-320/64) = 105 - 5 = 100.0 °C
        """
        raw  = bytes([PECI_CC_PASS, 0xC0, 0xFE])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.celsius == pytest.approx(100.0, abs=0.02)

    def test_celsius_at_tjmax_equals_tjmax(self):
        """raw_value = 0 → temperature exactly equals Tjmax."""
        raw  = bytes([PECI_CC_PASS, 0x00, 0x00])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.celsius == pytest.approx(105.0, abs=0.02)

    def test_celsius_well_below_tjmax(self):
        """Typical idle temperature: Tjmax=105, raw=-2560 → 65 °C."""
        raw_val = -2560   # 105 + (-2560/64) = 105 - 40 = 65
        packed  = struct.pack('<h', raw_val)
        raw     = bytes([PECI_CC_PASS]) + packed
        temp    = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.celsius == pytest.approx(65.0, abs=0.02)

    def test_is_valid_true_for_pass_completion_code(self):
        raw  = bytes([PECI_CC_PASS, 0xC0, 0xFE])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.is_valid is True

    def test_is_valid_false_for_abort_completion_code(self):
        """0x80 = CPU in deep C-state — reading is not valid."""
        raw  = bytes([PECI_CC_ABORT, 0x00, 0x00])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.is_valid is False

    def test_is_valid_false_for_error_completion_code(self):
        """0x90 = PECI bus error — reading is not valid."""
        raw  = bytes([PECI_CC_ERROR, 0x00, 0x00])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.is_valid is False

    def test_celsius_returns_none_when_not_valid(self):
        """celsius property returns None when completion code != PASS."""
        raw  = bytes([PECI_CC_ABORT, 0xC0, 0xFE])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        assert temp.celsius is None

    def test_too_short_raises_value_error(self):
        """Response shorter than 3 bytes raises ValueError."""
        with pytest.raises(ValueError, match='too short'):
            PeciTempReading.from_bytes(bytes([PECI_CC_PASS, 0xC0]),
                                       tjmax=105)

    def test_empty_bytes_raises_value_error(self):
        with pytest.raises(ValueError):
            PeciTempReading.from_bytes(bytes(), tjmax=105)

    def test_different_tjmax_changes_celsius(self):
        """Same raw bytes with different Tjmax gives different celsius."""
        raw      = bytes([PECI_CC_PASS, 0xC0, 0xFE])   # raw=-320
        temp_105 = PeciTempReading.from_bytes(raw, tjmax=105)
        temp_100 = PeciTempReading.from_bytes(raw, tjmax=100)
        assert temp_105.celsius == pytest.approx(100.0, abs=0.02)
        assert temp_100.celsius == pytest.approx(95.0,  abs=0.02)

    def test_str_contains_celsius_value_when_valid(self):
        raw  = bytes([PECI_CC_PASS, 0xC0, 0xFE])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        s    = str(temp)
        assert '100' in s

    def test_str_contains_invalid_when_not_valid(self):
        raw  = bytes([PECI_CC_ABORT, 0x00, 0x00])
        temp = PeciTempReading.from_bytes(raw, tjmax=105)
        s    = str(temp)
        assert 'INVALID' in s.upper()


# ---------------------------------------------------------------------------
# PeciDib.from_bytes() — 8-byte DIB parsing
# ---------------------------------------------------------------------------

class TestPeciDibFromBytes:

    def test_dev_info_byte_extracted(self):
        """Byte 0 = dev_info."""
        dib_bytes = bytes([0x10, 0x01, 0, 0, 0, 0, 0, 0])
        dib       = PeciDib.from_bytes(dib_bytes)
        assert dib.dev_info == 0x10

    def test_proc_num_byte_extracted(self):
        """Byte 1 = proc_num (number of processors at this PECI address)."""
        dib_bytes = bytes([0x10, 0x01, 0, 0, 0, 0, 0, 0])
        dib       = PeciDib.from_bytes(dib_bytes)
        assert dib.proc_num == 0x01

    def test_peci_revision_extracted_from_high_nibble(self):
        """
        PECI revision = bits[7:4] of dev_info byte.
        dev_info=0x10 → bits[7:4] = 0x1 (PECI 3.1)
        dev_info=0x00 → bits[7:4] = 0x0 (PECI 2.x)
        """
        dib_31 = PeciDib.from_bytes(bytes([0x10] + [0]*7))
        dib_2x = PeciDib.from_bytes(bytes([0x00] + [0]*7))
        assert dib_31.peci_revision == 0x1
        assert dib_2x.peci_revision == 0x0

    def test_is_valid_true_for_revision_0(self):
        """PECI 2.x (revision 0x0) is valid."""
        dib = PeciDib.from_bytes(bytes([0x00] + [0]*7))
        assert dib.is_valid() is True

    def test_is_valid_true_for_revision_1(self):
        """PECI 3.1 (revision 0x1) is valid."""
        dib = PeciDib.from_bytes(bytes([0x10] + [0]*7))
        assert dib.is_valid() is True

    def test_is_valid_false_for_unknown_revision(self):
        """Revision 0x2 or higher is unexpected."""
        dib = PeciDib.from_bytes(bytes([0x20] + [0]*7))
        assert dib.is_valid() is False

    def test_raw_bytes_preserved(self):
        """raw_bytes attribute stores all 8 bytes."""
        dib_bytes = bytes([0x10, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
        dib       = PeciDib.from_bytes(dib_bytes)
        assert dib.raw_bytes == dib_bytes

    def test_too_short_raises_value_error(self):
        """Fewer than 8 bytes raises ValueError."""
        with pytest.raises(ValueError, match='too short'):
            PeciDib.from_bytes(bytes([0x10, 0x01, 0x00]))

    def test_str_contains_revision(self):
        dib = PeciDib.from_bytes(bytes([0x10] + [0]*7))
        s   = str(dib)
        assert 'rev' in s.lower() or '1' in s


# ---------------------------------------------------------------------------
# PeciClient — using MockBmcClient
# ---------------------------------------------------------------------------

class TestPeciClient:

    @pytest.fixture
    def mock_bmc(self):
        return MockBmcClient()

    @pytest.fixture
    def peci(self, mock_bmc):
        return PeciClient(mock_bmc, tjmax=105)

    def test_ping_returns_true_when_mock_responds(self, peci):
        """
        MockBmcClient returns bytes([0x40]) for PECI ping command.
        PeciClient.ping() should return True when BMC responds.
        """
        result = peci.ping(PECI_ADDR_CPU0)
        assert result is True

    def test_ping_cpu1_address(self, peci):
        """Ping at CPU1 address 0x31 also returns True."""
        result = peci.ping(PECI_ADDR_CPU1)
        assert result is True

    def test_get_temperature_returns_peci_temp_reading(self, mock_bmc):
        """
        get_temperature() returns a PeciTempReading object.
        MockBmcClient returns empty bytes for unknown OEM commands —
        we provide a valid 3-byte PECI GetTemp response via override.
        """
        peci = PeciClient(mock_bmc, tjmax=105)
        valid_response = bytes([PECI_CC_PASS, 0xC0, 0xFE])
        # PeciClient uses self._ipmi.run() not run_raw — patch run()
        mock_bmc.run = lambda *args, **kwargs: '40 c0 fe'
        result = peci.get_temperature(PECI_ADDR_CPU0)
        assert isinstance(result, PeciTempReading)

    def test_get_dib_returns_peci_dib_or_none(self, peci):
        """get_dib() returns PeciDib or None — never raises."""
        result = peci.get_dib(PECI_ADDR_CPU0)
        assert result is None or isinstance(result, PeciDib)

    def test_verify_cpu_fails_when_temp_below_min(self, mock_bmc):
        """
        Temperature below -50°C should fail the range check.
        We configure the mock to return a very cold raw value.
        """
        peci = PeciClient(mock_bmc, tjmax=105)

        # override get_temperature to return sub-minimum reading
        raw_val   = -10000   # 105 + (-10000/64) ≈ -51.25 °C
        packed    = struct.pack('<h', raw_val)
        cold_resp = bytes([PECI_CC_PASS]) + packed
        cold_temp = PeciTempReading.from_bytes(cold_resp, tjmax=105)
        peci.get_temperature = lambda addr: cold_temp

        check = SdrCheckResult()
        peci.verify_cpu(cpu_index=0, check=check, temp_range=(-50.0, 105.0))
        assert check.result_code < 0
        assert any('below' in f.lower() or '-5' in f for f in check.failures)

    def test_verify_cpu_fails_when_temp_above_max(self, mock_bmc):
        """Temperature above Tjmax (105°C) should fail."""
        peci = PeciClient(mock_bmc, tjmax=105)

        # raw_value = 0 → celsius = Tjmax = 105 °C (exactly at max — borderline)
        # raw_value = 64 → would be Tjmax + 1 = 106°C but PECI can't go above Tjmax
        # simulate by overriding with a mock that returns 106°C
        hot_resp = bytes([PECI_CC_PASS, 0x40, 0x00])   # raw=64 → 105+1=106
        hot_temp = PeciTempReading.from_bytes(hot_resp, tjmax=105)
        peci.get_temperature = lambda addr: hot_temp

        check = SdrCheckResult()
        peci.verify_cpu(cpu_index=0, check=check, temp_range=(-50.0, 105.0))
        assert check.result_code < 0
        assert any('exceed' in f.lower() or 'maximum' in f.lower() or 'Tjmax' in f
                   for f in check.failures)

    def test_verify_cpu_fails_when_completion_code_not_pass(self, mock_bmc):
        """Abort completion code on GetTemp → failure."""
        peci = PeciClient(mock_bmc, tjmax=105)
        abort_temp = PeciTempReading.from_bytes(
            bytes([PECI_CC_ABORT, 0x00, 0x00]), tjmax=105
        )
        peci.get_temperature = lambda addr: abort_temp

        check = SdrCheckResult()
        peci.verify_cpu(cpu_index=0, check=check)
        assert check.result_code < 0
        assert any('completion' in f.lower() or '0x80' in f
                   for f in check.failures)


# ---------------------------------------------------------------------------
# SmlinkClient — Node Manager verification
# ---------------------------------------------------------------------------

class TestSmlinkClient:

    def _make_client_with_mc_info(self, mc_info_output: str) -> SmlinkClient:
        """Create SmlinkClient backed by mock that returns given mc info."""
        mock = MockBmcClient()
        client = SmlinkClient(mock)
        # override run_bridged to return controlled output
        client.run_bridged = lambda *args: mc_info_output
        return client

    def test_verify_node_manager_pass_with_intel(self):
        """'Intel Corporation' in Manufacturer Name → PASS."""
        mc_info = (
            "Device ID              : 1\n"
            "Firmware Revision      : 3.0\n"
            "Manufacturer Name      : Intel Corporation\n"
        )
        client = self._make_client_with_mc_info(mc_info)
        check  = SdrCheckResult()
        result = client.verify_node_manager(check)
        assert result is True
        assert check.result_code == 0

    def test_verify_node_manager_fail_with_wrong_manufacturer(self):
        """Non-Intel manufacturer → FAIL."""
        mc_info = (
            "Device ID              : 1\n"
            "Firmware Revision      : 3.0\n"
            "Manufacturer Name      : Unknown Vendor\n"
        )
        client = self._make_client_with_mc_info(mc_info)
        check  = SdrCheckResult()
        result = client.verify_node_manager(check)
        assert result is False
        assert check.result_code < 0
        assert any('manufacturer' in f.lower() or 'Intel' in f
                   for f in check.failures)

    def test_verify_node_manager_fail_when_empty_response(self):
        """Empty response (BMC not reachable) → RuntimeError → FAIL."""
        client = self._make_client_with_mc_info('')
        check  = SdrCheckResult()
        result = client.verify_node_manager(check)
        assert result is False
        assert check.result_code < 0

    def test_get_node_manager_info_parses_all_fields(self):
        """get_node_manager_info() returns dict with all colon-separated fields."""
        mc_info = (
            "Device ID              : 1\n"
            "Firmware Revision      : 3.0\n"
            "Manufacturer Name      : Intel Corporation\n"
            "Product ID             : 76\n"
        )
        client = self._make_client_with_mc_info(mc_info)
        info   = client.get_node_manager_info()
        assert info['Firmware Revision'] == '3.0'
        assert info['Manufacturer Name'] == 'Intel Corporation'
        assert info['Product ID']        == '76'

    def test_get_node_manager_info_raises_on_empty(self):
        """Empty response raises RuntimeError with diagnostic message."""
        client = self._make_client_with_mc_info('')
        with pytest.raises(RuntimeError, match='empty response'):
            client.get_node_manager_info()

    def test_smlink_bus_and_target_constants(self):
        """SMLINK_BUS and NM_IPMB_ADDR match Intel platform specification."""
        client = SmlinkClient(MockBmcClient())
        assert client.SMLINK_BUS    == '0x06'
        assert client.NM_IPMB_ADDR == '0x2c'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
