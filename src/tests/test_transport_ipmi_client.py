"""
src/tests/test_transport_ipmi_client.py

Unit tests for src/transport/ipmi_client.py

Strategy: patch subprocess.run to simulate ipmitool responses.
No real BMC or network required.

Tests cover:
    - IpmiClient.run(): normal response, retry on timeout,
      error pattern detection, auth error, non-zero exit
    - IpmiClient.run_raw(): hex parsing, empty response,
      non-hex token error
    - IpmiClient.get_device_info(): field parsing, missing fields
    - IpmiClient.get_power_state(): on/off detection, ambiguous output
    - IpmiClient.set_power(): valid actions, invalid action guard

Run with:
    python -m pytest src/tests/test_transport_ipmi_client.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

import subprocess
from unittest.mock import patch, MagicMock

import pytest
from src.transport.ipmi_client import (
    IpmiClient,
    IpmiError,
    IpmiTransportError,
    IpmiTimeoutError,
    IpmiAuthError,
)


# ---------------------------------------------------------------------------
# Fixture: IpmiClient with dummy credentials (never makes real connections)
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return IpmiClient(
        host     = '192.168.1.100',
        user     = 'admin',
        password = 'testpass',
        timeout  = 5,
    )


def _mock_run(stdout: str = '', returncode: int = 0):
    """Build a mock subprocess.CompletedProcess result."""
    result          = MagicMock()
    result.stdout   = stdout
    result.stderr   = ''
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# IpmiClient.run() — normal cases
# ---------------------------------------------------------------------------

class TestIpmiClientRun:

    def test_run_returns_stdout_string(self, client):
        """Normal ipmitool response is returned as-is."""
        expected = 'Device ID : 32\nFirmware Revision : 1.07\n'
        with patch('subprocess.run', return_value=_mock_run(expected)):
            result = client.run('mc', 'info')
        assert result == expected

    def test_run_strips_nothing_preserves_newlines(self, client):
        """run() does not strip the output — callers strip as needed."""
        raw = 'line1\nline2\n'
        with patch('subprocess.run', return_value=_mock_run(raw)):
            result = client.run('sel', 'elist')
        assert result == raw

    def test_run_passes_command_tokens_as_list(self, client):
        """Verifies that subprocess.run receives a proper argv list."""
        with patch('subprocess.run', return_value=_mock_run('ok')) as mock:
            client.run('mc', 'info')
        call_args = mock.call_args[0][0]
        assert 'ipmitool'  in call_args
        assert 'mc'        in call_args
        assert 'info'      in call_args
        assert 'admin'     in call_args

    def test_password_included_in_subprocess_call(self, client):
        """Password is passed to subprocess (required for ipmitool auth)."""
        with patch('subprocess.run', return_value=_mock_run('ok')) as mock:
            client.run('mc', 'info')
        call_args = mock.call_args[0][0]
        assert 'testpass' in call_args

    def test_run_with_extra_args(self, client):
        """Extra args appended to base command correctly."""
        with patch('subprocess.run', return_value=_mock_run('00 01')) as mock:
            client.run('raw', '0x06', '0x01')
        call_args = mock.call_args[0][0]
        assert '0x06' in call_args
        assert '0x01' in call_args

    def test_empty_stdout_with_zero_exit_returns_empty(self, client):
        """Zero exit with empty stdout is valid (e.g. sel clear)."""
        with patch('subprocess.run', return_value=_mock_run('', 0)):
            result = client.run('sel', 'clear')
        assert result == ''


# ---------------------------------------------------------------------------
# IpmiClient.run() — error detection
# ---------------------------------------------------------------------------

class TestIpmiClientRunErrors:

    def test_unable_to_send_raw_raises_transport_error(self, client):
        """ipmitool 'Unable to send RAW command' → IpmiTransportError."""
        bad = 'Unable to send RAW command (channel=0x0 netfn=0x6 lun=0x0)\n'
        with patch('subprocess.run', return_value=_mock_run(bad)):
            with pytest.raises(IpmiTransportError):
                client.run('raw', '0x06', '0x01')

    def test_rakp_error_raises_auth_error(self, client):
        """RAKP error in stdout → IpmiAuthError (do not retry)."""
        bad = 'RAKP 2 message indicates an error : unauthorized name\n'
        with patch('subprocess.run', return_value=_mock_run(bad)):
            with pytest.raises(IpmiAuthError):
                client.run('mc', 'info')

    def test_unable_to_establish_raises_transport_error(self, client):
        """'Unable to establish IPMI' → IpmiTransportError."""
        bad = 'Unable to establish IPMI v2 / RMCP+ session\n'
        with patch('subprocess.run', return_value=_mock_run(bad)):
            with pytest.raises(IpmiTransportError):
                client.run('mc', 'info')

    def test_nonzero_exit_with_empty_stdout_raises_transport_error(self, client):
        """Non-zero exit + no stdout → IpmiTransportError."""
        with patch('subprocess.run',
                   return_value=_mock_run('', returncode=1)):
            with pytest.raises(IpmiTransportError):
                client.run('mc', 'info')

    def test_timeout_raises_ipmi_timeout_error(self, client):
        """subprocess.TimeoutExpired → IpmiTimeoutError after retries."""
        with patch('subprocess.run',
                   side_effect=subprocess.TimeoutExpired('ipmitool', 5)):
            with pytest.raises(IpmiTimeoutError):
                client.run('mc', 'info', retries=0)

    def test_timeout_retries_before_raising(self, client):
        """IpmiClient retries on timeout before giving up."""
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise subprocess.TimeoutExpired('ipmitool', 5)

        with patch('subprocess.run', side_effect=side_effect):
            with pytest.raises(IpmiTimeoutError):
                client.run('mc', 'info', retries=2)

        assert call_count == 3   # 1 initial + 2 retries

    def test_auth_error_not_retried(self, client):
        """Auth errors are not retried — retrying with same creds is pointless."""
        bad = 'RAKP 2 message indicates an error\n'
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _mock_run(bad)

        with patch('subprocess.run', side_effect=side_effect):
            with pytest.raises(IpmiAuthError):
                client.run('mc', 'info', retries=2)

        assert call_count == 1   # not retried


# ---------------------------------------------------------------------------
# IpmiClient.run_raw() — binary response parsing
# ---------------------------------------------------------------------------

class TestIpmiClientRunRaw:

    def test_hex_string_converted_to_bytes(self, client):
        """Space-separated hex string → bytes object."""
        with patch('subprocess.run',
                   return_value=_mock_run('00 01 c0 d2')):
            result = client.run_raw(0x0A, 0x43)
        assert result == bytes([0x00, 0x01, 0xC0, 0xD2])

    def test_empty_response_returns_empty_bytes(self, client):
        """Empty ipmitool output → empty bytes (not error)."""
        with patch('subprocess.run', return_value=_mock_run('')):
            result = client.run_raw(0x06, 0x01)
        assert result == bytes()

    def test_non_hex_token_raises_transport_error(self, client):
        """Non-hex characters in raw output → IpmiTransportError."""
        with patch('subprocess.run',
                   return_value=_mock_run('00 ZZ ff')):
            with pytest.raises(IpmiTransportError, match='Non-hex'):
                client.run_raw(0x06, 0x01)

    def test_run_raw_includes_netfn_and_cmd(self, client):
        """NetFn and Cmd are correctly hex-encoded in the command."""
        with patch('subprocess.run',
                   return_value=_mock_run('20')) as mock:
            client.run_raw(0x06, 0x01)
        call_args = mock.call_args[0][0]
        assert '0x6'  in call_args or '0x06' in call_args
        assert '0x1'  in call_args or '0x01' in call_args

    def test_run_raw_with_extra_data_bytes(self, client):
        """Extra data bytes are appended after NetFn and Cmd."""
        with patch('subprocess.run',
                   return_value=_mock_run('00')) as mock:
            client.run_raw(0x0A, 0x43, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF)
        call_args = mock.call_args[0][0]
        assert '0xff' in call_args or '0xFF' in call_args

    def test_single_byte_response(self, client):
        """Single byte response decoded correctly."""
        with patch('subprocess.run', return_value=_mock_run('40')):
            result = client.run_raw(0x32, 0xBF)
        assert result == bytes([0x40])

    def test_uppercase_hex_decoded(self, client):
        """Uppercase hex tokens (FF, C0) decoded correctly."""
        with patch('subprocess.run',
                   return_value=_mock_run('FF C0 3A')):
            result = client.run_raw(0x06, 0x01)
        assert result == bytes([0xFF, 0xC0, 0x3A])


# ---------------------------------------------------------------------------
# IpmiClient.get_device_info()
# ---------------------------------------------------------------------------

class TestIpmiClientGetDeviceInfo:

    _MC_INFO = (
        "Device ID                 : 32\n"
        "Firmware Revision         : 1.07\n"
        "IPMI Version              : 2.0\n"
        "Manufacturer ID           : 343\n"
        "Manufacturer Name         : Generic BMC Vendor\n"
        "Product ID                : 1000\n"
    )

    def test_returns_dict_with_all_fields(self, client):
        """mc info parsed into dict keyed by field name."""
        with patch('subprocess.run',
                   return_value=_mock_run(self._MC_INFO)):
            info = client.get_device_info()
        assert info['Firmware Revision'] == '1.07'
        assert info['Manufacturer ID']   == '343'
        assert info['Device ID']         == '32'

    def test_manufacturer_name_included(self, client):
        with patch('subprocess.run',
                   return_value=_mock_run(self._MC_INFO)):
            info = client.get_device_info()
        assert 'Manufacturer Name' in info
        assert info['Manufacturer Name'] == 'Generic BMC Vendor'

    def test_missing_required_field_raises_transport_error(self, client):
        """If Firmware Revision is absent, raises IpmiTransportError."""
        incomplete = "Device ID : 32\nManufacturer ID : 343\n"
        with patch('subprocess.run',
                   return_value=_mock_run(incomplete)):
            with pytest.raises(IpmiTransportError, match='missing fields'):
                client.get_device_info()

    def test_empty_response_raises_transport_error(self, client):
        """Empty mc info output raises IpmiTransportError."""
        with patch('subprocess.run', return_value=_mock_run('')):
            with pytest.raises(IpmiTransportError):
                client.get_device_info()

    def test_field_values_stripped_of_whitespace(self, client):
        """Field values are stripped — '  1.07  ' becomes '1.07'."""
        padded = "Firmware Revision         :   1.07   \nManufacturer ID : 343\n"
        with patch('subprocess.run', return_value=_mock_run(padded)):
            info = client.get_device_info()
        assert info['Firmware Revision'] == '1.07'


# ---------------------------------------------------------------------------
# IpmiClient.get_power_state()
# ---------------------------------------------------------------------------

class TestIpmiClientGetPowerState:

    def test_chassis_power_on_returns_true(self, client):
        with patch('subprocess.run',
                   return_value=_mock_run('Chassis Power is on\n')):
            assert client.get_power_state() is True

    def test_chassis_power_off_returns_false(self, client):
        with patch('subprocess.run',
                   return_value=_mock_run('Chassis Power is off\n')):
            assert client.get_power_state() is False

    def test_ambiguous_output_raises_transport_error(self, client):
        """Output that matches neither 'on' nor 'off' raises error."""
        with patch('subprocess.run',
                   return_value=_mock_run('unknown state\n')):
            with pytest.raises(IpmiTransportError, match='power state'):
                client.get_power_state()

    def test_empty_output_raises_transport_error(self, client):
        with patch('subprocess.run', return_value=_mock_run('')):
            with pytest.raises(IpmiTransportError):
                client.get_power_state()


# ---------------------------------------------------------------------------
# IpmiClient.set_power()
# ---------------------------------------------------------------------------

class TestIpmiClientSetPower:

    def test_valid_action_on(self, client):
        """'on' is a valid power action — no exception raised."""
        with patch('subprocess.run', return_value=_mock_run('')):
            client.set_power('on')   # should not raise

    def test_valid_action_off(self, client):
        with patch('subprocess.run', return_value=_mock_run('')):
            client.set_power('off')

    def test_valid_action_cycle(self, client):
        with patch('subprocess.run', return_value=_mock_run('')):
            client.set_power('cycle')

    def test_valid_action_reset(self, client):
        with patch('subprocess.run', return_value=_mock_run('')):
            client.set_power('reset')

    def test_invalid_action_raises_value_error(self, client):
        """Invalid action raises ValueError before any IPMI call."""
        with pytest.raises(ValueError, match='Invalid power action'):
            client.set_power('explode')

    def test_set_power_includes_action_in_command(self, client):
        """The action string appears in the subprocess command."""
        with patch('subprocess.run', return_value=_mock_run('')) as mock:
            client.set_power('off')
        call_args = mock.call_args[0][0]
        assert 'off' in call_args


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
