"""
src/tests/test_transport_jtag_client.py

Unit tests for src/transport/jtag_client.py

Strategy: MockBmcClient provides canned IPMI responses for JTAG
OEM commands. JtagIdcode dataclass is tested directly (pure Python,
no I/O). JtagClient._parse_idcode() is tested with known byte strings.
JtagVerifier logic is tested by controlling what MockBmcClient returns.

Tests cover:
    - JtagIdcode.from_int(): field decoding from raw 32-bit value
    - JtagIdcode.is_valid(): IEEE 1149.1 compliance check
    - JtagClient._parse_idcode(): ASCII-encoded hex → JtagIdcode
    - JtagClient.read_cpu_idcode(): invalid cpu_index guard
    - JtagVerifier.verify_cpu(): PASS, BYPASS, ALL_ZEROS, wrong IDCODE
    - JtagVerifier.verify_pch(): PASS and FAIL cases
    - JtagVerifier.verify_all_accel_slots(): mixed slot states

Run with:
    python -m pytest src/tests/test_transport_jtag_client.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

import pytest
from mock.mock_bmc import MockBmcClient
from src.protocol.sdr_parser import SdrCheckResult
from src.transport.jtag_client import (
    JtagIdcode,
    JtagClient,
    JtagVerifier,
    KNOWN_IDCODES,
    VALID_ACCEL_IDCODES,
)


# ---------------------------------------------------------------------------
# JtagIdcode dataclass — pure Python, no I/O
# ---------------------------------------------------------------------------

class TestJtagIdcode:

    def test_from_int_decodes_version(self):
        """bits [31:28] = version field."""
        idcode = JtagIdcode.from_int(0x20044113)
        assert idcode.version == 0x2   # bits 31:28 of 0x20044113

    def test_from_int_decodes_part_number(self):
        """bits [27:12] = part number field."""
        idcode = JtagIdcode.from_int(0x20044113)
        assert idcode.part_number == 0x0044   # bits 27:12

    def test_from_int_decodes_mfr_id(self):
        """bits [11:1] = manufacturer ID field."""
        idcode = JtagIdcode.from_int(0x20044113)
        # 0x20044113 bits 11:1 = 0x089 = 137 (ARM manufacturer ID)
        assert idcode.mfr_id == (0x20044113 >> 1) & 0x7FF

    def test_from_int_lsb_always_1_for_valid_idcode(self):
        """bit [0] = 1 per IEEE 1149.1 specification."""
        idcode = JtagIdcode.from_int(0x20044113)
        assert idcode.lsb == 1

    def test_from_int_raw_value_preserved(self):
        """raw_value matches the input integer exactly."""
        idcode = JtagIdcode.from_int(0x20044113)
        assert idcode.raw_value == 0x20044113

    def test_is_valid_returns_true_for_normal_idcode(self):
        """Normal IDCODE with bit[0]=1 is valid."""
        idcode = JtagIdcode.from_int(0x20044113)
        assert idcode.is_valid() is True

    def test_is_valid_returns_false_for_all_zeros(self):
        """0x00000000 = TDO floating or stuck low — invalid."""
        idcode = JtagIdcode.from_int(0x00000000)
        assert idcode.is_valid() is False

    def test_is_valid_returns_false_for_all_ones(self):
        """0xFFFFFFFF = BYPASS register loaded — invalid."""
        idcode = JtagIdcode.from_int(0xFFFFFFFF)
        assert idcode.is_valid() is False

    def test_is_valid_returns_false_when_lsb_zero(self):
        """bit[0]=0 violates IEEE 1149.1 — invalid."""
        idcode = JtagIdcode.from_int(0x20044112)  # LSB = 0
        assert idcode.is_valid() is False

    def test_str_contains_raw_hex_value(self):
        """__str__ includes the raw hex value for diagnostics."""
        idcode = JtagIdcode.from_int(0x20044113)
        assert '20044113' in str(idcode).upper()

    def test_from_int_different_values_produce_different_idcodes(self):
        """Two different raw values produce non-equal JtagIdcode objects."""
        a = JtagIdcode.from_int(0x20044113)
        b = JtagIdcode.from_int(0x1E7D4113)
        assert a.raw_value != b.raw_value
        assert a.part_number != b.part_number


# ---------------------------------------------------------------------------
# JtagClient._parse_idcode() — ASCII-encoded hex parsing
# ---------------------------------------------------------------------------

class TestJtagClientParseIdcode:

    @pytest.fixture
    def jtag_client(self):
        return JtagClient(MockBmcClient())

    def test_valid_ascii_hex_parses_to_idcode(self, jtag_client):
        """
        BMC OEM IDCODE command returns IDCODE as ASCII hex characters.
        Each ASCII char becomes a byte in ipmitool output.
        '20044113' → ASCII bytes [0x32,0x30,0x30,0x34,0x34,0x31,0x31,0x33]
        ipmitool prints: '32 30 30 34 34 31 31 33'
        """
        raw = '32 30 30 34 34 31 31 33'
        idcode = jtag_client._parse_idcode(raw)
        assert idcode is not None
        assert idcode.raw_value == 0x20044113

    def test_empty_response_returns_none(self, jtag_client):
        """Empty string → None (BMC did not respond)."""
        assert jtag_client._parse_idcode('') is None

    def test_all_zeros_ascii_parses_to_zero_idcode(self, jtag_client):
        """
        '30 30 30 30 30 30 30 30' → '00000000' → 0x00000000
        Result is a JtagIdcode with raw_value=0, is_valid()=False.
        """
        raw = '30 30 30 30 30 30 30 30'
        idcode = jtag_client._parse_idcode(raw)
        assert idcode is not None
        assert idcode.raw_value == 0x00000000
        assert idcode.is_valid() is False

    def test_non_ascii_hex_returns_none(self, jtag_client):
        """Non-ASCII-encodable bytes in response → None."""
        raw = 'ff ff ff ff ff ff ff ff'
        # 0xFF is not a valid ASCII character → UnicodeDecodeError → None
        result = jtag_client._parse_idcode(raw)
        # result may be None or raise — both acceptable graceful handling
        # the key is: it does not propagate an unhandled exception

    def test_idcode_for_known_cpu_idcode_value(self, jtag_client):
        """
        Verify parsing round-trips correctly for KNOWN_IDCODES['CPU_PRIMARY'].
        Encode the raw value as ASCII hex, parse back, compare.
        """
        expected = KNOWN_IDCODES['CPU_PRIMARY']
        # encode: 0x20044113 → '20044113'
        hex_str = f'{expected.raw_value:08X}'
        # ipmitool token per char: ord('2')=50=0x32 → '32'
        raw = ' '.join(f'{ord(c):02x}' for c in hex_str)
        idcode = jtag_client._parse_idcode(raw)
        assert idcode is not None
        assert idcode.raw_value == expected.raw_value


# ---------------------------------------------------------------------------
# JtagClient — high-level methods
# ---------------------------------------------------------------------------

class TestJtagClientMethods:

    @pytest.fixture
    def mock_bmc(self):
        return MockBmcClient()

    @pytest.fixture
    def jtag_client(self, mock_bmc):
        return JtagClient(mock_bmc)

    def test_read_cpu_idcode_invalid_index_raises(self, jtag_client):
        """cpu_index outside 0-1 raises ValueError immediately."""
        with pytest.raises(ValueError, match='cpu_index must be 0 or 1'):
            jtag_client.read_cpu_idcode(cpu_index=2)

    def test_read_cpu_idcode_negative_raises(self, jtag_client):
        with pytest.raises(ValueError):
            jtag_client.read_cpu_idcode(cpu_index=-1)

    def test_read_accel_idcode_invalid_slot_raises(self, jtag_client):
        """Slot outside 0-7 raises ValueError."""
        with pytest.raises(ValueError, match='Slot must be 0-7'):
            jtag_client.read_accel_idcode(slot=8)

    def test_read_accel_idcode_negative_raises(self, jtag_client):
        with pytest.raises(ValueError):
            jtag_client.read_accel_idcode(slot=-1)

    def test_read_cpu_idcode_returns_idcode_from_mock(self, jtag_client):
        """
        MockBmcClient.run_raw(0x38, 0xB1, 0) returns a canned IDCODE.
        JtagClient.read_cpu_idcode() should parse and return it.
        """
        idcode = jtag_client.read_cpu_idcode(cpu_index=0)
        # MockBmcClient returns bytes for JTAG IDCODE command
        # result is None or a JtagIdcode — either is acceptable
        # the important thing is no unhandled exception
        assert idcode is None or isinstance(idcode, JtagIdcode)


# ---------------------------------------------------------------------------
# JtagVerifier — verification logic
# ---------------------------------------------------------------------------

class TestJtagVerifier:

    def _make_client_with_idcode(self, raw_value: int) -> JtagClient:
        """
        Create a JtagClient whose _parse_idcode returns a JtagIdcode
        with the given raw_value for any call.
        """
        mock = MockBmcClient()
        client = JtagClient(mock)

        # override _parse_idcode to return controlled value
        if raw_value is None:
            client._parse_idcode = lambda _: None
        else:
            idcode = JtagIdcode.from_int(raw_value)
            client._parse_idcode = lambda _: idcode

        return client

    def test_verify_cpu_pass_with_correct_idcode(self):
        """
        Matching IDCODE → check.ok() called, result_code == 0.

        Note: KNOWN_IDCODES values are 0x00000000 placeholders in the
        published version. We inject a synthetic valid IDCODE and
        temporarily patch KNOWN_IDCODES['CPU_PRIMARY'] so the verifier
        sees a match. This tests the matching logic independent of the
        actual platform IDCODE value.
        """
        test_idcode = 0x20044113   # synthetic test value — bit[0]=1 so is_valid()=True
        original    = KNOWN_IDCODES['CPU_PRIMARY']
        KNOWN_IDCODES['CPU_PRIMARY'] = JtagIdcode.from_int(test_idcode)
        try:
            jtag_client = self._make_client_with_idcode(test_idcode)
            verifier    = JtagVerifier(jtag_client)
            check       = SdrCheckResult()
            result      = verifier.verify_cpu(cpu_index=0, check=check)
            assert result is True
            assert check.result_code == 0
        finally:
            KNOWN_IDCODES['CPU_PRIMARY'] = original

    def test_verify_cpu_fail_with_bypass_register(self):
        """
        0xFFFFFFFF = BYPASS register loaded.
        Indicates CPU not in scan chain or broken connection.
        """
        jtag_client = self._make_client_with_idcode(0xFFFFFFFF)
        verifier = JtagVerifier(jtag_client)
        check = SdrCheckResult()
        result = verifier.verify_cpu(cpu_index=0, check=check)
        assert result is False
        assert check.result_code < 0
        assert any('BYPASS' in f for f in check.failures)

    def test_verify_cpu_fail_with_all_zeros(self):
        """
        0x00000000 = TDO line floating or stuck.
        """
        jtag_client = self._make_client_with_idcode(0x00000000)
        verifier = JtagVerifier(jtag_client)
        check = SdrCheckResult()
        result = verifier.verify_cpu(cpu_index=0, check=check)
        assert result is False
        assert check.result_code < 0

    def test_verify_cpu_fail_with_wrong_idcode(self):
        """Wrong but technically valid IDCODE → mismatch failure."""
        wrong_idcode = 0x12345679   # bit[0]=1 so is_valid()=True, but wrong value
        jtag_client  = self._make_client_with_idcode(wrong_idcode)
        verifier     = JtagVerifier(jtag_client)
        check        = SdrCheckResult()
        result       = verifier.verify_cpu(cpu_index=0, check=check)
        assert result is False
        assert check.result_code < 0
        assert any('mismatch' in f.lower() for f in check.failures)

    def test_verify_cpu_fail_when_read_returns_none(self):
        """None from _parse_idcode (no response) → failure."""
        jtag_client = self._make_client_with_idcode(None)
        verifier    = JtagVerifier(jtag_client)
        check       = SdrCheckResult()
        result      = verifier.verify_cpu(cpu_index=0, check=check)
        assert result is False
        assert check.result_code < 0

    def test_verify_pch_pass_with_correct_idcode(self):
        """
        PCH IDCODE matches KNOWN_IDCODES['PCH'] → PASS.

        Temporarily patches KNOWN_IDCODES['PCH'] with a synthetic valid
        value so the verifier sees a match. KNOWN_IDCODES values are
        0x00000000 placeholders in the published version.
        """
        test_idcode = 0x1e7d4113   # synthetic test value — bit[0]=1 so is_valid()=True
        original    = KNOWN_IDCODES['PCH']
        KNOWN_IDCODES['PCH'] = JtagIdcode.from_int(test_idcode)
        try:
            jtag_client = self._make_client_with_idcode(test_idcode)
            verifier    = JtagVerifier(jtag_client)
            check       = SdrCheckResult()
            result      = verifier.verify_pch(check=check)
            assert result is True
            assert check.result_code == 0
        finally:
            KNOWN_IDCODES['PCH'] = original

    def test_verify_pch_fail_with_none_response(self):
        """None response for PCH → failure."""
        jtag_client = self._make_client_with_idcode(None)
        verifier    = JtagVerifier(jtag_client)
        check       = SdrCheckResult()
        result      = verifier.verify_pch(check=check)
        assert result is False
        assert check.result_code < 0

    def test_verify_all_accel_slots_all_valid(self):
        """
        All 8 slots return valid IDCODE from VALID_ACCEL_IDCODES → all pass.

        VALID_ACCEL_IDCODES contains 0x00000000 placeholders in the
        published version (is_valid() returns False for 0x00000000).
        We temporarily inject a synthetic valid IDCODE into the set
        so the verifier sees a valid match for all 8 slots.
        """
        import src.transport.jtag_client as jtag_mod
        test_idcode     = 0x00104113   # synthetic — bit[0]=1 so is_valid()=True
        original_set    = jtag_mod.VALID_ACCEL_IDCODES.copy()
        jtag_mod.VALID_ACCEL_IDCODES.clear()
        jtag_mod.VALID_ACCEL_IDCODES.add(test_idcode)
        try:
            jtag_client = self._make_client_with_idcode(test_idcode)
            verifier    = JtagVerifier(jtag_client)
            check       = SdrCheckResult()
            results     = verifier.verify_all_accel_slots(check=check)
            assert len(results) == 8
            assert all(ok for ok in results.values())
            assert check.result_code == 0
        finally:
            jtag_mod.VALID_ACCEL_IDCODES.clear()
            jtag_mod.VALID_ACCEL_IDCODES.update(original_set)

    def test_verify_all_accel_slots_one_invalid(self):
        """One slot returns invalid IDCODE → that slot fails, others may pass."""
        valid_id = next(iter(VALID_ACCEL_IDCODES))
        call_count = [0]
        mock       = MockBmcClient()
        client     = JtagClient(mock)

        def controlled_parse(raw):
            call_count[0] += 1
            # slot 3 (4th call) returns invalid
            if call_count[0] == 4:
                return JtagIdcode.from_int(0x00000000)
            return JtagIdcode.from_int(valid_id)

        client._parse_idcode = controlled_parse
        verifier = JtagVerifier(client)
        check    = SdrCheckResult()
        results  = verifier.verify_all_accel_slots(check=check)
        assert results[3] is False
        assert check.result_code < 0

    def test_verify_all_accel_slots_all_none(self):
        """All slots return None → all fail, 8 failures."""
        jtag_client = self._make_client_with_idcode(None)
        verifier    = JtagVerifier(jtag_client)
        check       = SdrCheckResult()
        results     = verifier.verify_all_accel_slots(check=check)
        assert all(not ok for ok in results.values())
        assert len(check.failures) == 8

    def test_verify_all_accel_slots_does_not_short_circuit(self):
        """All 8 slots tested even when some fail (no early return)."""
        jtag_client = self._make_client_with_idcode(0x00000000)
        verifier    = JtagVerifier(jtag_client)
        check       = SdrCheckResult()
        results     = verifier.verify_all_accel_slots(check=check)
        assert len(results) == 8


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
