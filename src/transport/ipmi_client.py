"""
IPMI LAN client — thin wrapper around ipmitool subprocess calls.

run()     → string output (for text commands like sdr, fru, sel)
run_raw() → bytes        (for binary protocol parsing)

Auth errors are not retried. Timeouts kill the subprocess and raise
IpmiTimeoutError. Passwords are masked in debug logs.
"""

import re
import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed exception hierarchy
# ---------------------------------------------------------------------------

class IpmiError(Exception):
    """Base class for all IPMI transport errors."""
    pass


class IpmiTransportError(IpmiError):
    """
    BMC rejected or could not process the command.
    Indicates a protocol-level failure, not a test assertion failure.
    """
    pass


class IpmiTimeoutError(IpmiError):
    """
    No response received within the timeout window.
    Raised after all retry attempts are exhausted.
    """
    pass


class IpmiAuthError(IpmiError):
    """
    BMC rejected the credentials supplied.
    Retrying with the same credentials will not help.
    """
    pass


# ---------------------------------------------------------------------------
# Error string patterns from ipmitool stdout
# ipmitool writes error messages to stdout, not stderr
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = [
    (r'Unable to send RAW command',      IpmiTransportError),
    (r'Error in open session response',  IpmiAuthError),
    (r'RAKP.*error',                     IpmiAuthError),
    (r'Unable to establish IPMI',        IpmiTransportError),
    (r'Get session info command failed', IpmiTransportError),
    (r'Error connecting to',             IpmiTransportError),
]


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class IpmiClient:
    """
    Single-responsibility IPMI transport client.

    Each test class creates one IpmiClient in setUpClass() and passes it
    to protocol-layer objects. No shared module-level state is used.

    Example:
        client = IpmiClient(
            host='192.168.1.100',
            user='admin',
            password='password',
        )
        output = client.run('mc', 'info')
        version = client.run('raw', '0x06', '0x01')
    """

    def __init__(self,
                 host:      str,
                 user:      str,
                 password:  str,
                 timeout:   int = 15,
                 interface: str = 'lanplus'):
        """
        Args:
            host:      BMC IP address or hostname
            user:      IPMI username
            password:  IPMI password
                       Sourced from environment variable BMC_PASSWORD
                       rather than hardcoded — see config/README
            timeout:   seconds before a command is considered timed out
            interface: ipmitool -I argument (lanplus for IPMI 2.0 RMCP+)
        """
        self.host    = host
        self.timeout = timeout

        # build base command list once — reused for every call
        # stored as list (not string) so subprocess.run receives
        # proper argv[] array with no shell injection risk
        self._base_cmd = [
            'ipmitool',
            '-H', host,
            '-I', interface,
            '-U', user,
            '-P', password,
        ]

        # safe version for logging — password replaced with ***
        self._log_cmd = [
            'ipmitool',
            '-H', host,
            '-I', interface,
            '-U', user,
            '-P', '***',
        ]

    def run(self, *args,
            retries: int = 2,
            timeout: Optional[int] = None) -> str:
        """
        Execute one IPMI command and return its stdout as a string.

        Args:
            *args:   command tokens e.g. 'mc', 'info'
                     or 'raw', '0x0a', '0x43', '0x0', '0x0', '0x0',
                        '0x0', '0x0', '0xFF'
            retries: retry attempts on timeout (default 2 = 3 total tries)
            timeout: override instance default for this call only

        Returns:
            stdout string from ipmitool, stripped of trailing whitespace

        Raises:
            IpmiTransportError: BMC rejected or cannot process command
            IpmiTimeoutError:   no response within timeout after all retries
            IpmiAuthError:      credential rejection (do not retry)
        """
        effective_timeout = timeout if timeout is not None else self.timeout
        full_cmd = self._base_cmd + list(args)
        log_cmd  = self._log_cmd  + list(args)

        logger.debug('IPMI: %s', ' '.join(log_cmd))

        last_exception: Optional[Exception] = None

        for attempt in range(retries + 1):

            if attempt > 0:
                logger.debug(
                    'IPMI retry %d/%d: %s',
                    attempt, retries, ' '.join(str(a) for a in args)
                )

            try:
                result = subprocess.run(
                    full_cmd,
                    capture_output=True,    # captures both stdout and stderr
                    text=True,              # decodes bytes to str automatically
                    timeout=effective_timeout,
                )

                # check for ipmitool error strings in stdout
                # (ipmitool writes errors to stdout, not stderr)
                for pattern, exc_class in _ERROR_PATTERNS:
                    if re.search(pattern, result.stdout, re.IGNORECASE):
                        raise exc_class(
                            f"ipmitool error for '{' '.join(args)}':\n"
                            f"{result.stdout.strip()}"
                        )

                # non-zero exit with no stdout — genuine failure
                if result.returncode != 0 and not result.stdout.strip():
                    raise IpmiTransportError(
                        f"ipmitool exited {result.returncode} "
                        f"for '{' '.join(args)}'.\n"
                        f"stderr: {result.stderr.strip()}"
                    )

                logger.debug(
                    'IPMI response (%d chars): %s%s',
                    len(result.stdout),
                    result.stdout[:60].strip(),
                    '...' if len(result.stdout) > 60 else '',
                )

                return result.stdout

            except subprocess.TimeoutExpired:
                last_exception = IpmiTimeoutError(
                    f"IPMI '{' '.join(args)}' timed out after "
                    f"{effective_timeout}s "
                    f"(attempt {attempt + 1}/{retries + 1})"
                )
                logger.warning('%s', last_exception)

                if attempt == retries:
                    raise last_exception
                # loop continues to next retry attempt

            except (IpmiTransportError, IpmiAuthError):
                # deterministic failures — retrying will not help
                raise

        # unreachable but satisfies type checker
        raise last_exception  # type: ignore

    def run_raw(self, netfn: int, cmd: int,
                *data: int, **kwargs) -> bytes:
        """
        Execute a raw IPMI command and return the response as bytes.

        Useful for binary protocol parsing where the response must be
        processed at the byte level (SEL records, JTAG IDCODE, etc.)

        Args:
            netfn: Network Function byte (integer)
            cmd:   Command byte (integer)
            *data: additional data bytes as integers
            **kwargs: forwarded to run() (timeout, retries)

        Returns:
            Response bytes parsed from ipmitool hex output.

        Example:
            # Get SEL Entry: NetFn=0x0A Cmd=0x43
            raw = client.run_raw(0x0A, 0x43, 0,0,0,0,0,0xFF)
            # raw is a bytes object for struct.unpack processing
        """
        hex_args = (
            ['raw', hex(netfn), hex(cmd)] +
            [hex(b) for b in data]
        )
        output = self.run(*hex_args, **kwargs)

        tokens = output.split()
        if not tokens:
            return bytes()

        try:
            return bytes(int(t, 16) for t in tokens if t)
        except ValueError as exc:
            raise IpmiTransportError(
                f"Non-hex token in raw response for "
                f"NetFn=0x{netfn:02X} Cmd=0x{cmd:02X}: {exc}\n"
                f"Raw output: {output!r}"
            ) from exc

    def get_device_info(self) -> dict:
        """
        Get BMC device identification via Get Device ID command.
        (NetFn=0x06 Cmd=0x01)

        Returns:
            dict with keys matching ipmitool mc info field names:
            'Firmware Revision', 'Manufacturer Name', 'Product ID', etc.

        Raises:
            IpmiTransportError: if required fields are missing from response
        """
        output = self.run('mc', 'info')
        info   = {}
        for line in output.splitlines():
            if ':' in line:
                key, _, value = line.partition(':')
                info[key.strip()] = value.strip()

        required = ['Firmware Revision', 'Manufacturer ID']
        missing  = [f for f in required if f not in info]
        if missing:
            raise IpmiTransportError(
                f"Get Device ID response missing fields: {missing}\n"
                f"Raw output: {output!r}"
            )
        return info

    def get_power_state(self) -> bool:
        """
        Return True if chassis power is on.
        Uses chassis power status command (NetFn=0x00 Cmd=0x01).
        """
        output = self.run('power', 'status')
        if 'Chassis Power is on' in output:
            return True
        if 'Chassis Power is off' in output:
            return False
        raise IpmiTransportError(
            f"Cannot determine power state from: {output!r}"
        )

    def set_power(self, action: str):
        """
        Control chassis power.

        Args:
            action: one of 'on', 'off', 'cycle', 'reset', 'soft'
        """
        valid = {'on', 'off', 'cycle', 'reset', 'soft'}
        if action not in valid:
            raise ValueError(
                f"Invalid power action '{action}'. Valid: {valid}"
            )
        self.run('chassis', 'power', action)
