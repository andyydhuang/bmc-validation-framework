"""
Fan speed control and TACH readback.

Eight fans, two CPLDs (bottom and upper tray), each fan with two rotors
(inlet _0, outlet _1). PWM duty cycle is set via IPMI OEM command.
TACH counts come back via two paths:

    CPLD direct — read TACH register via IPMI Master Write-Read (~1s update)
    SDR         — BMC sensor polling (~5-30s update, polling cycle dependent)

FanSpeedVerifier runs a sweep (100% → 50% → 10%) and cross-validates both
paths at each point. try/finally restores auto-mode even if a test crashes
mid-sweep — otherwise fans can get stuck at 10% duty.

All OEM register addresses and auth bytes are 0xNN placeholders.
Replace with actual values from your platform CPLD specification.
"""

import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardware constants
# ---------------------------------------------------------------------------

class FanTray(Enum):
    """
    Physical fan tray identifiers.
    BOTTOM controls even-indexed fans (0,2,4,6).
    UPPER  controls odd-indexed fans  (1,3,5,7).
    """
    BOTTOM = 'bot'
    UPPER  = 'up'


# Map tray enum to CPLD I2C slave address
# Direct lookup — avoids tray label ambiguity
_TRAY_CPLD_ADDR: Dict[FanTray, str] = {
    FanTray.BOTTOM: '0x40',
    FanTray.UPPER:  '0x42',
}

# Map fan index to TACH register address
# Each fan pair shares one 2-byte register (inlet byte, outlet byte)
# Register layout per platform CPLD specification — replace with actual values:
#   Fan pair 0 (FAN0+FAN1) → 0xNN
#   Fan pair 1 (FAN2+FAN3) → 0xNN
#   Fan pair 2 (FAN4+FAN5) → 0xNN
#   Fan pair 3 (FAN6+FAN7) → 0xNN
_TACH_REG: Dict[int, str] = {
    0: '0xNN', 1: '0xNN',   # replace with actual TACH register addresses
    2: '0xNN', 3: '0xNN',
    4: '0xNN', 5: '0xNN',
    6: '0xNN', 7: '0xNN',
}

# Authentication prefix for OEM fan control commands
# Platform-specific bytes used as a command authentication token
# Prevents accidental execution by unrelated tools on the same network
# Replace with actual authentication bytes from your platform OEM spec
_OEM_AUTH_BYTES = ['0xNN', '0xNN', '0xNN']

# I2C bus number for fan board CPLD access
_FAN_I2C_BUS = '0x07'


# ---------------------------------------------------------------------------
# Fan RPM specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FanRpmSpec:
    """
    RPM acceptance band for one duty cycle test point.
    Values derived from fan module datasheet.

    frozen=True: immutable after creation — specification values
    should never be modified at runtime.
    """
    duty_percent: int
    inlet_min:    int
    inlet_max:    int
    outlet_min:   int
    outlet_max:   int

    def check_inlet(self, rpm: int) -> bool:
        """True if rpm is within the inlet acceptance band."""
        return self.inlet_min <= rpm <= self.inlet_max

    def check_outlet(self, rpm: int) -> bool:
        """True if rpm is within the outlet acceptance band."""
        return self.outlet_min <= rpm <= self.outlet_max


# Generic fan RPM specification — replace with actual datasheet values
# for your specific fan module model
GENERIC_FAN_SPEC: List[FanRpmSpec] = [
    FanRpmSpec(duty_percent=100, inlet_min=12150, inlet_max=14850,
               outlet_min=10980, outlet_max=13420),
    FanRpmSpec(duty_percent=50,  inlet_min=7335,  inlet_max=8965,
               outlet_min=6570,  outlet_max=8030),
    FanRpmSpec(duty_percent=10,  inlet_min=2200,  inlet_max=3200,
               outlet_min=1940,  outlet_max=2940),
]


# ---------------------------------------------------------------------------
# Fan measurement result
# ---------------------------------------------------------------------------

@dataclass
class FanReading:
    """
    Measurement results for one fan at one duty cycle point.

    Stores both CPLD-direct and SDR readings so they can be compared
    independently against datasheet bounds and against each other.
    """
    fan_index:    int
    duty_percent: int
    inlet_cpld:   int    # RPM from CPLD register (real-time)
    outlet_cpld:  int
    inlet_sdr:    int    # RPM from SDR (delayed by BMC polling interval)
    outlet_sdr:   int

    def cpld_sdr_delta_pct(self) -> Tuple[float, float]:
        """
        Percentage difference between CPLD-direct and SDR readings.

        Large deltas (>15%) indicate SDR polling lag — the BMC polling
        daemon has not yet updated the SDR to reflect the new fan speed.
        This is a warning, not a failure, because SDR lag is expected
        during rapid duty cycle changes.

        Returns:
            Tuple of (inlet_delta_pct, outlet_delta_pct)
        """
        def delta(cpld: int, sdr: int) -> float:
            return abs(cpld - sdr) / max(cpld, 1) * 100

        return delta(self.inlet_cpld, self.inlet_sdr), \
               delta(self.outlet_cpld, self.outlet_sdr)


# ---------------------------------------------------------------------------
# Fan controller
# ---------------------------------------------------------------------------

class FanControlError(Exception):
    """Raised when a fan control IPMI command fails."""
    pass


class FanController:
    """
    PWM fan control and TACH verification via IPMI OEM commands.

    One FanController instance is shared across all fan-related test methods.
    It holds no per-test state — all measurement results are returned as
    FanReading objects, never stored as instance variables.
    """

    def __init__(self, ipmi_client):
        """
        Args:
            ipmi_client: IpmiClient or MockBmcClient
        """
        self._ipmi = ipmi_client

    def get_tray_for_fan(self, fan_index: int) -> FanTray:
        """
        Map fan index to physical tray.
        Even indices (0,2,4,6) → BOTTOM tray.
        Odd  indices (1,3,5,7) → UPPER  tray.
        """
        return FanTray.BOTTOM if fan_index % 2 == 0 else FanTray.UPPER

    def get_presence(self, tray: FanTray) -> List[bool]:
        """
        Read fan presence bitmap for one tray from CPLD register 0x10.

        CPLD uses active-low presence encoding:
            bit value 0 → fan IS present (active-low asserted)
            bit value 1 → fan is NOT present

        Checks only odd bit positions (1,3,5,7) per CPLD register spec:
            bit 1 → fan pair 0
            bit 3 → fan pair 1
            bit 5 → fan pair 2
            bit 7 → fan pair 3

        Returns:
            List of 4 booleans — one per fan pair position in this tray
        """
        # Direct dict lookup — no string comparison that could swap trays
        cpld_addr = _TRAY_CPLD_ADDR[tray]

        response = self._ipmi.run(
            'raw', '0x06', '0x52',  # Master Write-Read
            _FAN_I2C_BUS,           # I2C bus 7
            cpld_addr,              # CPLD slave address
            '0x01',                 # read count: 1 byte back
            '0x10',                 # register address: presence detect
        )

        try:
            prsnt = int(response.strip(), 16)
        except ValueError as exc:
            raise FanControlError(
                f'Presence register read for {tray.value} tray '
                f'returned non-hex: {response!r}: {exc}'
            ) from exc

        result = [False] * 4
        for i in range(1, 8, 2):           # positions 1, 3, 5, 7
            if not ((prsnt >> i) & 0x01):  # active-low: 0 = present
                result[i // 2] = True
        return result

    def set_auto_mode(self, enabled: bool):
        """
        Enable or disable BMC automatic fan speed control.

        Auto-mode must be disabled before manual PWM sweeps so the
        BMC thermal management firmware does not override test commands.
        Auto-mode MUST be restored after — see FanSpeedVerifier.run_full_sweep
        for the try/finally pattern that guarantees restoration.

        Args:
            enabled: True  = BMC controls fan speed (normal operation)
                     False = test code controls fan speed (test mode)
        """
        mode_byte = '0x02' if enabled else '0x01'
        self._ipmi.run(
            'raw', '0x36', '0x67',
            *_OEM_AUTH_BYTES,
            '0x03',       # sub-command: set fan control mode
            mode_byte,    # 0x01=manual, 0x02=automatic
        )
        logger.info('Fan auto-control: %s',
                    'enabled' if enabled else 'disabled')

    def set_duty(self, fan_index: int, duty_percent: int):
        """
        Set PWM duty cycle for one fan.

        Args:
            fan_index:    0-7
            duty_percent: 0-100 (integer percentage)
        """
        if not 0 <= duty_percent <= 100:
            raise ValueError(
                f'duty_percent must be 0-100, got {duty_percent}'
            )

        self._ipmi.run(
            'raw', '0x36', '0x67',
            *_OEM_AUTH_BYTES,
            '0x04',                       # sub-command: set duty
            f'0x{fan_index:02x}',         # fan index as hex byte
            hex(duty_percent),            # duty percentage as hex
        )
        logger.debug('FAN%d duty → %d%%', fan_index, duty_percent)

    def read_tach_cpld(self, fan_index: int) -> Tuple[int, int]:
        """
        Read TACH directly from CPLD register via I2C Master Write-Read.

        The CPLD counts TACH pulses in hardware — no software polling.
        Register update latency: one hardware measurement window (~1s).
        This is the ground-truth path for RPM verification.

        Two bytes are returned per register read:
            byte 0 = INLET  rotor TACH count
            byte 1 = OUTLET rotor TACH count

        Both fans in a pair share one register because they are physically
        in the same slot — the CPLD exposes them together.

        Conversion: raw_count × 60 = RPM
        (CPLD counts pulses-per-second; ×60 converts to per-minute)

        Returns:
            Tuple of (inlet_rpm, outlet_rpm)
        """
        tray      = self.get_tray_for_fan(fan_index)
        cpld_addr = _TRAY_CPLD_ADDR[tray]
        tach_reg  = _TACH_REG[fan_index]

        response = self._ipmi.run(
            'raw', '0x06', '0x52',
            _FAN_I2C_BUS,
            cpld_addr,
            '0x02',     # read count: 2 bytes (inlet + outlet)
            tach_reg,   # register address to send first (write phase)
        )

        tokens = response.split()
        if len(tokens) < 2:
            raise FanControlError(
                f'TACH read for FAN{fan_index} returned insufficient bytes: '
                f'{response!r}. Expected 2 bytes (inlet, outlet).'
            )

        inlet_rpm  = int(tokens[0], 16) * 60
        outlet_rpm = int(tokens[1], 16) * 60
        logger.debug('FAN%d CPLD TACH: inlet=%d outlet=%d RPM',
                     fan_index, inlet_rpm, outlet_rpm)
        return inlet_rpm, outlet_rpm

    def read_tach_sdr(self, fan_index: int,
                      sdr_output: str) -> Tuple[int, int]:
        """
        Extract fan TACH RPM from ipmitool sdr elist output.

        SDR sensor names follow the pattern:
            Fan_SYS{fan_index}_0 = INLET  rotor
            Fan_SYS{fan_index}_1 = OUTLET rotor

        The BMC firmware applies the ×60 conversion internally before
        publishing to SDR, so values are already in RPM.

        SDR update latency: one BMC polling cycle (5-30 seconds, variable).
        This path verifies the BMC's sensor publishing pipeline, not just
        the raw CPLD hardware — which is why both paths are checked.

        Args:
            fan_index:  0-7
            sdr_output: stdout from 'ipmitool sdr elist all'

        Returns:
            Tuple of (inlet_rpm, outlet_rpm) — 0 if sensor not found
        """
        # build SDR lookup dict (same pattern as sdr_parser.py)
        sdrs: Dict[str, List[str]] = {}
        for line in sdr_output.splitlines():
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 5:
                reading = ' | '.join(parts[4:])
                sdrs[parts[0]] = parts[:4] + [reading]

        inlet_name  = f'Fan_SYS{fan_index}_0'
        outlet_name = f'Fan_SYS{fan_index}_1'
        inlet_rpm = outlet_rpm = 0

        if inlet_name in sdrs:
            try:
                inlet_rpm = int(sdrs[inlet_name][4].split()[0])
            except (ValueError, IndexError) as exc:
                logger.warning('Cannot parse %s RPM: %s', inlet_name, exc)

        if outlet_name in sdrs:
            try:
                outlet_rpm = int(sdrs[outlet_name][4].split()[0])
            except (ValueError, IndexError) as exc:
                logger.warning('Cannot parse %s RPM: %s', outlet_name, exc)

        logger.debug('FAN%d SDR TACH: inlet=%d outlet=%d RPM',
                     fan_index, inlet_rpm, outlet_rpm)
        return inlet_rpm, outlet_rpm

    def measure_fan_at_duty(self,
                            fan_index:    int,
                            spec:         FanRpmSpec,
                            sdr_fetch_fn) -> FanReading:
        """
        Set fan to duty cycle, wait for stabilization, measure both paths.

        Two settle times are used deliberately:
            5s after PWM command: physical RPM stabilization (mechanical inertia)
            2s before SDR read:  BMC polling cycle latency

        Args:
            fan_index:    0-7
            spec:         FanRpmSpec with duty_percent and acceptance bounds
            sdr_fetch_fn: callable() → str — returns fresh SDR output
                          dependency injection: avoids coupling this class
                          to the global g_cmd_output pattern

        Returns:
            FanReading with CPLD and SDR measurements
        """
        self.set_duty(fan_index, spec.duty_percent)
        time.sleep(5)   # mechanical inertia settle time

        inlet_cpld, outlet_cpld = self.read_tach_cpld(fan_index)

        time.sleep(2)   # BMC SDR polling lag settle time
        sdr_output = sdr_fetch_fn()
        inlet_sdr, outlet_sdr = self.read_tach_sdr(fan_index, sdr_output)

        return FanReading(
            fan_index    = fan_index,
            duty_percent = spec.duty_percent,
            inlet_cpld   = inlet_cpld,
            outlet_cpld  = outlet_cpld,
            inlet_sdr    = inlet_sdr,
            outlet_sdr   = outlet_sdr,
        )


# ---------------------------------------------------------------------------
# Fan speed verifier — orchestrates the full sweep
# ---------------------------------------------------------------------------

class FanSpeedVerifier:
    """
    Orchestrates the complete fan speed sweep test.

    Runs each present fan through all duty cycle points in spec_table,
    measuring TACH via both CPLD direct and SDR paths at each point.

    Critical safety guarantee:
        Auto-fan control is ALWAYS restored after the sweep via try/finally,
        even if an exception occurs mid-test — without try/finally
        a mid-sweep exception would leave fans stuck at the last
        duty setting (potentially 10%), risking thermal damage.
    """

    def __init__(self,
                 controller:  FanController,
                 spec_table:  List[FanRpmSpec] = None):
        """
        Args:
            controller:  FanController instance
            spec_table:  list of FanRpmSpec test points
                         defaults to GENERIC_FAN_SPEC if None
        """
        self._fan  = controller
        self._spec = spec_table if spec_table is not None \
                     else GENERIC_FAN_SPEC

    def run_full_sweep(self,
                       check:        'SdrCheckResult',
                       sdr_fetch_fn) -> None:
        """
        Execute the complete 8-fan × N-duty-point sweep.

        Args:
            check:        SdrCheckResult accumulator for pass/fail records
            sdr_fetch_fn: callable() → str returning fresh SDR elist output

        Auto-mode is guaranteed to be restored by try/finally even on
        exception — this is the most important safety property of this
        method, guaranteed even on exception.
        """
        from src.protocol.sdr_parser import SdrCheckResult  # avoid circular

        # read presence for both trays before disabling auto-mode
        presence: Dict[FanTray, List[bool]] = {
            FanTray.BOTTOM: self._fan.get_presence(FanTray.BOTTOM),
            FanTray.UPPER:  self._fan.get_presence(FanTray.UPPER),
        }
        logger.info('Fan presence — BOTTOM: %s  UPPER: %s',
                    presence[FanTray.BOTTOM], presence[FanTray.UPPER])

        # disable auto-control BEFORE try block
        # if this call fails we want the exception to propagate
        # without entering try (so finally does not run unnecessarily)
        self._fan.set_auto_mode(enabled=False)

        try:
            # ── pre-step: spin all present fans to 100% and settle ──
            for fan_idx in range(8):
                if self._fan_is_present(fan_idx, presence):
                    self._fan.set_duty(fan_idx, 100)
            time.sleep(5)

            # ── main sweep: each present fan × each duty point ──
            for fan_idx in range(8):
                if not self._fan_is_present(fan_idx, presence):
                    logger.debug('FAN%d not present — skipping', fan_idx)
                    continue

                for spec in self._spec:
                    reading = self._fan.measure_fan_at_duty(
                        fan_idx, spec, sdr_fetch_fn
                    )
                    self._verify_reading(reading, spec, check)

        finally:
            # ── GUARANTEED: restore auto-mode regardless of outcome ──
            # This runs whether the sweep completed normally, raised an
            # exception, or was interrupted. Fans will always return to
            # BMC thermal management control after this method returns.
            self._fan.set_auto_mode(enabled=True)
            logger.info(
                'Auto-fan control restored '
                '(guaranteed by finally block)'
            )

    def _fan_is_present(self,
                        fan_idx:  int,
                        presence: Dict[FanTray, List[bool]]) -> bool:
        """Check presence dict for a given fan index."""
        tray     = self._fan.get_tray_for_fan(fan_idx)
        pair_idx = fan_idx // 2
        tray_presence = presence.get(tray, [])
        if pair_idx < len(tray_presence):
            return tray_presence[pair_idx]
        return False

    def _verify_reading(self,
                        reading: FanReading,
                        spec:    FanRpmSpec,
                        check) -> None:
        """
        Verify one FanReading against spec bounds for both paths.
        Also checks CPLD/SDR consistency via cpld_sdr_delta_pct().
        """
        fan  = reading.fan_index
        duty = reading.duty_percent

        # CPLD direct path
        if spec.check_inlet(reading.inlet_cpld):
            check.ok(
                f'FAN{fan} INLET {duty}% CPLD: {reading.inlet_cpld} RPM '
                f'(spec {spec.inlet_min}-{spec.inlet_max})'
            )
        else:
            check.fail(
                f'FAN{fan} INLET {duty}% CPLD: {reading.inlet_cpld} RPM '
                f'out of spec ({spec.inlet_min}-{spec.inlet_max})'
            )

        if spec.check_outlet(reading.outlet_cpld):
            check.ok(
                f'FAN{fan} OUTLET {duty}% CPLD: {reading.outlet_cpld} RPM '
                f'(spec {spec.outlet_min}-{spec.outlet_max})'
            )
        else:
            check.fail(
                f'FAN{fan} OUTLET {duty}% CPLD: {reading.outlet_cpld} RPM '
                f'out of spec ({spec.outlet_min}-{spec.outlet_max})'
            )

        # SDR path — same bounds, independent measurement
        if spec.check_inlet(reading.inlet_sdr):
            check.ok(
                f'FAN{fan} INLET {duty}% SDR: {reading.inlet_sdr} RPM'
            )
        else:
            check.fail(
                f'FAN{fan} INLET {duty}% SDR: {reading.inlet_sdr} RPM '
                f'out of spec ({spec.inlet_min}-{spec.inlet_max})'
            )

        if spec.check_outlet(reading.outlet_sdr):
            check.ok(
                f'FAN{fan} OUTLET {duty}% SDR: {reading.outlet_sdr} RPM'
            )
        else:
            check.fail(
                f'FAN{fan} OUTLET {duty}% SDR: {reading.outlet_sdr} RPM '
                f'out of spec ({spec.outlet_min}-{spec.outlet_max})'
            )

        # cross-path consistency
        inlet_delta, outlet_delta = reading.cpld_sdr_delta_pct()
        if inlet_delta > 15.0:
            check.warn(
                f'FAN{fan} INLET {duty}%: CPLD/SDR delta {inlet_delta:.1f}% '
                f'(CPLD={reading.inlet_cpld}, SDR={reading.inlet_sdr}) '
                f'— possible SDR polling lag'
            )
        if outlet_delta > 15.0:
            check.warn(
                f'FAN{fan} OUTLET {duty}%: CPLD/SDR delta {outlet_delta:.1f}% '
                f'(CPLD={reading.outlet_cpld}, SDR={reading.outlet_sdr}) '
                f'— possible SDR polling lag'
            )
