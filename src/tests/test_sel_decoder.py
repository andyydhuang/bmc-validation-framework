"""
src/tests/test_sel_decoder.py

Unit tests for src/protocol/sel_decoder.py

Tests verify:
    1. parse_raw_response() — hex string to bytes conversion
    2. _decode_standard()   — Standard System Event Record (type 0x02)
    3. _decode_oem_pcie()   — OEM Timestamped PCIe AER record (type 0xC0)
    4. decode_entry()       — full 18-byte response parsing
    5. PCIe AER error map   — all error ID lookups
    6. format_record()      — human-readable output formatting

All tests run without hardware. Raw bytes are constructed manually
to match the IPMI 2.0 SEL wire format documented in Phase 3.

Run with:
    python -m pytest src/tests/test_sel_decoder.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

import struct
import pytest
from datetime import datetime, timezone

from src.protocol.sel_decoder import (
    SelDecoder,
    SelStandardRecord,
    SelOemPcieRecord,
    SelOemNonTimestampedRecord,
    PCIE_AER_ERRORS,
    SENSOR_TYPES,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_standard_record(record_id:   int   = 0x0001,
                          timestamp:   int   = 1645406930,
                          sensor_type: int   = 0x07,
                          sensor_num:  int   = 0x00,
                          ev1:         int   = 0x00,
                          ev2:         int   = 0x00,
                          ev3:         int   = 0x00) -> bytes:
    """
    Build a valid 18-byte Standard System Event Record response.

    Format: [next_id: 2 bytes LE] + [record: 16 bytes]
    Record struct: '<HBI BBBBB bBBB'
    """
    next_record_id = 0x0002

    record_16 = struct.pack(
        '<HBI BBBBB bBBB',
        record_id,          # bytes 0-1: record ID
        0x02,               # byte  2:  record type (Standard)
        timestamp,          # bytes 3-6: Unix timestamp LE
        0x20,               # byte  7:  gen_id_low
        0x00,               # byte  8:  gen_id_high
        0x04,               # byte  9:  ev_msg_rev
        sensor_type,        # byte  10: sensor type
        sensor_num,         # byte  11: sensor number
        0x6F,               # byte  12: evt_dir_type
        ev1,                # byte  13
        ev2,                # byte  14
        ev3,                # byte  15
    )

    nav = struct.pack('<H', next_record_id)
    return nav + record_16


def make_oem_pcie_record(record_id:       int = 0x010F,
                          timestamp:       int = 1645406930,
                          manufacturer_id: int = 0x001234,  # generic placeholder — replace with actual
                          vendor_id:       int = 0x8086,
                          device_id:       int = 0x352A,
                          slot_number:     int = 0xFF,
                          pcie_error_id:   int = 0x50) -> bytes:
    """
    Build a valid 18-byte OEM Timestamped PCIe AER record response.

    Byte layout after common header:
        [7:9]   Manufacturer ID (24-bit LE)
        [10:11] Vendor ID (16-bit LE)
        [12:13] Device ID (16-bit LE)
        [14]    Slot Number
        [15]    PCIe Error ID
    """
    next_record_id = 0x0110

    # construct 16-byte record manually
    record_16 = bytearray(16)

    # bytes 0-1: record ID (LE)
    struct.pack_into('<H', record_16, 0, record_id)

    # byte 2: record type
    record_16[2] = 0xC0

    # bytes 3-6: timestamp (LE uint32)
    struct.pack_into('<I', record_16, 3, timestamp)

    # bytes 7-9: manufacturer ID (24-bit LE)
    record_16[7] = manufacturer_id & 0xFF
    record_16[8] = (manufacturer_id >> 8)  & 0xFF
    record_16[9] = (manufacturer_id >> 16) & 0xFF

    # bytes 10-11: vendor ID (LE uint16)
    struct.pack_into('<H', record_16, 10, vendor_id)

    # bytes 12-13: device ID (LE uint16)
    struct.pack_into('<H', record_16, 12, device_id)

    # byte 14: slot number
    record_16[14] = slot_number

    # byte 15: PCIe error ID
    record_16[15] = pcie_error_id

    nav = struct.pack('<H', next_record_id)
    return nav + bytes(record_16)


# ---------------------------------------------------------------------------
# parse_raw_response() tests
# ---------------------------------------------------------------------------

class TestParseRawResponse:
    """Tests for hex string → bytes conversion."""

    def setup_method(self):
        self.decoder = SelDecoder()

    def test_space_separated_hex_converts_correctly(self):
        raw    = '00 01 c0 d2 4e'
        result = self.decoder.parse_raw_response(raw)
        assert result == bytes([0x00, 0x01, 0xC0, 0xD2, 0x4E])

    def test_multiline_output_flattened(self):
        """ipmitool sometimes wraps long responses across lines."""
        raw    = '00 01 c0\nd2 4e 13'
        result = self.decoder.parse_raw_response(raw)
        assert result == bytes([0x00, 0x01, 0xC0, 0xD2, 0x4E, 0x13])

    def test_empty_string_returns_empty_bytes(self):
        assert self.decoder.parse_raw_response('') == bytes()

    def test_non_hex_token_raises_value_error(self):
        with pytest.raises(ValueError, match='Non-hex token'):
            self.decoder.parse_raw_response('00 01 ZZ 03')

    def test_uppercase_hex_accepted(self):
        raw    = 'FF C0 3A'
        result = self.decoder.parse_raw_response(raw)
        assert result == bytes([0xFF, 0xC0, 0x3A])

    def test_lowercase_hex_accepted(self):
        raw    = 'ff c0 3a'
        result = self.decoder.parse_raw_response(raw)
        assert result == bytes([0xFF, 0xC0, 0x3A])


# ---------------------------------------------------------------------------
# Standard System Event Record tests
# ---------------------------------------------------------------------------

class TestDecodeStandardRecord:
    """
    Tests for Standard System Event Record decoding (type 0x02).
    IPMI 2.0 Table 32-1.
    """

    def setup_method(self):
        self.decoder = SelDecoder()

    def test_next_record_id_extracted_correctly(self):
        """
        Navigation pointer (bytes 0-1) correctly parsed as LE uint16.
        next_id=0x0002 stored as [0x02, 0x00] in little-endian.
        """
        raw_bytes = make_standard_record(record_id=0x0001)
        next_id, record = self.decoder.decode_entry(raw_bytes)
        assert next_id == 0x0002

    def test_record_type_is_standard(self):
        raw_bytes   = make_standard_record()
        _, record   = self.decoder.decode_entry(raw_bytes)
        assert isinstance(record, SelStandardRecord)
        assert record.record_type == 0x02

    def test_record_id_parsed_as_le_uint16(self):
        """
        Record ID is little-endian. ID=0x010F stored as [0x0F, 0x01].
        Verifies correct byte ordering in reconstruction.
        """
        raw_bytes = make_standard_record(record_id=0x010F)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.record_id == 0x010F

    def test_timestamp_reconstructed_correctly(self):
        """
        32-bit Unix timestamp stored little-endian.
        0x62134ED2 = 1645406930 = 2022-02-21 02:28:50 UTC
        Verifies struct.unpack('<I') is used correctly.
        """
        unix_ts   = 1645406930
        raw_bytes = make_standard_record(timestamp=unix_ts)
        _, record = self.decoder.decode_entry(raw_bytes)

        assert record.timestamp == datetime.fromtimestamp(
            unix_ts, tz=timezone.utc
        )
        assert record.timestamp.year  == 2022
        assert record.timestamp.month == 2
        assert record.timestamp.day   == 21

    def test_sensor_type_extracted(self):
        raw_bytes = make_standard_record(sensor_type=0x07)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.sensor_type == 0x07  # Processor

    def test_event_data_tuple_correct(self):
        raw_bytes = make_standard_record(ev1=0x01, ev2=0x02, ev3=0x03)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.event_data == (0x01, 0x02, 0x03)

    def test_too_short_raises_value_error(self):
        with pytest.raises(ValueError, match='too short'):
            self.decoder.decode_entry(bytes(10))

    def test_end_of_sel_sentinel_detected(self):
        """
        next_record_id == 0xFFFF signals end of SEL per IPMI 2.0 spec.
        The traversal loop should stop when this is returned.
        """
        # build response with next_id = 0xFFFF
        nav       = struct.pack('<H', 0xFFFF)
        record_16 = bytes(make_standard_record()[2:])  # strip normal nav
        raw_bytes = nav + record_16

        next_id, _ = self.decoder.decode_entry(raw_bytes)
        assert next_id == 0xFFFF


# ---------------------------------------------------------------------------
# OEM Timestamped PCIe AER record tests
# ---------------------------------------------------------------------------

class TestDecodeOemPcieRecord:
    """
    Tests for OEM Timestamped Record decoding (type 0xC0).
    This is the most protocol-dense record type in the framework.
    """

    def setup_method(self):
        self.decoder = SelDecoder()

    def test_record_type_is_oem_timestamped(self):
        raw_bytes = make_oem_pcie_record()
        _, record = self.decoder.decode_entry(raw_bytes)
        assert isinstance(record, SelOemPcieRecord)
        assert record.record_type == 0xC0

    def test_manufacturer_id_24bit_le_reconstruction(self):
        """
        Manufacturer ID is 24-bit little-endian at bytes 7-9.
        Example: 0x001234 stored as [0x34, 0x12, 0x00].

        Manufacturer ID is 24-bit little-endian — manual reconstruction
        must correctly separate it from the vendor ID bytes at bytes 10-11.

        struct.unpack_from('<H') at offset 10 gives the correct
        vendor ID without this confusion.
        """
        raw_bytes = make_oem_pcie_record(manufacturer_id=0x001234)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.manufacturer_id == 0x001234

    def test_vendor_id_16bit_le_reconstruction(self):
        """
        Vendor ID at bytes 10-11, little-endian.
        Intel = 0x8086, stored as [0x86, 0x80].

        A manual shift approach:
            vendor_id = (mysel[11] << 8) | mysel[10]
        gives (0x80 << 8) | 0x86 = 0x8086 — coincidentally correct
        for this specific case, but wrong byte index semantics.
        struct.unpack_from('<H', record, 10) is definitively correct.
        """
        raw_bytes = make_oem_pcie_record(vendor_id=0x8086)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.vendor_id == 0x8086

    def test_device_id_16bit_le_reconstruction(self):
        """Device ID at bytes 12-13, little-endian."""
        raw_bytes = make_oem_pcie_record(device_id=0x352A)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.device_id == 0x352A

    def test_slot_number_bcd_decoded(self):
        """
        Slot number byte decoded into riser_slot and mb_slot fields.
        bits [7:5] = riser slot (0-7)
        bits [4:0] = MB slot    (0-31)

        slot_byte=0xFF = 0b11111111
        riser = (0xFF >> 5) & 0x07 = 7
        mb    = 0xFF & 0x1F         = 31
        """
        raw_bytes = make_oem_pcie_record(slot_number=0xFF)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.slot_number  == 0xFF
        assert record.riser_slot   == 7
        assert record.mb_slot      == 31

    def test_slot_number_specific_value(self):
        """
        Verify slot decoding with a non-trivial slot value.
        slot_byte = 0x41 = 0b01000001
        riser = (0x41 >> 5) & 0x07 = 0b010 = 2
        mb    = 0x41 & 0x1F         = 0b00001 = 1
        """
        raw_bytes = make_oem_pcie_record(slot_number=0x41)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.riser_slot == 2
        assert record.mb_slot    == 1

    def test_pcie_error_id_extracted(self):
        raw_bytes = make_oem_pcie_record(pcie_error_id=0x50)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.pcie_error_id == 0x50

    def test_pcie_error_severity_decoded(self):
        """Error ID 0x50 maps to CORRECTABLE severity."""
        raw_bytes = make_oem_pcie_record(pcie_error_id=0x50)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.severity    == 'CORRECTABLE'
        assert 'Correctable Error' in record.description

    def test_fatal_error_severity(self):
        """Error ID 0x52 maps to FATAL severity."""
        raw_bytes = make_oem_pcie_record(pcie_error_id=0x52)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.severity == 'FATAL'

    def test_nonfatal_error_severity(self):
        """Error ID 0x24 (Completion Timeout) maps to NON-FATAL."""
        raw_bytes = make_oem_pcie_record(pcie_error_id=0x24)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.severity    == 'NON-FATAL'
        assert 'Completion Timeout' in record.description

    def test_unknown_error_id_returns_unknown_severity(self):
        """
        Error IDs not in the lookup table return 'UNKNOWN' severity
        rather than raising KeyError. This ensures the decoder handles
        future BMC firmware additions gracefully.
        """
        raw_bytes = make_oem_pcie_record(pcie_error_id=0xFE)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.severity == 'UNKNOWN'
        assert '0xFE' in record.description or 'FE' in record.description

    def test_timestamp_timezone_aware(self):
        """Decoded timestamp must be timezone-aware (UTC)."""
        raw_bytes = make_oem_pcie_record(timestamp=1645406930)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.timestamp.tzinfo is not None
        assert record.timestamp.tzinfo == timezone.utc

    def test_vendor_id_zero_decodes_without_error(self):
        """Edge case: vendor ID of 0x0000 should not crash the decoder."""
        raw_bytes = make_oem_pcie_record(vendor_id=0x0000)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert record.vendor_id == 0x0000


# ---------------------------------------------------------------------------
# OEM Non-Timestamped record tests
# ---------------------------------------------------------------------------

class TestDecodeOemNonTimestampedRecord:
    """Tests for OEM Non-Timestamped records (types 0xC1-0xFF)."""

    def setup_method(self):
        self.decoder = SelDecoder()

    def _make_nontimestamped(self, record_type: int = 0xC1) -> bytes:
        """Build minimal 18-byte OEM non-timestamped response."""
        nav       = struct.pack('<H', 0x0002)
        record_16 = bytearray(16)
        struct.pack_into('<H', record_16, 0, 0x0001)  # record_id
        record_16[2] = record_type
        # bytes 3-15 are OEM-defined — fill with test pattern
        for i in range(3, 16):
            record_16[i] = i
        return nav + bytes(record_16)

    def test_type_c1_decoded_as_nontimestamped(self):
        raw_bytes = self._make_nontimestamped(0xC1)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert isinstance(record, SelOemNonTimestampedRecord)
        assert record.record_type == 0xC1

    def test_type_ff_decoded_as_nontimestamped(self):
        raw_bytes = self._make_nontimestamped(0xFF)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert isinstance(record, SelOemNonTimestampedRecord)

    def test_raw_data_preserved(self):
        """Raw bytes 3-15 preserved for platform-specific interpretation."""
        raw_bytes = self._make_nontimestamped(0xC1)
        _, record = self.decoder.decode_entry(raw_bytes)
        assert isinstance(record.raw_data, bytes)
        assert len(record.raw_data) >= 13  # bytes 3-15


# ---------------------------------------------------------------------------
# PCIe AER error map completeness tests
# ---------------------------------------------------------------------------

class TestPcieAerErrorMap:
    """
    Tests that the AER error map is complete and correctly structured.
    Uses a dict lookup instead of a long elif chain.
    """

    def test_all_correctable_errors_present(self):
        """All 8 PCIe correctable error codes from spec should be mapped."""
        correctable_ids = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07]
        for eid in correctable_ids:
            assert eid in PCIE_AER_ERRORS, \
                f'Correctable error ID 0x{eid:02X} missing from map'
            severity, _ = PCIE_AER_ERRORS[eid]
            assert severity == 'CORRECTABLE', \
                f'Error ID 0x{eid:02X} should be CORRECTABLE, got {severity}'

    def test_fatal_errors_mapped_correctly(self):
        """Known fatal error IDs should have FATAL severity."""
        fatal_ids = [0x20, 0x22, 0x23, 0x27, 0x28]
        for eid in fatal_ids:
            assert eid in PCIE_AER_ERRORS
            severity, _ = PCIE_AER_ERRORS[eid]
            assert severity == 'FATAL', \
                f'0x{eid:02X} should be FATAL, got {severity}'

    def test_nonfatal_errors_mapped_correctly(self):
        """Known non-fatal error IDs should have NON-FATAL severity."""
        nonfatal_ids = [0x21, 0x24, 0x25, 0x26, 0x29]
        for eid in nonfatal_ids:
            assert eid in PCIE_AER_ERRORS
            severity, _ = PCIE_AER_ERRORS[eid]
            assert severity == 'NON-FATAL', \
                f'0x{eid:02X} should be NON-FATAL, got {severity}'

    def test_oem_extension_errors_present(self):
        """Platform OEM extension error IDs (0x50+) should be mapped."""
        oem_ids = [0x50, 0x51, 0x52, 0x60, 0x80, 0x85, 0x86]
        for eid in oem_ids:
            assert eid in PCIE_AER_ERRORS, \
                f'OEM error ID 0x{eid:02X} missing from map'

    def test_all_entries_have_severity_and_description(self):
        """Every entry must be a (severity, description) tuple."""
        valid_severities = {'CORRECTABLE', 'NON-FATAL', 'FATAL'}
        for eid, entry in PCIE_AER_ERRORS.items():
            assert isinstance(entry, tuple) and len(entry) == 2, \
                f'Entry for 0x{eid:02X} is not a 2-tuple'
            severity, description = entry
            assert severity in valid_severities, \
                f'0x{eid:02X} has invalid severity: {severity}'
            assert description, \
                f'0x{eid:02X} has empty description'

    def test_error_id_lookup_is_o1(self):
        """
        Dict lookup must succeed for known IDs.
        Verifies we replaced the elif chain with a proper dict.
        """
        assert PCIE_AER_ERRORS.get(0x00) is not None
        assert PCIE_AER_ERRORS.get(0x50) is not None
        # unknown ID returns None from .get(), not KeyError
        assert PCIE_AER_ERRORS.get(0xFF) is None


# ---------------------------------------------------------------------------
# format_record() tests
# ---------------------------------------------------------------------------

class TestFormatRecord:
    """Tests for human-readable record formatting."""

    def setup_method(self):
        self.decoder = SelDecoder()

    def test_standard_record_format_contains_sensor_type_name(self):
        """Standard record format includes human-readable sensor type."""
        raw_bytes  = make_standard_record(sensor_type=0x07)
        _, record  = self.decoder.decode_entry(raw_bytes)
        formatted  = self.decoder.format_record(record)
        assert 'Processor' in formatted

    def test_oem_pcie_record_format_contains_vendor_id(self):
        """OEM PCIe record format includes vendor ID in hex."""
        raw_bytes = make_oem_pcie_record(vendor_id=0x8086)
        _, record = self.decoder.decode_entry(raw_bytes)
        formatted = self.decoder.format_record(record)
        assert '8086' in formatted

    def test_oem_pcie_record_format_contains_severity(self):
        """OEM PCIe record format includes severity string."""
        raw_bytes = make_oem_pcie_record(pcie_error_id=0x00)
        _, record = self.decoder.decode_entry(raw_bytes)
        formatted = self.decoder.format_record(record)
        assert 'CORRECTABLE' in formatted

    def test_oem_pcie_record_format_contains_timestamp(self):
        """OEM PCIe record format includes human-readable timestamp."""
        raw_bytes = make_oem_pcie_record(timestamp=1645406930)
        _, record = self.decoder.decode_entry(raw_bytes)
        formatted = self.decoder.format_record(record)
        assert '2022' in formatted  # year extracted from Unix timestamp

    def test_nontimestamped_format_contains_type(self):
        """Non-timestamped record format includes record type byte."""
        nav       = struct.pack('<H', 0x0002)
        record_16 = bytearray(16)
        struct.pack_into('<H', record_16, 0, 0x0001)
        record_16[2] = 0xC3
        raw_bytes = nav + bytes(record_16)
        _, record = self.decoder.decode_entry(raw_bytes)
        formatted = self.decoder.format_record(record)
        assert 'C3' in formatted or 'c3' in formatted


# ---------------------------------------------------------------------------
# Sensor type table tests
# ---------------------------------------------------------------------------

class TestSensorTypeTable:
    """Tests for IPMI sensor type lookup table completeness."""

    def test_common_sensor_types_present(self):
        """Verify commonly used sensor types are in the lookup table."""
        assert 0x01 in SENSOR_TYPES  # Temperature
        assert 0x02 in SENSOR_TYPES  # Voltage
        assert 0x04 in SENSOR_TYPES  # Fan
        assert 0x07 in SENSOR_TYPES  # Processor
        assert 0x08 in SENSOR_TYPES  # Power Supply
        assert 0x0C in SENSOR_TYPES  # Memory

    def test_sensor_type_names_are_strings(self):
        """All sensor type names must be non-empty strings."""
        for code, name in SENSOR_TYPES.items():
            assert isinstance(name, str), \
                f'Sensor type 0x{code:02X} name is not a string'
            assert name, \
                f'Sensor type 0x{code:02X} has empty name'

    def test_unknown_sensor_type_handled_gracefully(self):
        """
        Sensor types above 0x2C are OEM-specific and not in the table.
        Callers should use .get() with a fallback, not direct indexing.
        """
        assert SENSOR_TYPES.get(0xFF) is None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
