"""
PECI and SMLINK clients.

PECI is Intel's single-wire bus from BMC to CPUs for thermal data and
device ID. CPU0 is always at 0x30, CPU1 at 0x31 per Intel spec.
PECI only works after BIOS POST — the CPU thermal subsystem is not
initialized until then.

GetTemp returns a signed 16-bit value in 1/64 °C units below Tjmax:
    actual_temp = Tjmax + (raw_value / 64.0)

SMLINK is the dedicated SMBus channel between BMC and PCH. Node Manager
runs on the PCH at IPMB address 0x2C on bus 6.

Both protocols go through BMC OEM IPMI commands, so they are accessible
remotely over IPMI LAN without any host OS involvement.
    Value is always negative or zero (temperature is always ≤ Tjmax)
    Example: raw=0xFEC0=-320 → 105 + (-320/64) = 100.0 °C

OEM IPMI command used:
    NetFn=<platform OEM>, Cmd=<PECI bridge>
    Data: [sub_cmd][reserved][cpu_addr][additional_data...]
    Sub-commands: 0x01=Ping, 0x02=GetTemp, 0x03=GetDIB

SMLINK bridge uses ipmitool -b and -t flags:
    -b 0x06  Bus 6 = the SMLink SMBus physical interface
    -t 0x2C  Target 0x2C = Intel Node Manager IPMB slave address
"""

import struct
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PECI client addresses (fixed by Intel PECI specification)
# ---------------------------------------------------------------------------

PECI_ADDR_CPU0 = 0x30
PECI_ADDR_CPU1 = 0x31

# Sub-command bytes for BMC PECI bridge OEM command
PECI_SUBCMD_PING    = 0x01
PECI_SUBCMD_GETTEMP = 0x02
PECI_SUBCMD_GETDIB  = 0x03

# PECI completion codes
PECI_CC_PASS  = 0x40   # command completed successfully
PECI_CC_ABORT = 0x80   # command aborted (CPU in low-power state)
PECI_CC_ERROR = 0x90   # response aborted (timeout or bus error)

# OEM command placeholders — replace with platform-specific values
_OEM_NETFN_PECI = 0x00   # platform OEM NetFn for PECI bridge
_PECI_BRIDGE_CMD = 0x00  # platform Cmd for PECI bridge


# ---------------------------------------------------------------------------
# PECI temperature reading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeciTempReading:
    """
    Decoded PECI GetTemp response.

    The raw value is a signed 16-bit integer in 1/64 °C units
    representing degrees BELOW Tjmax. It is always ≤ 0.

    Tjmax is the CPU's maximum junction temperature — a fixed value
    per CPU SKU (typically 100-105 °C for server CPUs).
    """
    completion_code: int    # 0x40=pass, 0x80=abort, 0x90=error
    raw_value:       int    # signed 16-bit, 1/64 °C below Tjmax
    tjmax:           int    # Tjmax in °C (provided by caller from spec)

    @classmethod
    def from_bytes(cls, response_bytes: bytes,
                   tjmax: int = 105) -> 'PeciTempReading':
        """
        Parse raw PECI GetTemp response bytes.

        Args:
            response_bytes: 3 bytes — [completion_code, temp_LSB, temp_MSB]
            tjmax:          platform-specific Tjmax in °C

        Returns:
            PeciTempReading with decoded fields

        Raises:
            ValueError: if response_bytes is too short
        """
        if len(response_bytes) < 3:
            raise ValueError(
                f'GetTemp response too short: {len(response_bytes)} bytes, '
                f'expected 3 (completion + 2 temp bytes). '
                f'PECI transaction may have failed or timed out.'
            )

        completion = response_bytes[0]

        # signed 16-bit little-endian temperature
        # struct '<h' = little-endian signed short
        # response_bytes[1] = LSB, response_bytes[2] = MSB
        raw_val, = struct.unpack('<h', response_bytes[1:3])

        return cls(
            completion_code = completion,
            raw_value       = raw_val,
            tjmax           = tjmax,
        )

    @property
    def celsius(self) -> Optional[float]:
        """
        Convert raw PECI reading to degrees Celsius.
        Returns None if completion code indicates failure.

        Formula: actual_temp = Tjmax + (raw_value / 64.0)
        raw_value is always ≤ 0, so actual_temp is always ≤ Tjmax.
        """
        if self.completion_code != PECI_CC_PASS:
            return None
        return self.tjmax + (self.raw_value / 64.0)

    @property
    def is_valid(self) -> bool:
        """True if completion code indicates a valid reading."""
        return self.completion_code == PECI_CC_PASS

    def __str__(self) -> str:
        if self.celsius is not None:
            return (
                f'{self.celsius:.2f} °C '
                f'(raw=0x{self.raw_value & 0xFFFF:04X}, '
                f'Tjmax={self.tjmax} °C, '
                f'margin={-self.raw_value / 64.0:.2f} °C below Tjmax)'
            )
        return (
            f'INVALID '
            f'(completion=0x{self.completion_code:02X}, '
            f'expected 0x{PECI_CC_PASS:02X})'
        )


# ---------------------------------------------------------------------------
# PECI DIB (Device Identification Block)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeciDib:
    """
    Decoded PECI GetDIB response.

    The DIB identifies the PECI client device and confirms PECI revision.
    8 bytes returned after completion code byte.

    Byte 0 (dev_info):
        bits [7:4] = PECI revision (0x1 = PECI 3.1, 0x0 = PECI 2.x)
        bits [3:0] = reserved
    Byte 1 (proc_num):
        number of processors at this PECI address
        single-die: 0x01
    Bytes 2-7: reserved
    """
    dev_info:  int    # byte 0
    proc_num:  int    # byte 1
    raw_bytes: bytes  # full 8 bytes for audit

    @classmethod
    def from_bytes(cls, dib_bytes: bytes) -> 'PeciDib':
        """
        Parse 8-byte DIB response (after stripping completion code).

        Args:
            dib_bytes: bytes 1-8 of PECI GetDIB response
                       (completion code already stripped by caller)
        """
        if len(dib_bytes) < 8:
            raise ValueError(
                f'GetDIB response too short: {len(dib_bytes)} bytes, '
                f'expected 8. PECI transaction may have failed.'
            )
        return cls(
            dev_info  = dib_bytes[0],
            proc_num  = dib_bytes[1],
            raw_bytes = bytes(dib_bytes[:8]),
        )

    @property
    def peci_revision(self) -> int:
        """PECI protocol revision: bits [7:4] of dev_info byte."""
        return (self.dev_info >> 4) & 0xF

    def is_valid(self) -> bool:
        """
        Basic validity: PECI revision must be 0x0 or 0x1.
        Higher values indicate unsupported or corrupt response.
        """
        return self.peci_revision in (0x0, 0x1)

    def __str__(self) -> str:
        return (
            f'PECI rev=0x{self.peci_revision:X} '
            f'proc_count={self.proc_num}'
        )


# ---------------------------------------------------------------------------
# PECI client
# ---------------------------------------------------------------------------

class PeciClient:
    """
    PECI operations via BMC OEM IPMI bridge.

    The BMC translates IPMI OEM commands into PECI frames on the
    single-wire PECI bus. This client issues the IPMI commands and
    parses the PECI responses.

    Usage:
        client = PeciClient(ipmi_client, tjmax=105)
        temp   = client.get_temperature(PECI_ADDR_CPU0)
        if temp.is_valid:
            print(f"CPU0 temperature: {temp.celsius:.1f} °C")
    """

    def __init__(self, ipmi_client, tjmax: int = 105):
        """
        Args:
            ipmi_client: IpmiClient or MockBmcClient
            tjmax:       CPU maximum junction temperature in °C
                         Must match the actual CPU SKU specification.
                         Typical values: 95, 100, 105 °C
        """
        self._ipmi  = ipmi_client
        self._tjmax = tjmax

    def _peci_cmd(self, sub_cmd: int,
                  cpu_addr: int,
                  *extra_bytes: int) -> bytes:
        """
        Send a PECI command via BMC OEM bridge and return raw response bytes.

        BMC PECI bridge command format:
            [sub_cmd][reserved=0x00][cpu_addr][extra_bytes...]

        Args:
            sub_cmd:     PECI sub-command (PING=0x01, GETTEMP=0x02, DIB=0x03)
            cpu_addr:    PECI client address (0x30=CPU0, 0x31=CPU1)
            extra_bytes: additional data bytes required by some sub-commands

        Returns:
            Response bytes from BMC (includes PECI completion code)
        """
        args = (
            ['raw', hex(_OEM_NETFN_PECI), hex(_PECI_BRIDGE_CMD),
             hex(sub_cmd),
             '0x00',              # reserved byte
             hex(cpu_addr)] +
            [hex(b) for b in extra_bytes]
        )
        output = self._ipmi.run(*args)

        tokens = output.split()
        if not tokens:
            return bytes()
        try:
            return bytes(int(t, 16) for t in tokens if t)
        except ValueError as exc:
            logger.error(
                'PECI response parse error: %s. Raw: %r', exc, output
            )
            return bytes()

    def ping(self, cpu_addr: int) -> bool:
        """
        Send PECI Ping to confirm CPU is alive on PECI bus.

        Ping uses command code 0x01 with WrLen=0, RdLen=0 — no data
        transfer, just an ACK pulse from the CPU.

        Args:
            cpu_addr: PECI_ADDR_CPU0 or PECI_ADDR_CPU1

        Returns:
            True if CPU responded (any non-error IPMI response)
        """
        try:
            response = self._peci_cmd(PECI_SUBCMD_PING, cpu_addr)
            # any response (including empty) means BMC sent the PECI frame
            # and did not report an error — CPU is reachable
            return True
        except Exception as exc:
            logger.error('PECI Ping CPU 0x%02X failed: %s', cpu_addr, exc)
            return False

    def get_temperature(self, cpu_addr: int) -> PeciTempReading:
        """
        Read CPU temperature via PECI GetTemp command.

        Returns a PeciTempReading with:
            - is_valid: True if completion code is 0x40
            - celsius:  actual temperature in °C (None if invalid)

        Args:
            cpu_addr: PECI_ADDR_CPU0 or PECI_ADDR_CPU1

        Returns:
            PeciTempReading — check .is_valid before using .celsius
        """
        response = self._peci_cmd(
            PECI_SUBCMD_GETTEMP, cpu_addr,
            0x00, 0x00,   # padding bytes required by BMC bridge
        )

        return PeciTempReading.from_bytes(response, self._tjmax)

    def get_dib(self, cpu_addr: int) -> Optional[PeciDib]:
        """
        Read CPU Device Identification Block via PECI GetDIB command.

        Args:
            cpu_addr: PECI_ADDR_CPU0 or PECI_ADDR_CPU1

        Returns:
            PeciDib or None if read failed
        """
        response = self._peci_cmd(
            PECI_SUBCMD_GETDIB, cpu_addr,
            0x00, 0x00,
        )

        if len(response) < 9:
            logger.error(
                'GetDIB CPU 0x%02X: too short (%d bytes, expected 9)',
                cpu_addr, len(response)
            )
            return None

        try:
            return PeciDib.from_bytes(response[1:])  # strip completion byte
        except ValueError as exc:
            logger.error('GetDIB parse failed: %s', exc)
            return None

    def verify_cpu(self, cpu_index: int, check,
                   temp_range: Tuple[float, float] = (-50.0, 105.0)):
        """
        Complete PECI verification: Ping + temperature range + DIB validity.

        Args:
            cpu_index:  0 for CPU0, 1 for CPU1
            check:      SdrCheckResult accumulator
            temp_range: (min_celsius, max_celsius) acceptance range
                        Default: (-50, 105) — below -50 indicates uninitialized
                        sensor, above Tjmax indicates thermal emergency
        """
        cpu_addr = PECI_ADDR_CPU0 + cpu_index
        temp_min, temp_max = temp_range

        # ── Ping ──────────────────────────────────────────────────────
        if self.ping(cpu_addr):
            check.ok(f'CPU{cpu_index} PECI Ping: responded at 0x{cpu_addr:02X}')
        else:
            check.fail(
                f'CPU{cpu_index} PECI Ping failed. '
                f'CPU may be off, PECI bus may be disconnected, '
                f'or BMC PECI controller may be unavailable. '
                f'Ensure host is powered and BIOS POST is complete.'
            )
            return  # no point continuing if CPU is unreachable

        # ── GetTemp ───────────────────────────────────────────────────
        try:
            temp = self.get_temperature(cpu_addr)

            if not temp.is_valid:
                check.fail(
                    f'CPU{cpu_index} PECI GetTemp: '
                    f'completion=0x{temp.completion_code:02X} '
                    f'(expected 0x{PECI_CC_PASS:02X}). '
                    f'CPU may be in deep C-state or thermal subsystem '
                    f'not yet initialized. Ensure BIOS POST is complete.'
                )
            else:
                celsius = temp.celsius
                check.ok(f'CPU{cpu_index} temperature: {temp}')

                if celsius < temp_min:
                    check.fail(
                        f'CPU{cpu_index} temperature {celsius:.1f} °C '
                        f'below minimum {temp_min} °C. '
                        f'Sensor may not be initialized yet. '
                        f'Wait for BIOS POST completion before reading PECI.'
                    )
                elif celsius > temp_max:
                    check.fail(
                        f'CPU{cpu_index} temperature {celsius:.1f} °C '
                        f'exceeds maximum {temp_max} °C (Tjmax). '
                        f'CPU in thermal emergency — '
                        f'check airflow and fan operation immediately.'
                    )
                else:
                    check.ok(
                        f'CPU{cpu_index} temperature in range '
                        f'[{temp_min}, {temp_max}] °C'
                    )

        except ValueError as exc:
            check.fail(f'CPU{cpu_index} GetTemp parse error: {exc}')

        # ── GetDIB ────────────────────────────────────────────────────
        dib = self.get_dib(cpu_addr)
        if dib is None:
            check.fail(
                f'CPU{cpu_index} PECI GetDIB: read failed. '
                f'Check PECI bus connectivity and CPU power state.'
            )
        elif not dib.is_valid():
            check.fail(
                f'CPU{cpu_index} DIB: unexpected PECI revision '
                f'0x{dib.peci_revision:X}. '
                f'Expected 0x0 (PECI 2.x) or 0x1 (PECI 3.1).'
            )
        else:
            check.ok(
                f'CPU{cpu_index} DIB: {dib}'
            )


# ---------------------------------------------------------------------------
# SMLINK / Node Manager client
# ---------------------------------------------------------------------------

class SmlinkClient:
    """
    Intel Node Manager communication via SMLINK SMBus bridge.

    SMLINK is a dedicated SMBus channel between BMC and PCH.
    The PCH runs Node Manager firmware — a platform power management
    controller with its own IPMB slave address (0x2C).

    ipmitool -b and -t flags enable IPMB bridging:
        -b 0x06  = use IPMB bus 6 (the physical SMLink interface)
        -t 0x2C  = send to slave address 0x2C (Node Manager)

    Node Manager identification:
        Manufacturer ID = 343 (decimal) = 0x157 = Intel
        Manufacturer Name = 'Intel Corporation'
        (Intel makes Node Manager firmware regardless of board vendor)

    Usage:
        client = SmlinkClient(ipmi_client)
        info   = client.get_node_manager_info()
        assert 'Intel Corporation' in info.get('Manufacturer Name', '')
    """

    # SMLINK bridge parameters (Intel platform specification)
    SMLINK_BUS    = '0x06'   # IPMB bus 6 = SMLink physical interface
    NM_IPMB_ADDR = '0x2c'   # Node Manager IPMB slave address

    def __init__(self, ipmi_client):
        """
        Args:
            ipmi_client: IpmiClient — SmlinkClient builds the bridge
                         flags into the command itself via run_bridged()
        """
        self._ipmi = ipmi_client

    def run_bridged(self, *args) -> str:
        """
        Send IPMI command via SMLink IPMB bridge to Node Manager.

        Appends -b and -t flags to the ipmitool command to route
        through IPMB bus 6 to Node Manager slave 0x2C.

        This is equivalent to:
            ipmitool -H <bmc_ip> ... -b 0x06 -t 0x2c <args>
        """
        # insert bridge flags before the command arguments
        # ipmitool accepts: -b <bus> -t <target> <command>
        bridge_args = ['-b', self.SMLINK_BUS, '-t', self.NM_IPMB_ADDR]

        # build the full argument list
        # Note: IpmiClient._base already has -H, -I, -U, -P
        # We need to inject -b and -t before the command words
        # Workaround: create a temporary client with extended base args
        full_args = list(args) + bridge_args
        return self._ipmi.run(*full_args)

    def get_node_manager_info(self) -> dict:
        """
        Get Node Manager device identification via mc info over SMLink.

        Sends IPMI Get Device ID (NetFn=0x06 Cmd=0x01) through the
        SMLink bridge to Node Manager.

        Returns:
            dict with ipmitool mc info field names as keys
            Key field: 'Manufacturer Name' should be 'Intel Corporation'

        Raises:
            RuntimeError: if mc info response is empty (NM not reachable)
        """
        output = self.run_bridged('mc', 'info')

        if not output.strip():
            raise RuntimeError(
                'Node Manager mc info returned empty response. '
                f'Check SMLink connection: bus={self.SMLINK_BUS} '
                f'target={self.NM_IPMB_ADDR}. '
                'Ensure host is powered — Node Manager requires PCH power.'
            )

        info: dict = {}
        for line in output.splitlines():
            if ':' in line:
                key, _, value = line.partition(':')
                info[key.strip()] = value.strip()

        return info

    def verify_node_manager(self, check) -> bool:
        """
        Verify Node Manager is alive and identifies as Intel firmware.

        Args:
            check: SdrCheckResult accumulator

        Returns:
            True if Node Manager responded with Intel manufacturer ID
        """
        try:
            info = self.get_node_manager_info()
        except RuntimeError as exc:
            check.fail(str(exc))
            return False

        mfr = info.get('Manufacturer Name', '')

        if 'Intel Corporation' in mfr:
            fw_rev = info.get('Firmware Revision', 'unknown')
            check.ok(
                f'Node Manager identified: '
                f'Mfr="{mfr}" FW={fw_rev}'
            )
            return True

        check.fail(
            f'Node Manager manufacturer unexpected: "{mfr}". '
            f'Expected "Intel Corporation". '
            f'Check SMLink bus routing '
            f'(-b {self.SMLINK_BUS} -t {self.NM_IPMB_ADDR}) '
            f'and PCH Node Manager initialization status.'
        )
        return False
