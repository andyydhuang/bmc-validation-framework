"""
JTAG and ASD (At-Scale Debug) interface via BMC OEM IPMI commands.

The BMC acts as a JTAG master with a hardware MUX that routes signals to
different targets (CPUs, PCH, accelerators). Every read is a three-step
sequence: set ASD mode → route MUX → read IDCODE.

IDCODE bit layout (IEEE 1149.1):
    [31:28] version (silicon stepping)
    [27:12] part number
    [11:1]  manufacturer ID (JEDEC)
    [0]     always 1

The CPU chain is daisy-chained — both CPUs share one MUX target.
Accelerator slots each have their own independent chain.

OEM NetFn/Cmd values are platform-specific and replaced with 0x00
placeholders. See KNOWN_IDCODES and JtagMuxTarget for other values
that also need updating for your platform.
    IDCODE read:    raw <OEM_NETFN> <JTAG_IDCODE_CMD> <device_index>

Note on OEM command encoding:
    NetFn and Cmd values are intentionally not hardcoded in this
    published version. In production, these come from the platform
    IPMI OEM specification. The structure and logic are preserved.
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JTAG IDCODE dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JtagIdcode:
    """
    Decoded 32-bit JTAG IDCODE register value.
    IEEE 1149.1 Section 12.1.1 mandatory register.

    All devices implementing IEEE 1149.1 must have an IDCODE register.
    On TAP reset, IDCODE loads automatically — no explicit instruction needed.
    """
    raw_value:   int    # full 32-bit value
    version:     int    # bits [31:28] — silicon stepping/revision
    part_number: int    # bits [27:12] — device part number
    mfr_id:      int    # bits [11:1]  — JEDEC manufacturer ID
    lsb:         int    # bit  [0]     — always 1 per IEEE 1149.1

    @classmethod
    def from_int(cls, value: int) -> 'JtagIdcode':
        """Decode a 32-bit integer into named IDCODE fields."""
        return cls(
            raw_value   = value,
            version     = (value >> 28) & 0xF,
            part_number = (value >> 12) & 0xFFFF,
            mfr_id      = (value >> 1)  & 0x7FF,
            lsb         = value & 0x1,
        )

    def is_valid(self) -> bool:
        """
        IEEE 1149.1 compliance check: bit 0 of IDCODE must always be 1.
        A value of 0x00000000 (all zeros) indicates no device or
        broken scan chain.
        A value of 0xFFFFFFFF (all ones) indicates BYPASS register
        loaded instead of IDCODE.
        """
        return (self.lsb == 1 and
                self.raw_value != 0x00000000 and
                self.raw_value != 0xFFFFFFFF)

    def __str__(self) -> str:
        return (
            f'0x{self.raw_value:08X} '
            f'(version=0x{self.version:X} '
            f'part=0x{self.part_number:04X} '
            f'mfr=0x{self.mfr_id:03X})'
        )


# ---------------------------------------------------------------------------
# Known IDCODE values
# Register these for your specific platform silicon.
# These are example values — replace with values from your platform spec.
# ---------------------------------------------------------------------------

KNOWN_IDCODES: Dict[str, JtagIdcode] = {
    # Replace 0x00000000 placeholders with actual IDCODE values from your platform spec.
    # BYPASS and NO_DEVICE are standard IEEE 1149.1 sentinel values — do not change.
    'CPU_PRIMARY':     JtagIdcode.from_int(0x00000000),  # replace with actual CPU primary IDCODE
    'CPU_SECONDARY':   JtagIdcode.from_int(0x00000000),  # replace with actual CPU secondary IDCODE
    'PCH':             JtagIdcode.from_int(0x00000000),  # replace with actual PCH IDCODE
    'ACCEL_VARIANT_A': JtagIdcode.from_int(0x00000000),  # replace with actual accelerator variant A IDCODE
    'ACCEL_VARIANT_B': JtagIdcode.from_int(0x00000000),  # replace with actual accelerator variant B IDCODE
    'BYPASS':          JtagIdcode.from_int(0xFFFFFFFF),  # IEEE 1149.1 BYPASS sentinel — do not change
    'NO_DEVICE':       JtagIdcode.from_int(0x00000000),  # TDO floating sentinel — do not change
}

# Set of valid IDCODE values for accelerator slots
# Both variants are acceptable — different silicon revisions
VALID_ACCEL_IDCODES = {
    KNOWN_IDCODES['ACCEL_VARIANT_A'].raw_value,
    KNOWN_IDCODES['ACCEL_VARIANT_B'].raw_value,
}


# ---------------------------------------------------------------------------
# MUX target encoding
# ---------------------------------------------------------------------------

class JtagMuxTarget:
    """
    JTAG MUX routing targets.
    Values are platform-specific — defined by BMC OEM IPMI specification.
    Replace with actual values from your platform documentation.
    """
    ACCEL_BASE  = 0x00   # replace with actual MUX base target for accelerator slots
                          # slot N = ACCEL_BASE + N (for N in 0-7)
    CPU_CHAIN   = 0x00   # replace with actual MUX target for CPU scan chain
    PCH         = 0x00   # replace with actual MUX target for PCH scan chain


# ---------------------------------------------------------------------------
# OEM command encoding placeholders
# ---------------------------------------------------------------------------

# These values are intentionally set to 0x00 in the published version.
# In production use, replace with the actual OEM NetFn and Cmd bytes
# from your platform IPMI OEM specification document.
_OEM_NETFN_JTAG  = 0x00   # replace with platform OEM NetFn for JTAG
_JTAG_MODE_CMD   = 0x00   # replace with platform Cmd for JTAG mode set
_JTAG_MUX_CMD    = 0x00   # replace with platform Cmd for MUX routing
_JTAG_IDCODE_CMD = 0x00   # replace with platform Cmd for IDCODE read
_ASD_MODE_VALUE  = 0x01   # value that selects ASD mode
_MODE_PARAM      = 0x00   # parameter index for mode command


# ---------------------------------------------------------------------------
# JTAG client
# ---------------------------------------------------------------------------

class JtagClient:
    """
    JTAG operations via BMC OEM IPMI commands.

    Encapsulates the three-step access sequence for every JTAG operation:
        1. Set ASD (At-Scale Debug) mode on BMC JTAG controller
        2. Route MUX to the target scan chain
        3. Read IDCODE register from the target device

    The BMC firmware handles all TAP state machine transitions, clock
    generation, and scan chain shift operations internally. This client
    only needs to issue the high-level OEM commands.

    Usage:
        client = JtagClient(ipmi_client)
        idcode = client.read_cpu_idcode(cpu_index=0)
        if idcode and idcode.raw_value == KNOWN_IDCODES['CPU_PRIMARY'].raw_value:
            print(f"CPU0 verified: {idcode}")
    """

    def __init__(self, ipmi_client):
        """
        Args:
            ipmi_client: IpmiClient or MockBmcClient
        """
        self._ipmi = ipmi_client

    def _set_asd_mode(self):
        """
        Switch BMC JTAG controller to ASD (At-Scale Debug) mode.

        ASD mode is required before routing the MUX to any CPU or PCH
        target. It configures the BMC JTAG engine to operate as an Intel
        ASD controller rather than in raw JTAG bit-bang mode.
        """
        self._ipmi.run(
            'raw',
            hex(_OEM_NETFN_JTAG),
            hex(_JTAG_MODE_CMD),
            hex(_MODE_PARAM),
            hex(_ASD_MODE_VALUE),
        )
        logger.debug('JTAG: ASD mode set')

    def _route_mux(self, target: int):
        """
        Route JTAG MUX to the specified target scan chain.

        Args:
            target: MUX target byte — JtagMuxTarget.CPU_CHAIN,
                    JtagMuxTarget.PCH, or JtagMuxTarget.ACCEL_BASE + slot
        """
        self._ipmi.run(
            'raw',
            hex(_OEM_NETFN_JTAG),
            hex(_JTAG_MUX_CMD),
            hex(target),
        )
        logger.debug('JTAG: MUX → 0x%02X', target)

    def _read_idcode_raw(self, device_index: int) -> str:
        """
        Read IDCODE from device at given index in the current scan chain.

        Args:
            device_index: position in the daisy chain (0 = first device)

        Returns:
            raw ipmitool stdout string containing IDCODE bytes
        """
        return self._ipmi.run(
            'raw',
            hex(_OEM_NETFN_JTAG),
            hex(_JTAG_IDCODE_CMD),
            hex(device_index),
        )

    def _parse_idcode(self, raw_output: str) -> Optional[JtagIdcode]:
        """
        Parse IDCODE from raw ipmitool hex output.

        BMC OEM IDCODE command returns the 32-bit IDCODE as ASCII hex
        characters. ipmitool then prints those as hex byte values.

        Example: IDCODE 0x20044113
            BMC returns bytes: b'2' b'0' b'0' b'4' b'4' b'1' b'1' b'3'
            ipmitool prints:   "32 30 30 34 34 31 31 33"
            Parse: chr(0x32)='2', chr(0x30)='0', ... → "20044113"
            Result: int("20044113", 16) = 0x20044113

        Args:
            raw_output: space-separated hex byte string from ipmitool

        Returns:
            JtagIdcode if parse succeeds, None on malformed input
        """
        tokens = raw_output.strip().split()
        if not tokens:
            logger.warning('JTAG IDCODE: empty response')
            return None

        try:
            # each token is hex representation of one ASCII character
            # e.g. '32' → chr(int('32',16)) → chr(50) → '2'
            ascii_chars = [
                bytes.fromhex(t).decode('ascii') for t in tokens
            ]
        except (ValueError, UnicodeDecodeError) as exc:
            logger.error(
                'JTAG IDCODE parse failed: %s. Raw: %r', exc, raw_output
            )
            return None

        idcode_hex = ''.join(ascii_chars)

        try:
            idcode_int = int(idcode_hex, 16)
        except ValueError as exc:
            logger.error(
                'JTAG IDCODE hex conversion failed: %r: %s', idcode_hex, exc
            )
            return None

        idcode = JtagIdcode.from_int(idcode_int)
        logger.debug('JTAG IDCODE parsed: %s', idcode)
        return idcode

    def read_cpu_idcode(self, cpu_index: int) -> Optional[JtagIdcode]:
        """
        Read JTAG IDCODE from CPU0 or CPU1.

        Both CPUs share one scan chain (daisy-chained). The BMC firmware
        extracts the correct IDCODE based on device_index parameter:
            device_index=0 → CPU0 (second in chain, index from BMC TDI)
            device_index=1 → CPU1 (first in chain, closest to BMC TDO)

        Args:
            cpu_index: 0 for CPU0, 1 for CPU1

        Returns:
            JtagIdcode or None if read failed
        """
        if cpu_index not in (0, 1):
            raise ValueError(
                f'cpu_index must be 0 or 1, got {cpu_index}'
            )

        self._set_asd_mode()
        self._route_mux(JtagMuxTarget.CPU_CHAIN)
        raw   = self._read_idcode_raw(cpu_index)
        idcode = self._parse_idcode(raw)

        logger.info(
            'CPU%d IDCODE: %s',
            cpu_index,
            str(idcode) if idcode else 'read failed'
        )
        return idcode

    def read_pch_idcode(self) -> Optional[JtagIdcode]:
        """
        Read JTAG IDCODE from the PCH (Platform Controller Hub).

        PCH has its own dedicated scan chain (MUX target PCH).
        device_index=0 because PCH is the only device on its chain.
        """
        self._set_asd_mode()
        self._route_mux(JtagMuxTarget.PCH)
        raw    = self._read_idcode_raw(0)
        idcode = self._parse_idcode(raw)

        logger.info(
            'PCH IDCODE: %s',
            str(idcode) if idcode else 'read failed'
        )
        return idcode

    def read_accel_idcode(self, slot: int) -> Optional[JtagIdcode]:
        """
        Read JTAG IDCODE from an accelerator module slot.

        Each accelerator slot has its own independent scan chain.
        device_index is always 0 (single device per chain).

        Args:
            slot: accelerator slot number 0-7

        Returns:
            JtagIdcode or None if read failed
        """
        if not 0 <= slot <= 7:
            raise ValueError(f'Slot must be 0-7, got {slot}')

        self._set_asd_mode()
        self._route_mux(JtagMuxTarget.ACCEL_BASE + slot)
        raw    = self._read_idcode_raw(0)
        idcode = self._parse_idcode(raw)

        logger.info(
            'Accel slot %d IDCODE: %s',
            slot,
            str(idcode) if idcode else 'read failed'
        )
        return idcode


# ---------------------------------------------------------------------------
# JTAG verifier — validation logic separate from transport
# ---------------------------------------------------------------------------

class JtagVerifier:
    """
    JTAG IDCODE verification logic.

    Separated from JtagClient so transport (how to read IDCODE) and
    validation (what the IDCODE should be) can be tested independently.

    Usage:
        client   = JtagClient(ipmi_client)
        verifier = JtagVerifier(client)
        check    = SdrCheckResult()
        verifier.verify_cpu(cpu_index=0, check=check)
    """

    def __init__(self, jtag_client: JtagClient):
        self._jtag = jtag_client

    def verify_cpu(self, cpu_index: int, check) -> bool:
        """
        Verify CPU IDCODE matches expected value.

        Distinguishes three failure modes with specific messages:
            - BYPASS (0xFFFFFFFF): scan chain is passing through without
              the CPU responding — broken connection or CPU not powered
            - ALL_ZEROS (0x00000000): TDO line is floating or stuck
            - Wrong IDCODE: unexpected silicon revision or wrong chip

        Args:
            cpu_index: 0 for CPU0, 1 for CPU1
            check:     SdrCheckResult accumulator

        Returns:
            True if IDCODE matches expected value
        """
        expected = KNOWN_IDCODES['CPU_PRIMARY']
        idcode   = self._jtag.read_cpu_idcode(cpu_index)

        if idcode is None:
            check.fail(
                f'CPU{cpu_index} JTAG IDCODE read returned no data. '
                f'Check ASD mode setting and MUX routing.'
            )
            return False

        if not idcode.is_valid():
            if idcode.raw_value == 0xFFFFFFFF:
                check.fail(
                    f'CPU{cpu_index} returned BYPASS (0xFFFFFFFF). '
                    f'CPU may not be installed or scan chain is open. '
                    f'Check physical seating and JTAG trace continuity.'
                )
            else:
                check.fail(
                    f'CPU{cpu_index} returned 0x{idcode.raw_value:08X} '
                    f'which violates IEEE 1149.1 (bit 0 must be 1). '
                    f'TDO line may be floating or stuck low.'
                )
            return False

        if idcode.raw_value == expected.raw_value:
            check.ok(f'CPU{cpu_index} IDCODE verified: {idcode}')
            return True

        check.fail(
            f'CPU{cpu_index} IDCODE mismatch: '
            f'expected 0x{expected.raw_value:08X} '
            f'got 0x{idcode.raw_value:08X}. '
            f'Expected stepping 0x{expected.version:X}, '
            f'got 0x{idcode.version:X}. '
            f'Possible causes: wrong CPU stepping, wrong silicon variant, '
            f'or JTAG MUX routing failure.'
        )
        return False

    def verify_pch(self, check) -> bool:
        """
        Verify PCH IDCODE.

        Args:
            check: SdrCheckResult accumulator

        Returns:
            True if IDCODE matches expected PCH value
        """
        expected = KNOWN_IDCODES['PCH']
        idcode   = self._jtag.read_pch_idcode()

        if idcode is None:
            check.fail(
                'PCH IDCODE read returned no data. '
                'Check MUX routing and PCH power state.'
            )
            return False

        if not idcode.is_valid():
            check.fail(
                f'PCH IDCODE 0x{idcode.raw_value:08X} invalid '
                f'(IEEE 1149.1 bit 0 = {idcode.lsb}). '
                f'Check PCH scan chain connection.'
            )
            return False

        if idcode.raw_value == expected.raw_value:
            check.ok(f'PCH IDCODE verified: {idcode}')
            return True

        check.fail(
            f'PCH IDCODE mismatch: '
            f'expected 0x{expected.raw_value:08X} '
            f'got 0x{idcode.raw_value:08X}'
        )
        return False

    def verify_all_accel_slots(self, check) -> Dict[int, bool]:
        """
        Verify JTAG IDCODE for all 8 accelerator slots.

        Accepts either of two known IDCODE variants — different silicon
        revisions of the same accelerator component are both valid.

        Does NOT short-circuit on failure — all 8 slots are always tested
        to give a complete picture of which slots have issues.

        Args:
            check: SdrCheckResult accumulator

        Returns:
            dict mapping slot_index → pass (True) / fail (False)
        """
        results: Dict[int, bool] = {}

        for slot in range(8):
            idcode = self._jtag.read_accel_idcode(slot)

            if idcode is None:
                check.fail(
                    f'Accel slot {slot} IDCODE read returned no data. '
                    f'Check slot {slot} physical presence.'
                )
                results[slot] = False
                continue

            if not idcode.is_valid():
                check.fail(
                    f'Accel slot {slot} IDCODE 0x{idcode.raw_value:08X} '
                    f'is not IEEE 1149.1 compliant (bit 0 = {idcode.lsb}). '
                    f'Check scan chain connection for slot {slot}.'
                )
                results[slot] = False
                continue

            if idcode.raw_value in VALID_ACCEL_IDCODES:
                check.ok(
                    f'Accel slot {slot} IDCODE verified: {idcode}'
                )
                results[slot] = True
            else:
                check.fail(
                    f'Accel slot {slot} IDCODE 0x{idcode.raw_value:08X} '
                    f'not in valid set '
                    f'{[hex(v) for v in VALID_ACCEL_IDCODES]}. '
                    f'Possible unrecognized silicon variant.'
                )
                results[slot] = False

        passed = sum(1 for ok in results.values() if ok)
        logger.info(
            'Accel JTAG scan: %d/8 slots verified', passed
        )
        return results
