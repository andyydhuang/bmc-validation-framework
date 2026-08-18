"""
src/tests/conftest.py

Pytest configuration and shared fixtures for BMC hardware tests.

Usage:
    # Unit tests only (no hardware needed — default):
    python -m pytest src/tests/ -v

    # Integration tests against real BMC:
    python -m pytest src/tests/ -v --integration \
        --bmc-ip 192.168.1.100 \
        --bmc-user admin \
        --bmc-password yourpassword \
        --sys-conf config/sys_conf.json \
        --test-config config/test_configs_example.json

Environment variable alternative (avoids password in shell history):
    export BMC_IP=192.168.1.100
    export BMC_USER=admin
    export BMC_PASSWORD=yourpassword
    python -m pytest src/tests/ -v --integration

How --integration works:
    All integration test functions are decorated with
    @pytest.mark.integration. The conftest adds that marker to the
    skip logic so they only run when --integration is passed.
    Unit tests (test_sdr_parser, test_sel_decoder, test_fru_validator,
    test_transport_*) are completely unaffected and always run without hardware.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))


# ---------------------------------------------------------------------------
# CLI option registration
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    """Register custom CLI flags for integration test runs."""
    parser.addoption(
        '--integration',
        action  = 'store_true',
        default = False,
        help    = 'Run BMC hardware tests against a real BMC',
    )
    parser.addoption(
        '--bmc-ip',
        default = os.environ.get('BMC_IP', ''),
        help    = 'BMC IP address (or set BMC_IP env var)',
    )
    parser.addoption(
        '--bmc-user',
        default = os.environ.get('BMC_USER', 'admin'),
        help    = 'BMC username (default: admin)',
    )
    parser.addoption(
        '--bmc-password',
        default = os.environ.get('BMC_PASSWORD', ''),
        help    = 'BMC password (or set BMC_PASSWORD env var)',
    )
    parser.addoption(
        '--sys-conf',
        default = 'config/sys_conf.json',
        help    = 'Path to sys_conf.json hardware presence bitmap',
    )
    parser.addoption(
        '--test-config',
        default = 'config/test_configs_example.json',
        help    = 'Path to test_configs.json authorization registry',
    )


# ---------------------------------------------------------------------------
# Marker registration — prevents pytest warning about unknown markers
# ---------------------------------------------------------------------------

def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'integration: marks tests that require a real BMC '
        '(run with --integration flag)',
    )


# ---------------------------------------------------------------------------
# Skip logic — integration tests are skipped unless --integration is set
# ---------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):
    """
    Automatically skip all @pytest.mark.integration tests unless
    --integration was passed on the command line.

    Unit tests are never skipped by this function.
    """
    if config.getoption('--integration'):
        return  # do not skip anything — run everything

    skip_marker = pytest.mark.skip(
        reason='Integration test — requires real BMC. '
               'Run with --integration flag.'
    )
    for item in items:
        if 'integration' in item.keywords:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def integration_enabled(request):
    """True when --integration flag was passed."""
    return request.config.getoption('--integration')


@pytest.fixture(scope='session')
def bmc_credentials(request):
    """
    Returns dict with BMC connection parameters.
    Raises immediately if --integration is active and credentials missing.
    """
    ip       = request.config.getoption('--bmc-ip')
    user     = request.config.getoption('--bmc-user')
    password = request.config.getoption('--bmc-password')

    if request.config.getoption('--integration'):
        if not ip:
            pytest.fail(
                'BMC IP not specified. Pass --bmc-ip <address> '
                'or set BMC_IP environment variable.'
            )
        if not password:
            pytest.fail(
                'BMC password not specified. Pass --bmc-password <pw> '
                'or set BMC_PASSWORD environment variable.'
            )

    return {'ip': ip, 'user': user, 'password': password}


@pytest.fixture(scope='session')
def sys_conf(request):
    """
    Load hardware presence bitmap from sys_conf.json.

    Returns empty dict if file is missing (unit tests do not need it).
    Fails immediately if --integration is active and file is missing.
    """
    path = request.config.getoption('--sys-conf')

    if not os.path.exists(path):
        if request.config.getoption('--integration'):
            pytest.fail(
                f'sys_conf.json not found at {path}. '
                f'Copy config/sys_conf_template.json to {path} '
                f'and fill in values for your hardware.'
            )
        return {}

    with open(path) as f:
        data = json.load(f)
    # strip comment keys
    return {k: v for k, v in data.items() if not k.startswith('_')}


@pytest.fixture(scope='session')
def test_config(request):
    """
    Load test case authorization registry from test_configs.json.

    Returns set of authorized TC IDs.
    """
    path = request.config.getoption('--test-config')
    if not os.path.exists(path):
        return set()

    with open(path) as f:
        data = json.load(f)
    return {k for k in data if not k.startswith('_')}


@pytest.fixture(scope='session')
def ipmi_client(bmc_credentials, request):
    """
    Create a real IpmiClient for BMC hardware tests.

    Session-scoped: one connection shared across all BMC hardware tests
    in one pytest run. This avoids repeated IPMI session setup overhead.
    """
    if not request.config.getoption('--integration'):
        pytest.skip('Integration only')

    from src.transport.ipmi_client import IpmiClient
    return IpmiClient(
        host     = bmc_credentials['ip'],
        user     = bmc_credentials['user'],
        password = bmc_credentials['password'],
        timeout  = 30,
    )


@pytest.fixture(scope='session')
def jtag_client(ipmi_client):
    """JtagClient built on the shared IpmiClient."""
    from src.transport.jtag_client import JtagClient
    return JtagClient(ipmi_client)


@pytest.fixture(scope='session')
def peci_client(ipmi_client):
    """PeciClient built on the shared IpmiClient. Tjmax=105 default."""
    from src.transport.peci_client import PeciClient
    return PeciClient(ipmi_client, tjmax=105)


@pytest.fixture(scope='session')
def smlink_client(ipmi_client):
    """SmlinkClient built on the shared IpmiClient."""
    from src.transport.peci_client import SmlinkClient
    return SmlinkClient(ipmi_client)


@pytest.fixture(scope='session')
def fan_controller(ipmi_client):
    """FanController built on the shared IpmiClient."""
    from src.transport.fan_controller import FanController
    return FanController(ipmi_client)


@pytest.fixture(scope='session')
def fru_verifier(ipmi_client):
    """FruIpmiVerifier built on the shared IpmiClient."""
    from src.protocol.fru_validator import FruIpmiVerifier
    return FruIpmiVerifier(ipmi_client)


# ---------------------------------------------------------------------------
# Helper: TC authorization check
# ---------------------------------------------------------------------------

def is_authorized(tc_id: str, test_config: set) -> bool:
    """
    Return True if tc_id is in the test_configs.json authorization set.
    Skips the test if not authorized rather than failing.
    """
    if tc_id not in test_config:
        pytest.skip(
            f'{tc_id} not in test_configs.json — '
            f'add it to authorize this test to run.'
        )
    return True
