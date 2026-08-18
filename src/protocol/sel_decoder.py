"""
SEL binary record decoder.

SEL records are 16-byte binary structs. Three types:
    0x02 — standard system event (sensors, power, CPU errors)
    0xC0 — OEM timestamped (PCIe AER errors on this platform)
    0xC1+ — OEM non-timestamped (stored as raw bytes)

struct.unpack handles byte order correctly for all multi-byte fields.
The 24-bit manufacturer ID has no struct format code so it is reconstructed
manually from three bytes.

PCIe AER error lookup is a dict rather than a long elif chain.
CORRECTABLE / NON-FATAL / FATAL severity comes from the same dict entry.
"""

import struct
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from typing import Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Record type constants (IPMI 2.0 Section 32.1)
# ---------------------------------------------------------------------------

class SelRecordType(IntEnum):
    STANDARD_SYSTEM_EVENT = 0x02
    OEM_TIMESTAMPED       = 0xC0
    # 0xC1-0xFF = OEM non-timestamped (handled as a range check)


# ---------------------------------------------------------------------------
# Decoded record dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SelStandardRecord:
    """
    Standard System Event Record (type 0x02).
    IPMI 2.0 Table 32-1.
    16 bytes total after stripping the 2-byte navigation pointer.
    """
    record_id:    int       # uint16 LE — unique SEL identifier
    record_type:  int       # 0x02
    timestamp:    datetime  # uint32 LE Unix seconds since 1970, UTC
    generator_id: int       # uint16 LE — identifies event source
    ev_msg_rev:   int       # 0x04 for IPMI 2.0
    sensor_type:  int       # IPMI Table 42-3 sensor type code
    sensor_num:   int       # sensor number within SDR
    evt_dir_type: int       # bit7=direction, bits6:0=event type
    event_data:   Tuple[int, int, int]  # three event-specific bytes


@dataclass(frozen=True)
class SelOemPcieRecord:
    """
    OEM Timestamped Record (type 0xC0) — PCIe AER event format.

    Used by BIOS to log PCIe Advanced Error Reporting events.
    Byte layout after the common header:
        [7:9]   Manufacturer ID  24-bit LE
        [10:11] Vendor ID        16-bit LE (e.g. 0x8086 = Intel)
        [12:13] Device ID        16-bit LE
        [14]    Slot Number      BCD: bits[7:5]=riser, bits[4:0]=MB slot
        [15]    PCIe Error ID    error category byte
    """
    record_id:       int
    record_type:     int       # 0xC0
    timestamp:       datetime
    manufacturer_id: int       # 24-bit LE — platform-specific manufacturer ID
    vendor_id:       int       # PCIe Vendor ID (e.g. 0x8086 = Intel)
    device_id:       int       # PCIe Device ID
    slot_number:     int       # raw BCD byte
    riser_slot:      int       # decoded: bits[7:5] of slot_number
    mb_slot:         int       # decoded: bits[4:0] of slot_number
    pcie_error_id:   int       # AER error category byte
    severity:        str       # 'CORRECTABLE', 'NON-FATAL', or 'FATAL'
    description:     str       # human-readable error description


@dataclass(frozen=True)
class SelOemNonTimestampedRecord:
    """
    OEM Non-Timestamped Record (types 0xC1-0xFF).
    IPMI 2.0 Section 32.3.
    Bytes 3-15 are entirely OEM-defined — only type is reliable.
    """
    record_id:  int
    record_type: int
    raw_data:   bytes   # bytes 3-15 unparsed


# Union type for all possible decoded record types
SelRecord = Union[
    SelStandardRecord,
    SelOemPcieRecord,
    SelOemNonTimestampedRecord,
]


# ---------------------------------------------------------------------------
# IPMI sensor type table (Table 42-3, subset)
# ---------------------------------------------------------------------------

SENSOR_TYPES: dict = {
    0x00: 'Reserved',
    0x01: 'Temperature',
    0x02: 'Voltage',
    0x03: 'Current',
    0x04: 'Fan',
    0x05: 'Physical Security',
    0x06: 'Platform Security Violation',
    0x07: 'Processor',
    0x08: 'Power Supply',
    0x09: 'Power Unit',
    0x0A: 'Cooling Device',
    0x0B: 'Other',
    0x0C: 'Memory',
    0x0D: 'Drive Slot',
    0x0E: 'POST Memory Resize',
    0x0F: 'System Firmware Progress',
    0x10: 'Event Logging Disabled',
    0x11: 'Watchdog 1',
    0x12: 'System Event',
    0x13: 'Critical Interrupt',
    0x14: 'Button/Switch',
    0x19: 'Chip Set',
    0x1B: 'Cable/Interconnect',
    0x1D: 'System Boot/Restart',
    0x1E: 'Boot Error',
    0x1F: 'Base OS Boot/Installation Status',
    0x20: 'OS Stop/Shutdown',
    0x21: 'Slot/Connector',
    0x23: 'Watchdog 2',
    0x24: 'Entity Presence',
    0x25: 'Monitor ASIC/IC',
    0x26: 'LAN',
    0x27: 'Management Subsystem Health',
    0x28: 'Battery',
    0x29: 'Session Audit',
    0x2A: 'Version Change',
    0x2B: 'FRU State',
    0x2C: 'FRU State',
}


# ---------------------------------------------------------------------------
# PCIe AER error taxonomy
# (PCIe Base Spec 4.0 Section 6.2 + platform OEM extensions)
# ---------------------------------------------------------------------------

# Maps PCIe Error ID byte → (severity, description)
PCIE_AER_ERRORS: dict = {
    # ── Correctable errors ──
    # PCIe AER Correctable Error Status Register bits
    0x00: ('CORRECTABLE', 'Receiver Error Status'),
    0x01: ('CORRECTABLE', 'Bad TLP Status'),
    0x02: ('CORRECTABLE', 'Bad DLLP Status'),
    0x03: ('CORRECTABLE', 'Replay Number Rollover Status'),
    0x04: ('CORRECTABLE', 'Replay Timer Timeout Status'),
    0x05: ('CORRECTABLE', 'Advisory Non-Fatal Error Status'),
    0x06: ('CORRECTABLE', 'Corrected Internal Error Status'),
    0x07: ('CORRECTABLE', 'Header Log Overflow Status'),

    # ── Uncorrectable fatal errors ──
    # PCIe AER Uncorrectable Error Status Register — fatal severity
    0x20: ('FATAL', 'Data Link Protocol Error Status'),
    0x22: ('FATAL', 'Poisoned TLP Received'),
    0x23: ('FATAL', 'Flow Control Protocol Error Status'),
    0x27: ('FATAL', 'Receiver Overflow Status'),
    0x28: ('FATAL', 'Malformed TLP Status'),
    0x3C: ('FATAL', 'Uncorrectable Internal Error Status'),

    # ── Uncorrectable non-fatal errors ──
    0x21: ('NON-FATAL', 'Surprise Down Error Status'),
    0x24: ('NON-FATAL', 'Completion Timeout Status'),
    0x25: ('NON-FATAL', 'Completer Abort Status'),
    0x26: ('NON-FATAL', 'Unexpected Completion Status'),
    0x29: ('NON-FATAL', 'ECRC Error Status'),
    0x2A: ('NON-FATAL', 'Unsupported Request Error Status'),
    0x2B: ('NON-FATAL', 'ACS Violation Status'),
    0x2D: ('NON-FATAL', 'MC Blocked TLP Status'),
    0x2E: ('NON-FATAL', 'AtomicOp Egress Blocked Status'),
    0x2F: ('NON-FATAL', 'TLP Prefix Blocked Error Status'),
    0x30: ('NON-FATAL', 'Poisoned TLP Egress Blocked Status'),

    # ── Platform OEM extensions (0x50+) ──
    # These IDs are vendor-specific and defined in platform firmware
    0x50: ('CORRECTABLE', 'Correctable Error Received at Switch'),
    0x51: ('NON-FATAL',   'Non-Fatal Error Messages Received'),
    0x52: ('FATAL',       'Fatal Error Messages Received'),
    0x60: ('CORRECTABLE', 'PCI Link Bandwidth Changed'),
    0x80: ('FATAL',       'Outbound Switch FIFO Data Parity Error'),
    0x81: ('NON-FATAL',   'Sent Completion with Completer Abort'),
    0x82: ('NON-FATAL',   'Sent Completion with Unsupported Request'),
    0x83: ('NON-FATAL',   'Received PCIe Completion with CA Status'),
    0x84: ('NON-FATAL',   'Received PCIe Completion with UR Status'),
    0x85: ('NON-FATAL',   'Received MSI Write Larger than DWORD'),
    0x86: ('NON-FATAL',   'Outbound Poisoned Data'),
}


# ---------------------------------------------------------------------------
# SEL decoder
# ---------------------------------------------------------------------------

class SelDecoder:
    """
    Decodes IPMI SEL Get Entry responses into typed Python dataclasses.

    Usage:
        decoder = SelDecoder()

        # parse raw 18-byte response from Get SEL Entry command
        next_id, record = decoder.decode_entry(raw_bytes)

        if isinstance(record, SelOemPcieRecord):
            print(f"PCIe {record.severity}: {record.description}")
            print(f"Vendor 0x{record.vendor_id:04X} "
                  f"Device 0x{record.device_id:04X}")
    """

    def decode_entry(self,
                     raw_bytes: bytes
                     ) -> Tuple[int, Optional[SelRecord]]:
        """
        Decode one 18-byte Get SEL Entry response.

        Wire format (IPMI 2.0 Section 31.4.2):
            bytes [0:2]  = Next Record ID (LE uint16) — navigation pointer
            bytes [2:18] = 16-byte SEL record

        Args:
            raw_bytes: exactly 18 bytes from Get SEL Entry response

        Returns:
            Tuple of (next_record_id, decoded_record)
            next_record_id == 0xFFFF signals end of SEL

        Raises:
            ValueError: response too short to parse
        """
        if len(raw_bytes) < 18:
            raise ValueError(
                f'SEL response too short: {len(raw_bytes)} bytes, '
                f'need 18. Possible BMC communication error.'
            )

        # extract navigation pointer — tells us which record to fetch next
        next_record_id, = struct.unpack_from('<H', raw_bytes, offset=0)
        # '<H' = little-endian unsigned short (2 bytes) at offset 0

        record_bytes = raw_bytes[2:18]  # strip navigation bytes
        record_type  = record_bytes[2]  # byte 2 of record = type field

        if record_type == SelRecordType.STANDARD_SYSTEM_EVENT:
            return next_record_id, self._decode_standard(record_bytes)

        elif record_type == SelRecordType.OEM_TIMESTAMPED:
            return next_record_id, self._decode_oem_pcie(record_bytes)

        elif record_type > SelRecordType.OEM_TIMESTAMPED:
            return next_record_id, self._decode_oem_nontimestamped(
                record_bytes
            )

        else:
            logger.warning(
                'Unknown SEL record type 0x%02X — skipping', record_type
            )
            return next_record_id, None

    def parse_raw_response(self, ipmitool_output: str) -> bytes:
        """
        Convert ipmitool hex string output to bytes.

        ipmitool raw command output format:
            '00 01 c0 d2 4e 13 62 4c 1c 00 86 80 2a 35 ff 50 00'
        Multi-line output is flattened before parsing.

        Args:
            ipmitool_output: stdout from ipmitool raw 0x0a 0x43 ...

        Returns:
            bytes object for struct.unpack processing
        """
        tokens = ipmitool_output.split()
        if not tokens:
            return bytes()
        try:
            return bytes(int(t, 16) for t in tokens if t)
        except ValueError as exc:
            raise ValueError(
                f'Non-hex token in SEL response: {exc}\n'
                f'Raw output: {ipmitool_output!r}'
            ) from exc

    def format_record(self, record: SelRecord) -> str:
        """
        Format a decoded record as a human-readable string.
        Suitable for log output and test assertion messages.
        """
        if isinstance(record, SelStandardRecord):
            sensor_name = SENSOR_TYPES.get(
                record.sensor_type, f'Unknown(0x{record.sensor_type:02X})'
            )
            return (
                f'SEL[{record.record_id:04X}] '
                f'{record.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")} '
                f'| {sensor_name} '
                f'| sensor=0x{record.sensor_num:02X} '
                f'| data={record.event_data}'
            )

        elif isinstance(record, SelOemPcieRecord):
            return (
                f'SEL[{record.record_id:04X}] '
                f'{record.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")} '
                f'| PCIe AER [{record.severity}] {record.description} '
                f'| VendorID=0x{record.vendor_id:04X} '
                f'DeviceID=0x{record.device_id:04X} '
                f'Riser={record.riser_slot} MBSlot={record.mb_slot}'
            )

        elif isinstance(record, SelOemNonTimestampedRecord):
            return (
                f'SEL[{record.record_id:04X}] '
                f'OEM non-timestamped type=0x{record.record_type:02X} '
                f'data={record.raw_data.hex(" ")}'
            )

        return f'SEL: unknown record type {type(record).__name__}'

    # ------------------------------------------------------------------
    # Private decoders
    # ------------------------------------------------------------------

    def _decode_standard(self,
                         record: bytes) -> SelStandardRecord:
        """
        Decode Standard System Event Record (type 0x02).
        IPMI 2.0 Table 32-1, 16 bytes.

        struct format '<HBI BBBBB bBBB':
            < = little-endian
            H = uint16  record_id       bytes 0-1
            B = uint8   record_type     byte  2
            I = uint32  timestamp       bytes 3-6
            B = uint8   gen_id_low      byte  7
            B = uint8   gen_id_high     byte  8
            B = uint8   ev_msg_rev      byte  9
            B = uint8   sensor_type     byte  10
            B = uint8   sensor_num      byte  11
            b = int8    evt_dir_type    byte  12
            B = uint8   event_data_1    byte  13
            B = uint8   event_data_2    byte  14
            B = uint8   event_data_3    byte  15
        """
        (record_id, record_type, timestamp,
         gen_id_low, gen_id_high, ev_msg_rev,
         sensor_type, sensor_num, evt_dir_type,
         ev1, ev2, ev3) = struct.unpack('<HBI BBBBB bBBB', record[:16])

        return SelStandardRecord(
            record_id    = record_id,
            record_type  = record_type,
            timestamp    = datetime.fromtimestamp(timestamp,
                                                  tz=timezone.utc),
            generator_id = (gen_id_high << 8) | gen_id_low,
            ev_msg_rev   = ev_msg_rev,
            sensor_type  = sensor_type,
            sensor_num   = sensor_num,
            evt_dir_type = evt_dir_type,
            event_data   = (ev1, ev2, ev3),
        )

    def _decode_oem_pcie(self,
                         record: bytes) -> SelOemPcieRecord:
        """
        Decode OEM Timestamped Record (type 0xC0) — PCIe AER format.

        Byte layout within the 16-byte record:
            [0:1]   Record ID        LE uint16
            [2]     Record Type      0xC0
            [3:6]   Timestamp        LE uint32
            [7:9]   Manufacturer ID  24-bit LE (3 bytes, no struct type)
            [10:11] Vendor ID        LE uint16
            [12:13] Device ID        LE uint16
            [14]    Slot Number      BCD byte
            [15]    PCIe Error ID    error category

        Note on Manufacturer ID reconstruction:
            struct has no uint24 format. Manual reconstruction uses named
            byte positions (record[7], [8], [9]) to avoid the off-by-one
            safe access that avoids index errors.
        """
        if len(record) < 16:
            raise ValueError(
                f'OEM PCIe record too short: {len(record)} bytes'
            )

        record_id,  = struct.unpack_from('<H', record, 0)
        record_type = record[2]
        timestamp,  = struct.unpack_from('<I', record, 3)

        # 24-bit LE Manufacturer ID — no struct uint24, reconstruct manually
        # record[7] = low byte, record[8] = mid byte, record[9] = high byte
        manufacturer_id = (
            (record[9] << 16) |
            (record[8] << 8)  |
             record[7]
        )

        # Vendor ID at bytes 10-11 (LE uint16)
        # record[10] = low byte, record[11] = high byte
        # struct '<H' reads LSB-first automatically
        vendor_id, = struct.unpack_from('<H', record, 10)

        # Device ID at bytes 12-13 (LE uint16)
        device_id, = struct.unpack_from('<H', record, 12)

        slot_byte  = record[14]
        error_id   = record[15]

        # decode BCD slot number
        # bits[7:5] = riser slot (0-7)
        # bits[4:0] = MB slot number (0-31)
        riser_slot = (slot_byte >> 5) & 0x07
        mb_slot    = slot_byte & 0x1F

        # look up PCIe AER error description
        severity, description = PCIE_AER_ERRORS.get(
            error_id,
            ('UNKNOWN', f'Unrecognized Error ID 0x{error_id:02X}')
        )

        return SelOemPcieRecord(
            record_id       = record_id,
            record_type     = record_type,
            timestamp       = datetime.fromtimestamp(timestamp,
                                                     tz=timezone.utc),
            manufacturer_id = manufacturer_id,
            vendor_id       = vendor_id,
            device_id       = device_id,
            slot_number     = slot_byte,
            riser_slot      = riser_slot,
            mb_slot         = mb_slot,
            pcie_error_id   = error_id,
            severity        = severity,
            description     = description,
        )

    def _decode_oem_nontimestamped(
            self, record: bytes) -> SelOemNonTimestampedRecord:
        """
        Decode OEM Non-Timestamped Record (types 0xC1-0xFF).
        IPMI 2.0 Section 32.3.
        Bytes 3-15 are entirely OEM-defined — only type is reliable.
        """
        record_id, = struct.unpack_from('<H', record, 0)
        return SelOemNonTimestampedRecord(
            record_id   = record_id,
            record_type = record[2],
            raw_data    = bytes(record[3:]),
        )
