"""
SDR parser and bidirectional verification.

ipmitool sdr elist produces pipe-delimited lines:
    name | id | status | entity | reading

The reading field may itself contain pipes (e.g. "Presence detected | AC OK"),
so parsing uses ' | '.join(parts[4:]) rather than a simple split.

PresenceState enum handles the four combinations of configured/detected
presence, including phantom sensors (sensor reading on an empty slot) and
missing sensors (no reading on a populated slot).

SdrCheckResult accumulates named failures so you know which specific
sensor failed rather than just getting a pass/fail count.
"""

import re
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SDR entry dataclass
# ---------------------------------------------------------------------------

@dataclass
class SdrEntry:
    """
    One parsed line from ipmitool sdr elist output.

    Fields map directly to ipmitool's pipe-delimited columns:
        name      — sensor name from SDR record name string field
        sensor_id — sensor number in hex e.g. 'A7h'
        status    — 'ok', 'ns' (not scanning), 'cr' (critical), 'na'
        entity    — entity ID.instance e.g. '3.1' = Processor 1
        reading   — engineering value or event string
                    e.g. '52 degrees C', 'Presence detected', 'No Reading'
    """
    name:      str
    sensor_id: str
    status:    str
    entity:    str
    reading:   str

    @classmethod
    def from_line(cls, line: str) -> Optional['SdrEntry']:
        """
        Parse one pipe-delimited SDR line.

        Handles the case where the reading field itself contains pipe
        characters (introduced by some BMC firmware versions):
            'PSU0_Status | C0h | ok | 8.1 | Presence detected | AC OK'
        becomes reading = 'Presence detected | AC OK'

        Returns None for malformed lines (fewer than 5 fields).
        """
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 5:
            if line.strip():
                logger.debug('SDR: skipping malformed line: %r', line)
            return None

        return cls(
            name      = parts[0],
            sensor_id = parts[1],
            status    = parts[2],
            entity    = parts[3],
            reading   = ' | '.join(parts[4:]),
            # joining from index 4 onward handles any number of pipe
            # characters in the reading field without information loss
        )

    @classmethod
    def parse_all(cls, raw_output: str) -> Dict[str, 'SdrEntry']:
        """
        Parse complete ipmitool sdr elist output into a lookup dictionary.

        Args:
            raw_output: multi-line string from ipmitool sdr elist all

        Returns:
            dict mapping sensor name → SdrEntry
            O(1) lookup by name for all subsequent verification checks
        """
        result: Dict[str, SdrEntry] = {}
        for line in raw_output.splitlines():
            entry = cls.from_line(line)
            if entry is not None:
                result[entry.name] = entry
        return result


# ---------------------------------------------------------------------------
# Hardware presence state — replaces binary 0/1 integer flags
# ---------------------------------------------------------------------------

class PresenceState(Enum):
    """
    Four possible outcomes from comparing sys_conf with live SDR data.

    CONFIRMED_PRESENT:  sys_conf=1 and BMC reports detected
                        → expected, downstream readings should be valid
    CONFIRMED_ABSENT:   sys_conf=0 and BMC reports not detected
                        → expected, downstream readings should be No Reading
    UNEXPECTED_PRESENT: sys_conf=0 but BMC reports detected
                        → phantom sensor — BMC firmware bug
    UNEXPECTED_ABSENT:  sys_conf=1 but BMC reports not detected
                        → missing sensor — hardware or detection failure
    """
    CONFIRMED_PRESENT  = auto()
    CONFIRMED_ABSENT   = auto()
    UNEXPECTED_PRESENT = auto()
    UNEXPECTED_ABSENT  = auto()


# ---------------------------------------------------------------------------
# Check result accumulator
# ---------------------------------------------------------------------------

@dataclass
class SdrCheckResult:
    """
    Accumulates named pass/fail/warn outcomes for one verify_* call.

    Uses
    structured records that produce actionable diagnostic messages.
    """
    passed:   int       = 0
    failures: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def fail(self, message: str):
        """Record one failure with a descriptive message."""
        self.failures.append(message)
        logger.error('  **FAIL** %s', message)

    def warn(self, message: str):
        """Record a warning — does not increment failure count."""
        self.warnings.append(message)
        logger.warning('  **WARN** %s', message)

    def ok(self, message: str):
        """Record one pass."""
        self.passed += 1
        logger.debug('  ==PASS== %s', message)

    @property
    def result_code(self) -> int:
        """
        Return 0 if all checks passed, negative integer if any failed.
        Negative value equals -(number of failures) for diagnostic counting.
        """
        return -len(self.failures)

    def summary(self) -> str:
        """Human-readable summary line plus all failure messages."""
        lines = [
            f'{self.passed} passed, {len(self.failures)} failed, '
            f'{len(self.warnings)} warnings'
        ]
        for f in self.failures:
            lines.append(f'  FAIL: {f}')
        for w in self.warnings:
            lines.append(f'  WARN: {w}')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Regex patterns for sensor name parsing
# ---------------------------------------------------------------------------

# Matches 'CPU0', 'CPU1', 'CPU10' — presence sensors
_CPU_PRESENCE_RE = re.compile(r'^CPU(?P<idx>\d+)$')

# Matches 'Temp_CPU0', 'Temp_CPU0_VR', 'Temp_CPU1_ABCD', etc.
_CPU_TEMP_RE = re.compile(r'^Temp_CPU(?P<idx>\d+)')

# Matches 'Vol_PVCCIN_CPU0', 'Vol_PVCCFA_CPU1', etc.
_CPU_VOLT_RE = re.compile(r'^Vol_\w+_CPU(?P<idx>\d+)$')

# Matches 'T_CPU_Highest' or 'Power_CPU'
_CPU_AGG_RE  = re.compile(r'^(T_CPU_Highest|Power_CPU)$')

# Known presence sensor IDs for CPU slots (platform-specific assignment)
_CPU_PRESENCE_SENSOR_IDS = {'A7h', 'A8h'}


# ---------------------------------------------------------------------------
# CPU SDR verification
# ---------------------------------------------------------------------------

def verify_cpu_sdr(raw_output: str,
                   power_state: int,
                   sys_conf: dict,
                   cpu_sdrs: frozenset) -> SdrCheckResult:
    """
    Bidirectional CPU sensor verification.

    Pass 1: compare sys_conf CPU presence flags against live BMC detection.
            Builds cpu_states dict mapping CPU index → PresenceState.
    Pass 2: verify each reading sensor against the presence state from Pass 1.
            Temperature sensors, VR voltage sensors, aggregate sensors.

    Args:
        raw_output:   ipmitool sdr elist all output string
        power_state:  0=DC off, 1=DC on, 2=BIOS POST complete
                      CPU sensors only valid at power_state=2
        sys_conf:     hardware presence bitmap from sys_conf.json
        cpu_sdrs:     frozenset of known CPU sensor names to verify

    Returns:
        SdrCheckResult with per-sensor pass/fail records
    """
    check = SdrCheckResult()

    # precondition: CPU sensors only initialize after BIOS POST
    if power_state != 2:
        check.fail(
            f'BIOS POST not complete (power_state={power_state}). '
            f'CPU SDR sensors require POST completion before they '
            f'initialize. Ensure system reaches state 2 first.'
        )
        return check

    sdrs = SdrEntry.parse_all(raw_output)

    # ── Pass 1: presence state detection ──────────────────────────────
    cpu_states: Dict[int, PresenceState] = {}

    for name, entry in sdrs.items():
        m = _CPU_PRESENCE_RE.match(name)
        if not m:
            continue
        if entry.sensor_id not in _CPU_PRESENCE_SENSOR_IDS:
            continue

        cpu_idx = int(m.group('idx'))
        config_key = f'CPU{cpu_idx}'
        configured = sys_conf.get(config_key)

        if configured is None:
            check.warn(
                f'Sensor {name} references {config_key} which is not '
                f'in sys_conf.json. Add it to the hardware bitmap.'
            )
            continue

        bmc_detected = 'Presence detected' in entry.reading

        if configured and bmc_detected:
            cpu_states[cpu_idx] = PresenceState.CONFIRMED_PRESENT
            check.ok(f'CPU{cpu_idx} confirmed present')

        elif configured and not bmc_detected:
            cpu_states[cpu_idx] = PresenceState.UNEXPECTED_ABSENT
            check.fail(
                f'CPU{cpu_idx}: sys_conf=1 but BMC reports not detected. '
                f'Check physical installation and IPMB bus integrity. '
                f'Reading: [{entry.reading}]'
            )

        elif not configured and bmc_detected:
            cpu_states[cpu_idx] = PresenceState.UNEXPECTED_PRESENT
            check.fail(
                f'CPU{cpu_idx}: sys_conf=0 but BMC reports detected — '
                f'phantom sensor. Check BMC SDR repository for stale '
                f'entries. Reading: [{entry.reading}]'
            )

        else:
            cpu_states[cpu_idx] = PresenceState.CONFIRMED_ABSENT
            check.ok(f'CPU{cpu_idx} correctly absent (sys_conf=0, '
                     f'BMC=not detected)')

    # ── Pass 2: reading sensor verification ───────────────────────────
    for name, entry in sdrs.items():
        if name not in cpu_sdrs:
            continue

        reading = entry.reading

        # temperature sensors
        m = _CPU_TEMP_RE.match(name)
        if m:
            cpu_idx = int(m.group('idx'))
            state   = cpu_states.get(cpu_idx)

            if state is None:
                check.warn(
                    f'{name}: no presence state for CPU{cpu_idx} — '
                    f'presence sensor missing from SDR output'
                )
                continue

            if state == PresenceState.CONFIRMED_PRESENT:
                if 'No Reading' in reading:
                    check.fail(
                        f'{name}: CPU{cpu_idx} present but thermal sensor '
                        f'uninitialized. Possible BMC firmware issue.'
                    )
                else:
                    check.ok(f'{name}: {reading}')

            elif state == PresenceState.CONFIRMED_ABSENT:
                if 'No Reading' not in reading:
                    check.fail(
                        f'{name}: CPU{cpu_idx} absent but reading '
                        f'[{reading}] — stale register value.'
                    )
                else:
                    check.ok(f'{name}: correctly No Reading (CPU absent)')

            elif state == PresenceState.UNEXPECTED_ABSENT:
                check.warn(
                    f'{name}: CPU{cpu_idx} presence failed — '
                    f'temperature [{reading}] is unreliable'
                )

            elif state == PresenceState.UNEXPECTED_PRESENT:
                check.fail(
                    f'{name}: CPU{cpu_idx} is phantom — '
                    f'temperature [{reading}] from ghost sensor'
                )
            continue

        # voltage sensors
        m = _CPU_VOLT_RE.match(name)
        if m:
            cpu_idx = int(m.group('idx'))
            state   = cpu_states.get(cpu_idx)

            if state == PresenceState.CONFIRMED_PRESENT:
                if 'Disabled' in reading:
                    check.fail(
                        f'{name}: CPU{cpu_idx} present but VR disabled. '
                        f'Power delivery fault — check VR hardware.'
                    )
                else:
                    check.ok(f'{name}: {reading}')

            elif state == PresenceState.CONFIRMED_ABSENT:
                if 'Disabled' not in reading:
                    check.fail(
                        f'{name}: CPU{cpu_idx} absent but VR reports '
                        f'[{reading}] — phantom power reading.'
                    )
                else:
                    check.ok(f'{name}: correctly Disabled (CPU absent)')
            continue

        # aggregate sensors (T_CPU_Highest, Power_CPU)
        if _CPU_AGG_RE.match(name):
            any_present = any(
                s == PresenceState.CONFIRMED_PRESENT
                for s in cpu_states.values()
            )

            if any_present and 'No Reading' in reading:
                check.fail(
                    f'{name}: CPU(s) present but aggregate uninitialized. '
                    f'BMC aggregation logic may be broken.'
                )
            elif not any_present and 'No Reading' not in reading:
                check.fail(
                    f'{name}: no CPUs present but aggregate reports '
                    f'[{reading}] — phantom aggregate value.'
                )
            else:
                check.ok(f'{name}: {reading}')

    logger.info('CPU SDR check: %s', check.summary())
    return check


# ---------------------------------------------------------------------------
# PSU SDR verification
# ---------------------------------------------------------------------------

_PSU_STATUS_RE  = re.compile(r'^PSU(\d+)_Status$')
_PSU_FAN_RE     = re.compile(r'^Fan_PSU(\d+)_\d+$')
_PSU_CURRENT_RE = re.compile(r'^PSU(\d+)_Current$')
_PSU_INPUT_RE   = re.compile(r'^PSU(\d+)_Input$')
_PSU_TEMP_RE    = re.compile(r'^Temp_PSU(\d+)_')


def verify_psu_sdr(raw_output: str,
                   power_state: int,
                   sys_conf: dict,
                   psu_sdrs: frozenset) -> SdrCheckResult:
    """
    PSU sensor bidirectional verification.

    Pass 1: status sensors determine which PSUs are present and whether
            AC power is supplied.
    Pass 2: reading sensors (fan RPM, current, input power, temperature)
            verified against presence state.

    PSU presence states:
        0 = not present
        1 = present, AC supplied
        2 = present, AC lost (0 RPM and 0W are expected in this state)
    """
    check = SdrCheckResult()
    sdrs  = SdrEntry.parse_all(raw_output)

    psu_presents = [0] * 6  # 6 PSU slots

    # ── Pass 1: PSU status sensors ────────────────────────────────────
    for name, entry in sdrs.items():
        m = _PSU_STATUS_RE.match(name)
        if not m:
            continue

        psu_idx    = int(m.group(1))
        config_key = f'PSU{psu_idx}'
        configured = sys_conf.get(config_key)

        if configured is None:
            check.warn(f'{name}: {config_key} not in sys_conf.json')
            continue

        reading      = entry.reading
        bmc_detected = 'Presence detected' in reading

        if configured and bmc_detected:
            psu_presents[psu_idx] = 1
            if 'Power Supply AC lost' in reading:
                psu_presents[psu_idx] = 2
                check.ok(f'PSU{psu_idx} present, AC lost '
                         f'(0 RPM/Watts are expected)')
            else:
                check.ok(f'PSU{psu_idx} present, AC supplied')

        elif configured and not bmc_detected:
            check.fail(
                f'PSU{psu_idx}: sys_conf=1 but not detected. '
                f'Check physical installation.'
            )

        elif not configured and bmc_detected:
            check.fail(
                f'PSU{psu_idx}: sys_conf=0 but BMC detects it — '
                f'phantom sensor. Reading: [{reading}]'
            )

        else:
            check.ok(f'PSU{psu_idx} correctly absent')

    # ── Pass 2: reading sensors ────────────────────────────────────────
    for name, entry in sdrs.items():
        if name not in psu_sdrs:
            continue

        reading = entry.reading

        # fan RPM sensors
        m = _PSU_FAN_RE.match(name)
        if m:
            psu_idx = int(m.group(1))
            _check_psu_reading(
                check, name, reading, psu_idx, psu_presents,
                zero_string='0 RPM',
                zero_ok_state=2,
                zero_msg='0 RPM on AC-supplied PSU'
            )
            continue

        # current sensors
        m = _PSU_CURRENT_RE.match(name)
        if m:
            psu_idx = int(m.group(1))
            _check_psu_reading(
                check, name, reading, psu_idx, psu_presents,
                zero_string='0 Amps',
                zero_ok_state=2,
                zero_msg='0 Amps on AC-supplied PSU'
            )
            continue

        # input power sensors
        m = _PSU_INPUT_RE.match(name)
        if m:
            psu_idx = int(m.group(1))
            _check_psu_reading(
                check, name, reading, psu_idx, psu_presents,
                zero_string='0 Watts',
                zero_ok_state=2,
                zero_msg='0 Watts on AC-supplied PSU'
            )
            continue

    logger.info('PSU SDR check: %s', check.summary())
    return check


def _check_psu_reading(check:         SdrCheckResult,
                       name:          str,
                       reading:       str,
                       psu_idx:       int,
                       psu_presents:  list,
                       zero_string:   str,
                       zero_ok_state: int,
                       zero_msg:      str):
    """Helper: verify one PSU reading sensor against presence state."""
    state = psu_presents[psu_idx] if psu_idx < len(psu_presents) else 0

    if 'No Reading' in reading:
        if state == 0:
            check.ok(f'{name}: correctly No Reading (PSU absent)')
        else:
            check.fail(f'{name}: PSU present but No Reading')

    elif zero_string in reading:
        val = reading.split()[0]
        if val == '0':
            if state == zero_ok_state:
                check.ok(f'{name}: 0 reading expected (AC lost)')
            elif state == 0:
                check.ok(f'{name}: 0 reading expected (PSU absent)')
            else:
                check.fail(f'{name}: {zero_msg}')
        else:
            if state >= 1:
                check.ok(f'{name}: {reading}')
            else:
                check.fail(f'{name}: PSU absent but has reading [{reading}]')

    else:
        if state >= 1:
            check.ok(f'{name}: {reading}')
        else:
            check.fail(f'{name}: PSU absent but has reading [{reading}]')
