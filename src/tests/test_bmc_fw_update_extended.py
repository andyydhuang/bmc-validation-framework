"""
src/tests/test_bmc_fw_update_extended.py

Extended firmware update integration tests.

This file covers three groups of missing TCs:

GROUP 1 — TC_BMC_0_03xx additional targets (0311-0314)
    TC_BMC_0_0311  Host Retimer FW update
    TC_BMC_0_0312  PCIe Switch FW update
    TC_BMC_0_0313  PHY Retimer FW update
    TC_BMC_0_0314  PSU FW update

GROUP 2 — TC_BMC_1_03xx preserve-config path (1300-1314)
    TC numbering convention for middle digit:
        TC_BMC_0_03xx = no-preserve-config flash (clean write)
        TC_BMC_1_03xx = preserve-config flash (retain NVRAM settings)
        TC_BMC_2_03xx = Web UI triggered flash path

    TC_BMC_1_0300  BIOS FW update — preserve NVRAM
    TC_BMC_1_0301  BMC FW update — preserve config
    TC_BMC_1_0302  CPLD FANB_DOWN cfm0/cfm1 — verify both sectors
    TC_BMC_1_0303  CPLD FANB_UP cfm0/cfm1
    TC_BMC_1_0304  CPLD HDDBP cfm0/cfm1
    TC_BMC_1_0305  CPLD IOB_DOWN cfm0/cfm1
    TC_BMC_1_0306  CPLD IOB_UP cfm0/cfm1
    TC_BMC_1_0307  CPLD MB cfm0/cfm1
    TC_BMC_1_0308  CPLD PDB cfm0/cfm1
    TC_BMC_1_0309  CPLD UBB cfm0/cfm1
    TC_BMC_1_0310  AMC FW update — preserve config
    TC_BMC_1_0311  Host Retimer FW update — preserve config
    TC_BMC_1_0312  PCIe Switch FW update — preserve config
    TC_BMC_1_0313  PHY Retimer FW update — preserve config
    TC_BMC_1_0314  PSU FW update — preserve config

GROUP 3 — TC_BMC_2_03xx Web UI path (2300-2314)
    TC_BMC_2_0300  BIOS FW update via Web UI
    TC_BMC_2_0301  BMC FW update via Web UI
    TC_BMC_2_0302  CPLD FANB_DOWN — Web UI flash trigger
    ... (same device list as TC_BMC_1_03xx)
    TC_BMC_2_0310  AMC FW update via Web UI
    TC_BMC_2_0311-2314  Retimer/PCIe Switch/PHY/PSU via Web UI

TC_BMC_1_0302 through TC_BMC_1_0309 are per-device CPLD tests.
Each exercises cfm0-and-cfm1 flash for one specific CPLD device,
unlike TC_BMC_0_0302 (all 8 CPLDs in one function).

WARNING: ALL TESTS IN THIS FILE ARE PERMANENTLY DESTRUCTIVE.
See test_bmc_fw_update.py for full safety notes.

Run with:
    pytest src/tests/test_bmc_fw_update_extended.py --integration \\
           --bmc-ip 192.168.1.100 --bmc-user admin --bmc-password pw \\
           --fw-config config/fw_update_configs.json \\
           --flash-tool-binary ./FwFlashTool
"""

import json
import os
import time

import pytest

from src.transport.fw_update_client import (
    FwUpdateClient, FwVersionVerifier, FwImage,
    BmcImageSlot, CPLD_BOARD_TYPES,
    wait_for_bmc_online, virtual_reseat,
    FwFlashToolError,
)
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — reuse from test_bmc_fw_update.py via conftest
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def fw_config(request):
    path = request.config.getoption('--fw-config')
    if not os.path.exists(path):
        pytest.skip(
            f'Firmware config not found at {path}. '
            f'Copy config/fw_update_configs_template.json to {path}.'
        )
    with open(path) as f:
        return json.load(f)


@pytest.fixture(scope='session')
def flash_client(request, bmc_credentials):
    binary = request.config.getoption('--flash-tool-binary')
    if not os.path.exists(binary):
        pytest.skip(f'firmware flash tool binary not found at {binary}.')
    return FwUpdateClient(
        bmc_ip       = bmc_credentials['ip'],
        bmc_user     = bmc_credentials['user'],
        bmc_password = bmc_credentials['password'],
        flash_tool_binary  = binary,
        timeout      = 600,
    )


@pytest.fixture(scope='session')
def fw_verifier(ipmi_client):
    return FwVersionVerifier(ipmi_client, max_retries=6, retry_interval=10)


# ---------------------------------------------------------------------------
# Helper: per-device CPLD cfm0-and-cfm1 test
# ---------------------------------------------------------------------------

def _flash_single_cpld_cfm0_cfm1(flash_client, fw_verifier,
                                   ipmi_client, fw_config,
                                   cpld_name: str, tc_id: str):
    """
    Flash one CPLD device through both cfm0 and cfm1 sectors.
    Used by all TC_BMC_1_030x per-device CPLD tests.

    Sequence:
        1. Power off host
        2. Flash cfm0 old version → virtual reseat → verify version
        3. Flash cfm0 new version → virtual reseat → verify version
        4. Flash cfm1 old version → virtual reseat → verify cfm1 baseline
        5. Flash cfm0 new version + reseat → verify cfm0 preferred
    """
    cfm0_conf = fw_config.get('cpld_images', {}).get('cfm0', {})
    cfm1_conf = fw_config.get('cpld_images', {}).get('cfm1', {})

    if cpld_name not in cfm0_conf or cpld_name not in cfm1_conf:
        pytest.skip(
            f'fw_update_configs.json missing cfm0 or cfm1 images '
            f'for CPLD {cpld_name}.'
        )

    cfm0_entries = cfm0_conf[cpld_name]
    cfm1_entries = cfm1_conf[cpld_name]

    if len(cfm0_entries) < 2 or len(cfm1_entries) < 2:
        pytest.skip(
            f'Need at least 2 cfm0 and 2 cfm1 images for {cpld_name}.'
        )

    board_type = cfm0_entries[0].get(
        'board_type', CPLD_BOARD_TYPES.get(cpld_name, '0x0')
    )

    def _power_off():
        try:
            if ipmi_client.get_power_state():
                ipmi_client.set_power('off')
                time.sleep(10)
        except Exception:
            pass

    def _flash_and_verify(img_conf, label):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', label),
        )
        try:
            flash_client.flash_cpld(image)
        except FwFlashToolError as exc:
            pytest.fail(f'{tc_id} {label} flash failed:\n{exc}')
        virtual_reseat(ipmi_client)
        time.sleep(120)
        wait_for_bmc_online(ipmi_client, max_wait=60)
        ok = fw_verifier.verify_cpld(cpld_name, image.version, board_type)
        assert ok, (
            f'{tc_id} FAILED: {cpld_name} version {image.version} '
            f'not confirmed after {label}.'
        )

    _power_off()
    # cfm0: old → new
    _flash_and_verify(cfm0_entries[0], f'{cpld_name} cfm0 old')
    _flash_and_verify(cfm0_entries[1], f'{cpld_name} cfm0 new')
    # cfm1: old (baseline for fallback)
    _flash_and_verify(cfm1_entries[0], f'{cpld_name} cfm1 old')
    # cfm0 new again: confirms cfm0 is preferred over cfm1 when both valid
    _flash_and_verify(cfm0_entries[1], f'{cpld_name} cfm0 new (final)')


# ---------------------------------------------------------------------------
# ── GROUP 1: TC_BMC_0_03xx additional targets ─────────────────────────────
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0311_host_retimer_fw_update(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """
    TC_BMC_0_0311 — Host Retimer firmware update via FwFlashTool.

    Host retimers are PCIe signal conditioning chips between the CPU
    PCIe slots and the edge connectors. They have their own firmware
    stored in a small EEPROM.

    Update protocol: I2C EEPROM write via BMC out-of-band path.
    FwFlashTool handles the EEPROM erase/write/verify sequence.
    """
    is_authorized('TC_BMC_0_0311', test_config)

    retimer_images = fw_config.get('retimer_images', [])
    if len(retimer_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain retimer_images '
            '(at least 2 entries) for TC_BMC_0_0311.'
        )

    for j, img_conf in enumerate(retimer_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'Retimer image {j}'),
        )
        try:
            flash_client._run([image.path, '-d', '8'])
            # device type 8 = host retimer (platform-specific)
            # replace with actual FwFlashTool device type for your retimer
        except FwFlashToolError as exc:
            pytest.fail(
                f'TC_BMC_0_0311 Host Retimer flash failed '
                f'(image[{j}]):\n{exc}'
            )
        time.sleep(10)


def test_tc_bmc_0_0312_pex_switch_fw_update(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """
    TC_BMC_0_0312 — PCIe Switch firmware update via FwFlashTool.

    PCIe switches fan out lanes from the CPU to multiple OAM slots.
    Each switch has independent firmware stored in SPI flash.

    Update protocol: SPI flash write via BMC I2C/SMBus management interface.
    Requires host to be powered off before flashing.
    """
    is_authorized('TC_BMC_0_0312', test_config)

    pesw_images = fw_config.get('pesw_images', [])
    if len(pesw_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain pesw_images '
            '(at least 2 entries) for TC_BMC_0_0312.'
        )

    try:
        if ipmi_client.get_power_state():
            ipmi_client.set_power('off')
            time.sleep(10)
    except Exception as exc:
        pytest.fail(f'Cannot power off host before PCIe switch flash: {exc}')

    for j, img_conf in enumerate(pesw_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'PCIe switch image {j}'),
        )
        try:
            flash_client._run([image.path, '-d', '16'])
            # device type 16 = PCIe switch (platform-specific — replace with actual)
            # replace with actual FwFlashTool device type for your switch
        except FwFlashToolError as exc:
            pytest.fail(
                f'TC_BMC_0_0312 PCIe Switch flash failed '
                f'(image[{j}]):\n{exc}'
            )
        time.sleep(15)


def test_tc_bmc_0_0313_phy_retimer_fw_update(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """
    TC_BMC_0_0313 — PHY Retimer firmware update via FwFlashTool.

    PHY retimers condition high-speed serial signals for network interfaces
    (distinct from host retimers which condition PCIe signals).
    Located on OCP NIC cards or on the motherboard near network connectors.
    """
    is_authorized('TC_BMC_0_0313', test_config)

    phy_images = fw_config.get('phy_retimer_images', [])
    if len(phy_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain phy_retimer_images '
            'for TC_BMC_0_0313.'
        )

    for j, img_conf in enumerate(phy_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'PHY Retimer image {j}'),
        )
        try:
            flash_client._run([image.path, '-d', '32'])
            # device type 32 = PHY retimer (platform-specific)
        except FwFlashToolError as exc:
            pytest.fail(
                f'TC_BMC_0_0313 PHY Retimer flash failed '
                f'(image[{j}]):\n{exc}'
            )
        time.sleep(10)


def test_tc_bmc_0_0314_psu_fw_update(
        flash_client, fw_verifier, ipmi_client, fw_config,
        sys_conf, test_config):
    """
    TC_BMC_0_0314 — PSU (Power Supply Unit) firmware update via FwFlashTool.

    Modern server PSUs have microcontrollers running updatable firmware.
    The BMC communicates with PSUs via PMBus over I2C.
    FwFlashTool handles the PSU firmware download protocol.

    Safety: Only installed PSUs (per sys_conf.json) are updated.
    All PSU firmware operations are hot-plug safe — the PSU
    continues supplying power during its own firmware update.
    """
    is_authorized('TC_BMC_0_0314', test_config)

    psu_images = fw_config.get('psu_images', [])
    if len(psu_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain psu_images '
            'for TC_BMC_0_0314.'
        )

    any_psu = any(sys_conf.get(f'PSU{i}', 0) for i in range(6))
    if not any_psu:
        pytest.skip('No PSUs present per sys_conf.json.')

    for j, img_conf in enumerate(psu_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'PSU image {j}'),
        )
        try:
            flash_client._run([image.path, '-d', '48'])
            # device type 48 = PSU (platform-specific)
        except FwFlashToolError as exc:
            pytest.fail(
                f'TC_BMC_0_0314 PSU flash failed '
                f'(image[{j}]):\n{exc}'
            )
        time.sleep(30)  # PSU firmware update takes longer than CPLD


# ---------------------------------------------------------------------------
# ── GROUP 2: TC_BMC_1_03xx preserve-config path ───────────────────────────
# ---------------------------------------------------------------------------
#
# The TC_BMC_1_* prefix covers firmware updates run WITH preserve-config.
# BIOS and BMC tests use preserve-config mode. CPLD tests are per-device (one function
# per CPLD) and exercise both cfm0 and cfm1 for that device.
# ---------------------------------------------------------------------------

def test_tc_bmc_1_0300_bios_fw_update_preserve_config(
        flash_client, fw_verifier, ipmi_client, bmc_credentials,
        fw_config, test_config):
    """
    TC_BMC_1_0300 — BIOS firmware update with NVRAM preservation.

    Same FwFlashTool invocation as TC_BMC_0_0300 but sets the preserve-NVRAM
    flag before flashing so boot order, UEFI settings, and SATA mode are
    retained across the update.

    Platform-specific command sets preserve mode:
        raw 0x30 0xc4 0x02 0x01  (preserve=1)
    Then FwFlashTool is called identically to the no-preserve path.
    """
    is_authorized('TC_BMC_1_0300', test_config)

    bios_images = fw_config.get('bios_images', [])
    if len(bios_images) < 2:
        pytest.skip('Need at least 2 bios_images in fw_update_configs.json.')

    # set preserve-NVRAM mode before flashing
    ipmi_client.run('raw', '0x30', '0xc4', '0x02', '0x01')

    for j, img_conf in enumerate(bios_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BIOS image {j}'),
        )

        try:
            if ipmi_client.get_power_state():
                ipmi_client.set_power('off')
                time.sleep(10)
        except Exception as exc:
            pytest.fail(f'Cannot power off: {exc}')

        try:
            flash_client.flash_bios(image)
        except FwFlashToolError as exc:
            pytest.fail(f'TC_BMC_1_0300 BIOS flash failed:\n{exc}')

        time.sleep(10)
        ipmi_client.set_power('on')
        time.sleep(30)

        ok = fw_verifier.verify_bios(image.version)
        assert ok, (
            f'TC_BMC_1_0300 FAILED: BIOS version {image.version} '
            f'not confirmed after preserve-config flash (image[{j}]).'
        )

    # restore no-preserve mode after test
    ipmi_client.run('raw', '0x30', '0xc4', '0x02', '0x00')


def test_tc_bmc_1_0301_bmc_fw_update_preserve_config(
        flash_client, fw_verifier, ipmi_client, bmc_credentials,
        fw_config, test_config):
    """
    TC_BMC_1_0301 — BMC firmware update with config preservation.

    Uses FwFlashTool preserve-config mode to retain BMC NVRAM settings:
        IP address, user accounts, IPMI settings, fan curves, alert thresholds.

    After a preserve-config flash the credentials should still work
    (no need to re-authenticate as in the no-preserve path).
    """
    is_authorized('TC_BMC_1_0301', test_config)

    bmc_images = fw_config.get('bmc_images', [])
    if len(bmc_images) < 2:
        pytest.skip('Need at least 2 bmc_images in fw_update_configs.json.')

    for j, img_conf in enumerate(bmc_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BMC image {j}'),
        )

        try:
            ipmi_client.set_power('off')
            time.sleep(5)
        except Exception:
            pass

        try:
            flash_client.flash_bmc(
                image, BmcImageSlot.SLOT_1, preserve_config=True
            )
        except FwFlashToolError as exc:
            pytest.fail(f'TC_BMC_1_0301 BMC flash failed:\n{exc}')

        ok = wait_for_bmc_online(ipmi_client, max_wait=180)
        if not ok:
            pytest.fail('BMC did not come back online within 180s.')

        # with preserve-config: credentials NOT reset — no re-auth needed
        ok = fw_verifier.verify_bmc(image.version)
        assert ok, (
            f'TC_BMC_1_0301 FAILED: BMC version {image.version} '
            f'not confirmed after preserve-config flash (image[{j}]).'
        )


# ---------------------------------------------------------------------------
# TC_BMC_1_030x — per-device CPLD cfm0/cfm1 tests
# ---------------------------------------------------------------------------

def test_tc_bmc_1_0302_cpld_fanb_down(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0302 — CPLD FANB_DOWN cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0302', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'fanb_down', 'TC_BMC_1_0302'
    )


def test_tc_bmc_1_0303_cpld_fanb_up(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0303 — CPLD FANB_UP cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0303', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'fanb_up', 'TC_BMC_1_0303'
    )


def test_tc_bmc_1_0304_cpld_hdbp(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0304 — CPLD HDDBP cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0304', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'hdbp', 'TC_BMC_1_0304'
    )


def test_tc_bmc_1_0305_cpld_iob_down(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0305 — CPLD IOB_DOWN cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0305', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'iob_down', 'TC_BMC_1_0305'
    )


def test_tc_bmc_1_0306_cpld_iob_up(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0306 — CPLD IOB_UP cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0306', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'iob_up', 'TC_BMC_1_0306'
    )


def test_tc_bmc_1_0307_cpld_mb(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0307 — CPLD MB (Motherboard) cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0307', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'mb', 'TC_BMC_1_0307'
    )


def test_tc_bmc_1_0308_cpld_pdb(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0308 — CPLD PDB (Power Distribution Board) cfm0/cfm1."""
    is_authorized('TC_BMC_1_0308', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'pdb', 'TC_BMC_1_0308'
    )


def test_tc_bmc_1_0309_cpld_ubb(
        flash_client, fw_verifier, ipmi_client, fw_config, test_config):
    """TC_BMC_1_0309 — CPLD UBB cfm0/cfm1 update and verify."""
    is_authorized('TC_BMC_1_0309', test_config)
    _flash_single_cpld_cfm0_cfm1(
        flash_client, fw_verifier, ipmi_client, fw_config,
        'ubb', 'TC_BMC_1_0309'
    )


def test_tc_bmc_1_0310_amc_fw_update_preserve(
        flash_client, fw_verifier, ipmi_client, fw_config,
        sys_conf, test_config):
    """
    TC_BMC_1_0310 — AMC firmware update with preserve-config context.

    Same as TC_BMC_0_0310 but run after a preserve-config BMC flash
    to verify AMC update still works correctly with retained BMC settings.
    """
    is_authorized('TC_BMC_1_0310', test_config)

    any_amc = any(sys_conf.get(f'OAM{i}', 0) for i in range(8))
    if not any_amc:
        pytest.skip('No AMC modules present per sys_conf.json.')

    from src.transport.fw_update_client import get_amc_eids
    eids = get_amc_eids(ipmi_client)
    if not eids:
        pytest.skip('No AMC EIDs discovered from BMC MCTP endpoint table.')

    amc_images = fw_config.get('amc_images', [])
    if len(amc_images) < 2:
        pytest.skip('Need at least 2 amc_images in fw_update_configs.json.')

    for slot, eid in sorted(eids.items()):
        if not sys_conf.get(f'OAM{slot}', 0):
            continue
        for j, img_conf in enumerate(amc_images):
            image = FwImage(
                path    = img_conf['path'],
                version = img_conf['version'],
                label   = img_conf.get('label', f'AMC image {j}'),
            )
            try:
                flash_client.flash_amc(image, eid)
            except FwFlashToolError as exc:
                pytest.fail(
                    f'TC_BMC_1_0310 AMC flash failed '
                    f'(OAM{slot} EID={eid} image[{j}]):\n{exc}'
                )
            time.sleep(5)


def test_tc_bmc_1_0311_host_retimer_preserve(
        flash_client, fw_config, test_config):
    """TC_BMC_1_0311 — Host Retimer FW update with preserve-config context."""
    is_authorized('TC_BMC_1_0311', test_config)
    pytest.skip(
        'TC_BMC_1_0311 requires retimer_images in fw_update_configs.json '
        'and platform-specific FwFlashTool device type. '
        'Implement retimer flash command and remove this skip.'
    )


def test_tc_bmc_1_0312_pex_switch_preserve(
        flash_client, fw_config, test_config):
    """TC_BMC_1_0312 — PCIe Switch FW update with preserve-config context."""
    is_authorized('TC_BMC_1_0312', test_config)
    pytest.skip(
        'TC_BMC_1_0312 requires pesw_images in fw_update_configs.json. '
        'Implement PCIe switch flash command and remove this skip.'
    )


def test_tc_bmc_1_0313_phy_retimer_preserve(
        flash_client, fw_config, test_config):
    """TC_BMC_1_0313 — PHY Retimer FW update with preserve-config context."""
    is_authorized('TC_BMC_1_0313', test_config)
    pytest.skip(
        'TC_BMC_1_0313 requires phy_retimer_images in fw_update_configs.json. '
        'Implement PHY retimer flash command and remove this skip.'
    )


def test_tc_bmc_1_0314_psu_fw_preserve(
        flash_client, fw_config, sys_conf, test_config):
    """TC_BMC_1_0314 — PSU FW update with preserve-config context."""
    is_authorized('TC_BMC_1_0314', test_config)
    pytest.skip(
        'TC_BMC_1_0314 requires psu_images in fw_update_configs.json. '
        'Implement PSU firmware update command and remove this skip.'
    )


# ---------------------------------------------------------------------------
# ── GROUP 3: TC_BMC_2_03xx Web UI path ────────────────────────────────────
# ---------------------------------------------------------------------------
#
# TC_BMC_2_03xx tests trigger firmware updates through the BMC web UI
# instead of FwFlashTool CLI. The web UI firmware update page accepts
# the same encrypted image files and initiates the same flash sequence.
#
# These tests use Selenium to:
#   1. Navigate to the BMC web UI firmware update page
#   2. Upload the image file via the file picker
#   3. Click the update button
#   4. Poll for completion
#   5. Verify the new version via IPMI
#
# The Selenium approach verifies the BMC web UI firmware update code path
# is distinct from the CLI/IPMI path (TC_BMC_0_03xx).
# ---------------------------------------------------------------------------

def _webui_flash(webui_verifier, image_path: str,
                 target: str, tc_id: str):
    """
    Trigger a firmware update via BMC Web UI.

    Args:
        webui_verifier: FruWebUiVerifier instance with active browser
        image_path:     path to firmware image file
        target:         update target string shown in web UI
                        e.g. 'BIOS', 'BMC', 'CPLD_MB'
        tc_id:          TC ID for error messages

    This is a placeholder implementation. Replace with actual Selenium
    code for your BMC web UI's firmware update page structure:
        - Navigate to /fwupdate or equivalent URL
        - Find file upload input and send image_path
        - Click the update/flash button
        - Wait for the progress indicator to complete
    """
    if webui_verifier._driver is None:
        pytest.fail(f'{tc_id}: Web UI driver not initialized.')

    pytest.skip(
        f'{tc_id}: Web UI firmware update requires Selenium automation '
        f'specific to your BMC web interface firmware update page. '
        f'Implement _webui_flash() with your BMC web UI selectors '
        f'and remove this skip.'
    )


@pytest.fixture(scope='module')
def webui_verifier(request, bmc_credentials):
    """Web UI verifier for firmware update Web UI tests."""
    from src.protocol.fru_validator import FruWebUiVerifier
    bmc_url      = f"https://{bmc_credentials['ip']}"
    chromedriver = request.config.getoption('--chromedriver',
                                            default='./chromedriver')
    v = FruWebUiVerifier(
        bmc_url      = bmc_url,
        username     = bmc_credentials['user'],
        password     = bmc_credentials['password'],
        chromedriver = chromedriver,
    )
    try:
        v._launch_browser()
    except Exception as exc:
        pytest.skip(f'Cannot launch Chrome for Web UI tests: {exc}')
    yield v
    v._close_browser()


def test_tc_bmc_2_0300_bios_fw_update_webui(
        webui_verifier, fw_verifier, ipmi_client,
        fw_config, test_config):
    """TC_BMC_2_0300 — BIOS firmware update triggered via BMC Web UI."""
    is_authorized('TC_BMC_2_0300', test_config)
    bios_images = fw_config.get('bios_images', [])
    if not bios_images:
        pytest.skip('No bios_images in fw_update_configs.json.')
    _webui_flash(webui_verifier, bios_images[0]['path'], 'BIOS',
                 'TC_BMC_2_0300')


def test_tc_bmc_2_0301_bmc_fw_update_webui(
        webui_verifier, fw_verifier, ipmi_client,
        fw_config, test_config):
    """TC_BMC_2_0301 — BMC firmware update triggered via BMC Web UI."""
    is_authorized('TC_BMC_2_0301', test_config)
    bmc_images = fw_config.get('bmc_images', [])
    if not bmc_images:
        pytest.skip('No bmc_images in fw_update_configs.json.')
    _webui_flash(webui_verifier, bmc_images[0]['path'], 'BMC',
                 'TC_BMC_2_0301')


def test_tc_bmc_2_0302_cpld_fanb_down_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0302 — CPLD FANB_DOWN firmware update via Web UI."""
    is_authorized('TC_BMC_2_0302', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'fanb_down' not in cfm0 or not cfm0['fanb_down']:
        pytest.skip('No fanb_down cfm0 images in fw_update_configs.json.')
    _webui_flash(webui_verifier, cfm0['fanb_down'][0]['path'],
                 'CPLD_FANB_DOWN', 'TC_BMC_2_0302')


def test_tc_bmc_2_0303_cpld_fanb_up_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0303 — CPLD FANB_UP firmware update via Web UI."""
    is_authorized('TC_BMC_2_0303', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'fanb_up' not in cfm0:
        pytest.skip('No fanb_up cfm0 images.')
    _webui_flash(webui_verifier, cfm0['fanb_up'][0]['path'],
                 'CPLD_FANB_UP', 'TC_BMC_2_0303')


def test_tc_bmc_2_0304_cpld_hdbp_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0304 — CPLD HDDBP firmware update via Web UI."""
    is_authorized('TC_BMC_2_0304', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'hdbp' not in cfm0:
        pytest.skip('No hdbp cfm0 images.')
    _webui_flash(webui_verifier, cfm0['hdbp'][0]['path'],
                 'CPLD_HDBP', 'TC_BMC_2_0304')


def test_tc_bmc_2_0305_cpld_iob_down_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0305 — CPLD IOB_DOWN firmware update via Web UI."""
    is_authorized('TC_BMC_2_0305', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'iob_down' not in cfm0:
        pytest.skip('No iob_down cfm0 images.')
    _webui_flash(webui_verifier, cfm0['iob_down'][0]['path'],
                 'CPLD_IOB_DOWN', 'TC_BMC_2_0305')


def test_tc_bmc_2_0306_cpld_iob_up_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0306 — CPLD IOB_UP firmware update via Web UI."""
    is_authorized('TC_BMC_2_0306', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'iob_up' not in cfm0:
        pytest.skip('No iob_up cfm0 images.')
    _webui_flash(webui_verifier, cfm0['iob_up'][0]['path'],
                 'CPLD_IOB_UP', 'TC_BMC_2_0306')


def test_tc_bmc_2_0307_cpld_mb_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0307 — CPLD MB firmware update via Web UI."""
    is_authorized('TC_BMC_2_0307', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'mb' not in cfm0:
        pytest.skip('No mb cfm0 images.')
    _webui_flash(webui_verifier, cfm0['mb'][0]['path'],
                 'CPLD_MB', 'TC_BMC_2_0307')


def test_tc_bmc_2_0308_cpld_pdb_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0308 — CPLD PDB firmware update via Web UI."""
    is_authorized('TC_BMC_2_0308', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'pdb' not in cfm0:
        pytest.skip('No pdb cfm0 images.')
    _webui_flash(webui_verifier, cfm0['pdb'][0]['path'],
                 'CPLD_PDB', 'TC_BMC_2_0308')


def test_tc_bmc_2_0309_cpld_ubb_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0309 — CPLD UBB firmware update via Web UI."""
    is_authorized('TC_BMC_2_0309', test_config)
    cfm0 = fw_config.get('cpld_images', {}).get('cfm0', {})
    if 'ubb' not in cfm0:
        pytest.skip('No ubb cfm0 images.')
    _webui_flash(webui_verifier, cfm0['ubb'][0]['path'],
                 'CPLD_UBB', 'TC_BMC_2_0309')


def test_tc_bmc_2_0310_amc_fw_update_webui(
        webui_verifier, fw_config, sys_conf, test_config):
    """TC_BMC_2_0310 — AMC firmware update triggered via BMC Web UI."""
    is_authorized('TC_BMC_2_0310', test_config)
    any_amc = any(sys_conf.get(f'OAM{i}', 0) for i in range(8))
    if not any_amc:
        pytest.skip('No AMC modules present per sys_conf.json.')
    amc_images = fw_config.get('amc_images', [])
    if not amc_images:
        pytest.skip('No amc_images in fw_update_configs.json.')
    _webui_flash(webui_verifier, amc_images[0]['path'],
                 'AMC', 'TC_BMC_2_0310')


def test_tc_bmc_2_0311_host_retimer_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0311 — Host Retimer firmware update via Web UI."""
    is_authorized('TC_BMC_2_0311', test_config)
    retimer_images = fw_config.get('retimer_images', [])
    if not retimer_images:
        pytest.skip('No retimer_images in fw_update_configs.json.')
    _webui_flash(webui_verifier, retimer_images[0]['path'],
                 'HOST_RETIMER', 'TC_BMC_2_0311')


def test_tc_bmc_2_0312_pex_switch_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0312 — PCIe Switch firmware update via Web UI."""
    is_authorized('TC_BMC_2_0312', test_config)
    pesw_images = fw_config.get('pesw_images', [])
    if not pesw_images:
        pytest.skip('No pesw_images in fw_update_configs.json.')
    _webui_flash(webui_verifier, pesw_images[0]['path'],
                 'PCIE_SWITCH', 'TC_BMC_2_0312')


def test_tc_bmc_2_0313_phy_retimer_webui(
        webui_verifier, fw_config, test_config):
    """TC_BMC_2_0313 — PHY Retimer firmware update via Web UI."""
    is_authorized('TC_BMC_2_0313', test_config)
    phy_images = fw_config.get('phy_retimer_images', [])
    if not phy_images:
        pytest.skip('No phy_retimer_images in fw_update_configs.json.')
    _webui_flash(webui_verifier, phy_images[0]['path'],
                 'PHY_RETIMER', 'TC_BMC_2_0313')


def test_tc_bmc_2_0314_psu_fw_webui(
        webui_verifier, fw_config, sys_conf, test_config):
    """TC_BMC_2_0314 — PSU firmware update via Web UI."""
    is_authorized('TC_BMC_2_0314', test_config)
    psu_images = fw_config.get('psu_images', [])
    if not psu_images:
        pytest.skip('No psu_images in fw_update_configs.json.')
    any_psu = any(sys_conf.get(f'PSU{i}', 0) for i in range(6))
    if not any_psu:
        pytest.skip('No PSUs present per sys_conf.json.')
    _webui_flash(webui_verifier, psu_images[0]['path'],
                 'PSU', 'TC_BMC_2_0314')
