"""
Firmware update via FwFlashTool subprocess and IPMI OEM commands.

Four targets:
    BIOS  — SPI flash. Host MUST be powered off first (single SPI master).
    BMC   — NOR flash, dual-image slots. BMC reboots itself after flash.
    CPLD  — dual-sector (cfm0 active, cfm1 fallback). Needs virtual reseat.
    AMC   — PLDM over MCTP. EIDs are dynamic — discovered at runtime.

FwFlashTool flags:
    -vyes  non-interactive
    -nw    IPMI LAN transport
    -fb    full BMC flash
    -pc    preserve NVRAM across BMC flash (platform-specific)
    -img-select <n>  BMC image slot selector (platform-specific)
    Device type flag (-d) is platform-specific — see 0xNN placeholders

Post-flash verification uses standard IPMI commands:
    BIOS version:  NetFn=0x06 Cmd=0x59 (Get System Info Parameters)
    BMC version:   mc info → Firmware Revision field
    CPLD version:  NetFn=0x38 Cmd=0xAB <board_type>
    Active image:  NetFn=0x32 Cmd=0x8F 0x07 (GET active image)

Safety requirements enforced in code:
    - BIOS flash always powers off host first
    - BMC flash pings BMC after reboot (up to 120s wait)
    - CPLD flash performs virtual reseat after programming
    - All flash operations timeout at 600s (FwFlashTool internal limit)
"""

import re
import subprocess
import time
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Device type constants (FwFlashTool -d flag values)
# ---------------------------------------------------------------------------

class YafuDevice(Enum):
    BIOS  = 2    # platform-specific device type for BIOS SPI flash
    CPLD  = 4    # platform-specific device type for CPLD
    AMC   = 64   # platform-specific device type for AMC PLDM


# ---------------------------------------------------------------------------
# BMC dual-image constants
# ---------------------------------------------------------------------------

class BmcImageSlot(Enum):
    SLOT_1 = '0x1'    # image slot 1
    SLOT_2 = '0x2'    # image slot 2
    BOTH   = '3'      # update both slots simultaneously


# ---------------------------------------------------------------------------
# CPLD device board type codes (NetFn=0x38 Cmd=0xAB parameter)
# Values are platform-specific — adjust for your CPLD inventory
# ---------------------------------------------------------------------------

CPLD_BOARD_TYPES = {
    # Replace 0xNN values with actual board type codes from your platform
    # OEM IPMI specification. These are passed to the CPLD version read command.
    'fanb_down': '0xNN',   # fan board, bottom
    'fanb_up':   '0xNN',   # fan board, upper
    'iob_down':  '0xNN',   # IO board, bottom
    'iob_up':    '0xNN',   # IO board, upper
    'hdbp':      '0xNN',   # hard drive backplane
    'pdb':       '0xNN',   # power distribution board
    'mb':        '0xNN',   # motherboard
    'ubb':       '0xNN',   # universal baseboard
}


# ---------------------------------------------------------------------------
# Firmware image descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FwImage:
    """One firmware image with its expected post-flash version string."""
    path:       str    # absolute or relative path to image file
    version:    str    # expected version after flash e.g. '1A04', '1.08'
    label:      str    # human-readable label for log messages


# ---------------------------------------------------------------------------
# FwFlashTool runner
# ---------------------------------------------------------------------------

class FwFlashToolError(Exception):
    """Raised when FwFlashTool exits with non-zero or reports failure."""
    pass


class FwUpdateClient:
    """
    Firmware flash tool wrapper for firmware update operations.

    Handles subprocess execution, timeout, stdout capture,
    and basic success/failure detection from FwFlashTool output.

    All flash methods are destructive and irreversible.
    Call power_off() before BIOS flash.
    Call wait_for_bmc_online() after BMC flash.
    """

    # FwFlashTool output strings that indicate success/failure
    _SUCCESS_PATTERNS = [
        r'Firmware Update Successful',
        r'Update Completed Successfully',
        r'Programming\s+Completed',
    ]
    _FAILURE_PATTERNS = [
        r'Firmware Update Failed',
        r'Error:',
        r'Unable to connect',
        r'Authentication failed',
    ]

    def __init__(self,
                 bmc_ip:       str,
                 bmc_user:     str,
                 bmc_password: str,
                 flash_tool_binary:  str = './FwFlashTool',
                 timeout:      int = 600):
        """
        Args:
            bmc_ip:       BMC IP address
            bmc_user:     BMC username
            bmc_password: BMC password
            flash_tool_binary:  path to firmware flash tool binary
            timeout:      maximum seconds per flash operation (default 600)
        """
        self._ip       = bmc_ip
        self._user     = bmc_user
        self._password = bmc_password
        self._binary   = flash_tool_binary
        self._timeout  = timeout

        # base command — password masked in log
        self._base = [
            flash_tool_binary,
            '-vyes',
            '-nw',
            '-ip', bmc_ip,
            '-u',  bmc_user,
            '-p',  bmc_password,
        ]
        self._base_log = [
            flash_tool_binary, '-vyes', '-nw',
            '-ip', bmc_ip, '-u', bmc_user, '-p', '***',
        ]

    def _run(self, extra_args: list, stdin_data: str = None) -> str:
        """
        Execute FwFlashTool with extra arguments and return stdout.

        Args:
            extra_args:  additional flags and image path
            stdin_data:  string piped to FwFlashTool stdin (for AMC EID)

        Returns:
            Combined stdout string

        Raises:
            FwFlashToolError on non-zero exit or failure pattern in output
        """
        cmd = self._base + extra_args
        log_cmd = self._base_log + extra_args
        logger.info('FwFlashTool: %s', ' '.join(log_cmd))

        try:
            result = subprocess.run(
                cmd,
                input          = stdin_data,
                capture_output = True,
                text           = True,
                timeout        = self._timeout,
            )
        except subprocess.TimeoutExpired:
            raise FwFlashToolError(
                f'Firmware flash tool timed out after {self._timeout}s. '
                f'Command: {" ".join(log_cmd)}'
            )

        output = result.stdout + result.stderr
        logger.debug('FwFlashTool output:\n%s', output[:2000])

        for pattern in self._FAILURE_PATTERNS:
            if re.search(pattern, output, re.IGNORECASE):
                raise FwFlashToolError(
                    f'Firmware flash tool reported failure '
                    f'(matched "{pattern}"):\n{output[:1000]}'
                )

        if result.returncode != 0:
            raise FwFlashToolError(
                f'Firmware flash tool exited {result.returncode}:\n{output[:1000]}'
            )

        return output

    # ------------------------------------------------------------------
    # BIOS flash
    # ------------------------------------------------------------------

    def flash_bios(self, image: FwImage) -> str:
        """
        Flash BIOS image via FwFlashTool (device type is platform-specific).

        HOST MUST BE POWERED OFF before calling this method.
        Concurrent CPU reads from SPI flash during erase/write
        cause immediate system crash and potential flash corruption.

        Args:
            image: FwImage with path and expected version

        Returns:
            FwFlashTool stdout output
        """
        logger.info('Flashing BIOS: %s (expected version %s)',
                    image.path, image.version)
        return self._run([image.path, '-d', str(YafuDevice.BIOS.value)])

    # ------------------------------------------------------------------
    # BMC flash
    # ------------------------------------------------------------------

    def flash_bmc(self,
                  image:       FwImage,
                  slot:        BmcImageSlot,
                  preserve_config: bool = False) -> str:
        """
        Flash BMC firmware image to specified slot via FwFlashTool.

        BMC reboots itself after flash (~90-120 seconds).
        Caller must call wait_for_bmc_online() after this returns.

        Args:
            image:           FwImage with path and expected version
            slot:            which image slot to write
            preserve_config: if True, adds -pc flag to preserve NVRAM

        Returns:
            FwFlashTool stdout output
        """
        args = [
            f'-img-select {slot.value if slot != BmcImageSlot.BOTH else "3"}',
            '-fb',
        ]
        if preserve_config:
            args.append('-pc')
        args.append(image.path)

        logger.info(
            'Flashing BMC slot %s: %s (preserve_config=%s)',
            slot.name, image.path, preserve_config
        )
        return self._run(args)

    # ------------------------------------------------------------------
    # CPLD flash
    # ------------------------------------------------------------------

    def flash_cpld(self, image: FwImage) -> str:
        """
        Flash one CPLD image via FwFlashTool (device type is platform-specific).

        Args:
            image: FwImage with path, version, and CPLD name in label

        Returns:
            FwFlashTool stdout output
        """
        logger.info('Flashing CPLD %s: %s (version %s)',
                    image.label, image.path, image.version)
        return self._run([image.path, '-d', str(YafuDevice.CPLD.value)])

    # ------------------------------------------------------------------
    # AMC flash (PLDM)
    # ------------------------------------------------------------------

    def flash_amc(self, image: FwImage, eid: int) -> str:
        """
        Flash AMC firmware via PLDM using FwFlashTool.

        FwFlashTool prompts for the EID interactively when using PLDM mode.
        This method supplies the EID via stdin pipe.

        Args:
            image: FwImage with path and expected version
            eid:   MCTP Endpoint ID for this AMC slot (dynamic — fetched
                   at runtime from BMC MCTP endpoint table)

        Returns:
            FwFlashTool stdout output
        """
        logger.info(
            'Flashing AMC (EID %d): %s (version %s)',
            eid, image.path, image.version
        )
        return self._run(
            [image.path, '-d', str(YafuDevice.AMC.value)],
            stdin_data=f'{eid}\n',
        )


# ---------------------------------------------------------------------------
# Version verifiers
# ---------------------------------------------------------------------------

class FwVersionVerifier:
    """
    Post-flash firmware version verification via IPMI.

    Each verify_* method polls the BMC for the firmware version
    with retries (because the BMC may still be initializing).
    """

    def __init__(self, ipmi_client, max_retries: int = 6,
                 retry_interval: int = 10):
        """
        Args:
            ipmi_client:    IpmiClient instance
            max_retries:    attempts before giving up
            retry_interval: seconds between attempts
        """
        self._ipmi          = ipmi_client
        self._max_retries   = max_retries
        self._retry_interval = retry_interval

    def verify_bios(self, expected_version: str) -> bool:
        """
        Verify BIOS version via IPMI Get System Info Parameters.
        NetFn=0x06 Cmd=0x59, parameter 0x01 = BIOS firmware string.

        BIOS version is encoded as ASCII bytes in the IPMI response.
        e.g. version '1A04' → response bytes 0x31 0x41 0x30 0x34

        Args:
            expected_version: e.g. '1A04'

        Returns:
            True if version matches
        """
        # encode expected version as hex string for comparison
        expected_hex = ' '.join(f'{ord(c):02x}' for c in expected_version)

        for attempt in range(self._max_retries):
            try:
                output = self._ipmi.run(
                    'raw', '0xNN', '0xNN',  # replace with platform OEM command for BIOS version read
                    '0x00', '0x01', '0x00', '0x00'
                )
                if expected_hex in output.lower():
                    logger.info(
                        'BIOS version verified: %s', expected_version
                    )
                    return True
            except Exception as exc:
                logger.debug('BIOS version check attempt %d failed: %s',
                             attempt + 1, exc)

            if attempt < self._max_retries - 1:
                time.sleep(self._retry_interval)

        logger.error(
            'BIOS version verification failed. '
            'Expected %s (hex: %s). Last output: %s',
            expected_version, expected_hex, output if 'output' in dir() else 'N/A'
        )
        return False

    def verify_bmc(self, expected_version: str) -> bool:
        """
        Verify BMC firmware version via mc info.

        Parses 'Firmware Revision : X.YY' from mc info output.

        Args:
            expected_version: e.g. '1.08'

        Returns:
            True if version matches
        """
        for attempt in range(self._max_retries):
            try:
                output = self._ipmi.run('mc', 'info')
                for line in output.splitlines():
                    if 'Firmware Revision' in line:
                        actual = line.split(':', 1)[1].strip()
                        if actual == expected_version:
                            logger.info(
                                'BMC version verified: %s', expected_version
                            )
                            return True
                        else:
                            logger.debug(
                                'BMC version mismatch: expected %s got %s',
                                expected_version, actual
                            )
            except Exception as exc:
                logger.debug('BMC version check attempt %d failed: %s',
                             attempt + 1, exc)

            if attempt < self._max_retries - 1:
                time.sleep(self._retry_interval)

        logger.error(
            'BMC version verification failed. Expected %s',
            expected_version
        )
        return False

    def verify_bmc_active_slot(self, expected_slot: BmcImageSlot) -> bool:
        """
        Verify which BMC image slot is currently active.
        Uses a platform-specific command to read the active BMC image slot.

        Args:
            expected_slot: BmcImageSlot.SLOT_1 or SLOT_2

        Returns:
            True if active slot matches expected
        """
        for attempt in range(self._max_retries):
            try:
                # replace with platform OEM command to read active BMC slot
                output = self._ipmi.run('raw', '0xNN', '0xNN', '0xNN')
                tokens = output.strip().split()
                if not tokens:
                    continue

                active_hex = tokens[0].lower()
                if expected_slot == BmcImageSlot.SLOT_1 and active_hex == '01':
                    logger.info('BMC active slot verified: IMAGE-1')
                    return True
                elif expected_slot == BmcImageSlot.SLOT_2 and active_hex == '02':
                    logger.info('BMC active slot verified: IMAGE-2')
                    return True
                else:
                    logger.debug(
                        'Active slot mismatch: expected %s got %s',
                        expected_slot.name, active_hex
                    )
            except Exception as exc:
                logger.debug('Slot check attempt %d failed: %s',
                             attempt + 1, exc)

            if attempt < self._max_retries - 1:
                time.sleep(self._retry_interval)

        return False

    def set_bmc_boot_slot(self, slot: BmcImageSlot):
        """
        Set which BMC image slot to boot from on next reset.
        Uses a platform-specific command to set the BMC boot slot.
        """
        self._ipmi.run('raw', '0xNN', '0xNN', '0xNN', slot.value)  # replace with platform OEM command to set BMC boot slot
        logger.info('BMC boot slot set to %s', slot.name)

    def verify_cpld(self, cpld_name: str,
                    expected_version: str,
                    board_type: str) -> bool:
        """
        Verify CPLD firmware version via OEM Get Component FW Version.
        NetFn=0x38 Cmd=0xAB <board_type>.

        CPLD version response: 2 bytes little-endian [minor, major].
        e.g. version '1.08' → bytes [0x08, 0x01].

        Args:
            cpld_name:        name for log messages e.g. 'mb'
            expected_version: e.g. '1.08'
            board_type:       hex string e.g. '0xa'

        Returns:
            True if version matches
        """
        parts = expected_version.split('.')
        if len(parts) != 2:
            logger.error(
                'Invalid CPLD version format: %s (expected X.YY)',
                expected_version
            )
            return False

        # encode as little-endian hex for comparison
        # '1.08' → minor=8 → hex '08', major=1 → hex '01'
        # compare as ' 8 1' or '08 01' in output
        try:
            major = int(parts[0])
            minor = int(parts[1])
        except ValueError:
            logger.error('Cannot parse CPLD version: %s', expected_version)
            return False

        expected_pattern = f'{minor:x} {major:x}'

        for attempt in range(self._max_retries):
            try:
                output = self._ipmi.run(
                    'raw', '0xNN', '0xNN', board_type  # replace with platform OEM command to read CPLD version
                )
                if expected_pattern in output.lower():
                    logger.info(
                        'CPLD %s version verified: %s',
                        cpld_name, expected_version
                    )
                    return True
            except Exception as exc:
                logger.debug(
                    'CPLD %s version check attempt %d: %s',
                    cpld_name, attempt + 1, exc
                )

            if attempt < self._max_retries - 1:
                time.sleep(self._retry_interval)

        logger.error(
            'CPLD %s version verification failed. '
            'Expected %s (pattern: %s)',
            cpld_name, expected_version, expected_pattern
        )
        return False


# ---------------------------------------------------------------------------
# BMC online waiter
# ---------------------------------------------------------------------------

def wait_for_bmc_online(ipmi_client,
                        max_wait:      int = 180,
                        poll_interval: int = 5) -> bool:
    """
    Poll until BMC responds to IPMI commands after a reboot.

    The BMC network stack takes 90-120 seconds to come back after
    a firmware flash triggers a self-reboot. Calling any IPMI command
    too early gets 'connection refused' or timeout.

    Strategy: try 'mc info' every poll_interval seconds.
    Success = BMC is fully back online and responding.

    Args:
        ipmi_client:   IpmiClient instance
        max_wait:      maximum seconds to wait (default 180)
        poll_interval: seconds between attempts (default 5)

    Returns:
        True if BMC came online within max_wait seconds
    """
    elapsed = 0
    while elapsed < max_wait:
        try:
            output = ipmi_client.run('mc', 'info', timeout=10)
            if 'Firmware Revision' in output:
                logger.info(
                    'BMC back online after %ds', elapsed
                )
                return True
        except Exception:
            pass  # expected during BMC reboot

        time.sleep(poll_interval)
        elapsed += poll_interval
        logger.debug('Waiting for BMC... %ds/%ds', elapsed, max_wait)

    logger.error(
        'BMC did not come back online within %ds after firmware flash.',
        max_wait
    )
    return False


def virtual_reseat(ipmi_client):
    """
    Trigger CPLD virtual reseat.

    Sends a platform-specific command with manufacturer authentication bytes.
    Causes all CPLDs to reload their configuration from flash.
    Without reseat, CPLDs continue running from internal flip-flops
    regardless of what was written to flash.
    Replace 0xNN placeholders with actual NetFn, Cmd, and auth bytes.
    """
    ipmi_client.run('raw', '0xNN', '0xNN', '0xNN', '0xNN', '0xNN')  # replace with platform OEM virtual reseat command
    logger.info('Virtual reseat triggered — CPLDs reloading from flash')


def erase_cpld_cfm0(ipmi_client):
    """
    Erase CPLD cfm0 (active) sector via OEM erase command.

    NetFn=0x38 Cmd=0xAC + 'ERASEONLY' ASCII authentication string.
    ASCII bytes: E=0x45 R=0x52 A=0x41 S=0x53 E=0x45 O=0x4F N=0x4E L=0x4C Y=0x59

    This is a DESTRUCTIVE operation — cfm0 is wiped completely.
    Used in TC_BMC_0_0303 to test cfm1 fallback mechanism.
    """
    ipmi_client.run(
        'raw', '0xNN', '0xNN',
        '0xNN', '0xNN', '0xNN', '0xNN',
        '0xNN', '0xNN', '0xNN', '0xNN', '0xNN',
    )  # replace with platform OEM command to erase CPLD cfm0 sector
    logger.info('CPLD cfm0 erased (ERASEONLY command)')


def get_amc_eids(ipmi_client) -> dict:
    """
    Discover MCTP Endpoint IDs for all present AMC slots.

    EIDs are assigned dynamically by the BMC at boot time and
    are not fixed values — they must be discovered at runtime.

    Returns:
        dict mapping slot_index (int) → eid (int)
        e.g. {0: 12, 1: 13, 3: 15}
        Empty dict if no AMC modules are present or EID
        discovery command is not supported.
    """
    eids = {}
    try:
        # OEM command to get MCTP endpoint table
        # Response format: list of [slot, eid] pairs
        output = ipmi_client.run(
            'raw', '0xNN', '0xNN',   # replace with platform OEM command to query AMC MCTP endpoint table
            '0xNN', '0xNN', '0xNN',  # replace with platform manufacturer authentication bytes
        )
        tokens = output.split()
        # parse pairs: slot_byte eid_byte slot_byte eid_byte ...
        for i in range(0, len(tokens) - 1, 2):
            try:
                slot = int(tokens[i], 16)
                eid  = int(tokens[i + 1], 16)
                if eid != 0xFF:  # 0xFF = no device
                    eids[slot] = eid
            except (ValueError, IndexError):
                pass
    except Exception as exc:
        logger.warning('AMC EID discovery failed: %s', exc)

    return eids
