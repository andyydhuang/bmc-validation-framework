"""
src/tests/test_bmc_fw_update.py

Hardware integration tests for firmware update validation.

TC IDs covered:
    TC_BMC_0_0300 — BIOS firmware update (OOB via FwFlashTool)
    TC_BMC_0_0301 — BMC firmware update (dual-image, all scenarios)
    TC_BMC_0_0302 — CPLD cfm0 sector update (all 8 CPLDs)
    TC_BMC_0_0303 — CPLD cfm1 baseline then cfm0 fallback test
    TC_BMC_0_0310 — AMC firmware update via PLDM

WARNING — ALL TESTS IN THIS FILE ARE PERMANENTLY DESTRUCTIVE:
    They overwrite flash chips. A crash mid-flash may brick
    the target device and require physical recovery.

    DO NOT run these tests during normal regression.
    Run them only as part of a dedicated firmware validation session
    with full knowledge of the recovery procedure for your platform.

    The test runner enforces this via:
        1. Requires --integration flag (no accidental execution)
        2. Requires --fw-config flag pointing to firmware image paths
        3. Each TC requires explicit authorization in test_configs.json
        4. Host power-off is enforced before BIOS flash
        5. BMC online polling is enforced after BMC flash

Prerequisites:
    - firmware flash tool binary (download from platform firmware release package)
    - Firmware image files (encrypted .bin, .img, .jed, .pldm)
    - config/fw_update_configs.json (copy from fw_update_configs_template.json)
    - ipmitool on PATH

Run with:
    pytest src/tests/test_bmc_fw_update.py --integration \\
           --bmc-ip 192.168.1.100 \\
           --bmc-user admin \\
           --bmc-password yourpassword \\
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
    erase_cpld_cfm0, get_amc_eids,
    FwFlashToolError,
)
from src.tests.conftest import is_authorized


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Additional CLI options for firmware update tests
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    try:
        parser.addoption(
            '--fw-config',
            default = 'config/fw_update_configs.json',
            help    = 'Path to fw_update_configs.json image paths file '
                      '(copy from config/fw_update_configs_template.json)',
        )
        parser.addoption(
            '--flash-tool-binary',
            default = './FwFlashTool',
            help    = 'Path to firmware flash tool binary (default: ./FwFlashTool)',
        )
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Firmware config fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def fw_config(request):
    """
    Load firmware image configuration from fw_update_configs.json.
    Skips if file not found.
    """
    path = request.config.getoption('--fw-config')
    if not os.path.exists(path):
        pytest.skip(
            f'Firmware config not found at {path}. '
            f'Copy config/fw_update_configs_template.json to {path} '
            f'and fill in your image paths and versions.'
        )
    with open(path) as f:
        return json.load(f)


@pytest.fixture(scope='session')
def flash_client(request, bmc_credentials):
    """FwUpdateClient using firmware flash tool binary."""
    binary = request.config.getoption('--flash-tool-binary')
    if not os.path.exists(binary):
        pytest.skip(
            f'firmware flash tool binary not found at {binary}. '
            f'Pass --flash-tool-binary <path> to specify its location.'
        )
    return FwUpdateClient(
        bmc_ip       = bmc_credentials['ip'],
        bmc_user     = bmc_credentials['user'],
        bmc_password = bmc_credentials['password'],
        flash_tool_binary  = binary,
        timeout      = 600,
    )


@pytest.fixture(scope='session')
def fw_verifier(ipmi_client):
    """FwVersionVerifier using the shared IpmiClient."""
    return FwVersionVerifier(ipmi_client, max_retries=6, retry_interval=10)


# ---------------------------------------------------------------------------
# Helper: re-authenticate after no-preserve-config flash
# ---------------------------------------------------------------------------

def _reauth_after_flash(ipmi_client, bmc_credentials):
    """
    Re-apply BMC password after a no-preserve-config flash.

    Some BMC firmware versions reset all credentials to factory
    defaults after a no-preserve-config flash. This helper attempts
    to change the password back to the test session password.
    Uses a platform-specific command — may be a no-op on some platforms.
    """
    try:
        ipmi_client.run(
            'raw', '0x32', '0x66',
            '0x00',  # user ID 0 = admin
            *[hex(ord(c)) for c in bmc_credentials['password'][:16]],
        )
        logger.debug('BMC password re-applied after flash')
    except Exception as exc:
        logger.debug('Password re-auth skipped (may not be needed): %s', exc)


# ---------------------------------------------------------------------------
# TC_BMC_0_0300 — BIOS firmware update
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0300_bios_firmware_update(
        flash_client, fw_verifier, ipmi_client,
        fw_config, test_config):
    """
    TC_BMC_0_0300 — BIOS firmware update via FwFlashTool OOB.

    Tests both upgrade and downgrade paths:
        Round 0: flash image[0] (older version), verify
        Round 1: flash image[1] (newer version), verify

    Safety: host is powered OFF before each flash and powered ON
    after to verify the new BIOS boots correctly.

    Post-flash verification:
        BIOS version is read via IPMI Get System Info Parameters
        (NetFn=0x06 Cmd=0x59, parameter 0x01 = firmware string).
        The version string is ASCII-encoded in the response bytes.
    """
    is_authorized('TC_BMC_0_0300', test_config)

    bios_images = fw_config.get('bios_images', [])
    if len(bios_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain at least 2 bios_images '
            '(index 0 = older, index 1 = newer) to test upgrade and downgrade.'
        )

    for j, img_conf in enumerate(bios_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BIOS image {j}'),
        )

        # ── power off host before BIOS flash ──────────────────────────
        try:
            if ipmi_client.get_power_state():
                ipmi_client.set_power('off')
                time.sleep(10)
        except Exception as exc:
            pytest.fail(
                f'Cannot power off host before BIOS flash: {exc}. '
                f'BIOS flash with host powered on risks flash corruption.'
            )

        # ── flash BIOS ────────────────────────────────────────────────
        try:
            flash_client.flash_bios(image)
        except FwFlashToolError as exc:
            pytest.fail(
                f'BIOS flash failed for {image.label}:\n{exc}'
            )

        time.sleep(10)

        # ── power on and wait for POST ─────────────────────────────────
        ipmi_client.set_power('on')
        time.sleep(30)  # allow BIOS POST to begin

        # ── verify BIOS version ───────────────────────────────────────
        verified = fw_verifier.verify_bios(image.version)
        assert verified, (
            f'TC_BMC_0_0300 FAILED (image {j} — {image.label}): '
            f'BIOS version {image.version} not confirmed after flash. '
            f'Check that the image was correctly written and that '
            f'BIOS POST completed successfully.'
        )


# ---------------------------------------------------------------------------
# TC_BMC_0_0301 — BMC firmware update (dual-image, all scenarios)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0301_bmc_firmware_update(
        flash_client, fw_verifier, ipmi_client, bmc_credentials,
        fw_config, test_config):
    """
    TC_BMC_0_0301 — BMC firmware update covering all dual-image scenarios.

    Four update scenarios tested in sequence:

    Scenario 1: Image slot 1, no config preservation
        Flash old version, verify slot 1 is active, verify version.

    Scenario 2: Image slot 1, with config preservation
        Flash new version preserving NVRAM settings.
        Verifies config-preserved flash path (IP, users retained).

    Scenario 3: Image slot 2, no config preservation
        Flash to slot 2, verify slot 2 becomes active.

    Scenario 4: Both slots simultaneously
        Flash same version to both slots.
        Then toggle active slot and verify both slots boot correctly.

    Each scenario:
        - Powers off host (reduces I/O during BMC reboot)
        - Flashes via FwFlashTool
        - Waits for BMC to reboot and come back online (up to 180s)
        - Re-authenticates if needed (no-preserve-config resets creds)
        - Verifies active slot via platform-specific command
        - Verifies firmware version via mc info
    """
    is_authorized('TC_BMC_0_0301', test_config)

    bmc_images = fw_config.get('bmc_images', [])
    if len(bmc_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain at least 2 bmc_images.'
        )

    def _flash_and_verify(image, slot, preserve_config, scenario_label):
        """Flash one BMC image and verify. Used by all four scenarios."""
        # power off host to reduce I/O during BMC reboot
        try:
            if ipmi_client.get_power_state():
                ipmi_client.set_power('off')
                time.sleep(5)
        except Exception:
            pass  # host power state is not critical for BMC flash

        try:
            flash_client.flash_bmc(image, slot, preserve_config)
        except FwFlashToolError as exc:
            pytest.fail(
                f'BMC flash failed ({scenario_label}):\n{exc}'
            )

        # wait for BMC to come back online after self-reboot
        online = wait_for_bmc_online(ipmi_client, max_wait=180)
        if not online:
            pytest.fail(
                f'BMC did not come back online within 180s after '
                f'flash ({scenario_label}).'
            )

        # re-authenticate if no-preserve-config
        if not preserve_config:
            _reauth_after_flash(ipmi_client, bmc_credentials)

        # verify active slot
        if slot != BmcImageSlot.BOTH:
            slot_ok = fw_verifier.verify_bmc_active_slot(slot)
            assert slot_ok, (
                f'BMC active slot wrong after {scenario_label}. '
                f'Expected {slot.name}.'
            )

        # verify version
        version_ok = fw_verifier.verify_bmc(image.version)
        assert version_ok, (
            f'BMC version {image.version} not confirmed '
            f'after {scenario_label}.'
        )

    # ── Scenario 1: slot 1, no preserve ───────────────────────────────
    for j, img_conf in enumerate(bmc_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BMC image {j}'),
        )
        fw_verifier.set_bmc_boot_slot(BmcImageSlot.SLOT_1)
        _flash_and_verify(
            image, BmcImageSlot.SLOT_1,
            preserve_config = False,
            scenario_label  = f'Scenario 1 image[{j}] (slot1, no-preserve)',
        )

    # ── Scenario 2: slot 1, preserve config ───────────────────────────
    for j, img_conf in enumerate(bmc_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BMC image {j}'),
        )
        fw_verifier.set_bmc_boot_slot(BmcImageSlot.SLOT_1)
        _flash_and_verify(
            image, BmcImageSlot.SLOT_1,
            preserve_config = True,
            scenario_label  = f'Scenario 2 image[{j}] (slot1, preserve)',
        )

    # ── Scenario 3: slot 2, no preserve ───────────────────────────────
    for j, img_conf in enumerate(bmc_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BMC image {j}'),
        )
        fw_verifier.set_bmc_boot_slot(BmcImageSlot.SLOT_2)
        _flash_and_verify(
            image, BmcImageSlot.SLOT_2,
            preserve_config = False,
            scenario_label  = f'Scenario 3 image[{j}] (slot2, no-preserve)',
        )
        _reauth_after_flash(ipmi_client, bmc_credentials)

    # ── Scenario 4: both slots, then toggle ───────────────────────────
    for j, img_conf in enumerate(bmc_images):
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = img_conf.get('label', f'BMC image {j}'),
        )
        _flash_and_verify(
            image, BmcImageSlot.BOTH,
            preserve_config = False,
            scenario_label  = f'Scenario 4 image[{j}] (both slots)',
        )
        _reauth_after_flash(ipmi_client, bmc_credentials)

        # read current active slot and toggle to the OTHER slot
        active_output = ipmi_client.run('raw', '0x32', '0x8F', '0x07')
        active_token  = active_output.strip().split()[0].lower() \
                        if active_output.strip() else '01'

        other_slot = (BmcImageSlot.SLOT_2
                      if active_token == '01'
                      else BmcImageSlot.SLOT_1)

        fw_verifier.set_bmc_boot_slot(other_slot)
        ipmi_client.run('mc', 'reset', 'cold')
        time.sleep(120)
        wait_for_bmc_online(ipmi_client, max_wait=60)

        # both slots have same version — just confirm BMC is alive
        version_ok = fw_verifier.verify_bmc(image.version)
        assert version_ok, (
            f'BMC version {image.version} not confirmed after '
            f'slot toggle to {other_slot.name} (Scenario 4 image[{j}])'
        )


# ---------------------------------------------------------------------------
# TC_BMC_0_0302 — CPLD cfm0 update for all 8 CPLDs
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0302_cpld_cfm0_update(
        flash_client, fw_verifier, ipmi_client,
        fw_config, test_config):
    """
    TC_BMC_0_0302 — Flash cfm0 sector for all 8 CPLD devices.

    Flashes each CPLD in test_cpld_list with:
        Round 0: older version (cfm0 index 0)
        Round 1: newer version (cfm0 index 1)

    After each round: virtual reseat triggers all CPLDs to reload
    from their newly-written cfm0 sector.

    Host must be powered off before CPLD flash — CPLDs control
    power sequencing, fan PWM, and GPIO routing. Updating them
    while the host is powered on risks:
        - PWM glitches causing fan speed spikes
        - Spurious GPIO state changes triggering alerts
        - Power sequencing errors

    CPLD version verification:
        Uses OEM Get Component FW Version (NetFn=0x38 Cmd=0xAB)
        with board_type parameter to select which CPLD to query.
        Response is 2-byte little-endian version [minor, major].
    """
    is_authorized('TC_BMC_0_0302', test_config)

    cpld_conf = fw_config.get('cpld_images', {}).get('cfm0', {})
    if not cpld_conf:
        pytest.skip(
            'fw_update_configs.json has no cpld_images.cfm0 entries. '
            'Populate the CPLD cfm0 image paths to run this test.'
        )

    test_cpld_list = list(CPLD_BOARD_TYPES.keys())

    for j in range(2):  # round 0 = older, round 1 = newer
        # ── power off host ─────────────────────────────────────────────
        try:
            if ipmi_client.get_power_state():
                ipmi_client.set_power('off')
                time.sleep(10)
        except Exception as exc:
            pytest.fail(
                f'Cannot power off host before CPLD flash: {exc}'
            )

        # ── flash all CPLDs ────────────────────────────────────────────
        for cpld_name in test_cpld_list:
            entries = cpld_conf.get(cpld_name, [])
            if not entries or j >= len(entries):
                pytest.skip(
                    f'fw_update_configs.json missing cfm0 image[{j}] '
                    f'for CPLD {cpld_name}.'
                )

            img_conf = entries[j]
            image = FwImage(
                path    = img_conf['path'],
                version = img_conf['version'],
                label   = img_conf.get('label', f'{cpld_name} cfm0 [{j}]'),
            )

            try:
                flash_client.flash_cpld(image)
            except FwFlashToolError as exc:
                pytest.fail(
                    f'CPLD {cpld_name} cfm0 flash failed '
                    f'(image[{j}]):\n{exc}'
                )

        # ── virtual reseat — activate new cfm0 ────────────────────────
        virtual_reseat(ipmi_client)
        time.sleep(120)
        wait_for_bmc_online(ipmi_client, max_wait=60)

        # ── verify all CPLD versions ───────────────────────────────────
        for cpld_name in test_cpld_list:
            entries   = cpld_conf.get(cpld_name, [])
            img_conf  = entries[j]
            board_type = img_conf.get(
                'board_type', CPLD_BOARD_TYPES.get(cpld_name, '0x0')
            )

            verified = fw_verifier.verify_cpld(
                cpld_name        = cpld_name,
                expected_version = img_conf['version'],
                board_type       = board_type,
            )
            assert verified, (
                f'TC_BMC_0_0302 FAILED: CPLD {cpld_name} cfm0 version '
                f'{img_conf["version"]} not confirmed after flash '
                f'(image[{j}]).'
            )


# ---------------------------------------------------------------------------
# TC_BMC_0_0303 — CPLD cfm1 baseline then cfm0 fallback
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0303_cpld_cfm1_fallback(
        flash_client, fw_verifier, ipmi_client,
        fw_config, test_config):
    """
    TC_BMC_0_0303 — Verify CPLD dual-sector fallback recovery mechanism.

    Test sequence (per CPLD dual-sector architecture):

    Step 1: Flash cfm1 with OLD version (establish known baseline).
    Step 2: Erase cfm0 (simulate corrupt active sector).
    Step 3: Flash cfm0 with NEW version.
    Step 4: Virtual reseat → CPLD should boot from cfm1 (old/trusted).
    Step 5: Verify active version = OLD (cfm1 fallback worked).
    Step 6: Flash cfm0 with NEW version again (now valid).
    Step 7: Virtual reseat → CPLD should boot from cfm0 (new/preferred).
    Step 8: Verify active version = NEW (cfm0 is now preferred).

    This test verifies that a CPLD with a freshly-erased-then-reflashed
    cfm0 correctly falls back to cfm1 on the first reseat, confirming
    the CPLD's own integrity-checking logic is working. On the second
    reseat (after a clean cfm0 flash), cfm0 is preferred again.

    The cfm0 erase uses a platform-specific ERASEONLY command — destructive.
    """
    is_authorized('TC_BMC_0_0303', test_config)

    cfm0_conf = fw_config.get('cpld_images', {}).get('cfm0', {})
    cfm1_conf = fw_config.get('cpld_images', {}).get('cfm1', {})
    if not cfm0_conf or not cfm1_conf:
        pytest.skip(
            'fw_update_configs.json must have both cfm0 and cfm1 '
            'CPLD image entries for TC_BMC_0_0303.'
        )

    test_cpld_list = list(CPLD_BOARD_TYPES.keys())

    # power off host for CPLD operations
    try:
        if ipmi_client.get_power_state():
            ipmi_client.set_power('off')
            time.sleep(10)
    except Exception as exc:
        pytest.fail(f'Cannot power off host: {exc}')

    # ── Step 1: flash cfm1 with OLD version (index 0) ─────────────────
    for cpld_name in test_cpld_list:
        entries = cfm1_conf.get(cpld_name, [])
        if not entries:
            pytest.skip(f'No cfm1 images for CPLD {cpld_name}')
        img_conf = entries[0]  # older version
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = f'{cpld_name} cfm1 old',
        )
        try:
            flash_client.flash_cpld(image)
        except FwFlashToolError as exc:
            pytest.fail(f'Step 1 cfm1 flash failed for {cpld_name}:\n{exc}')

    # ── Step 2: erase cfm0 ────────────────────────────────────────────
    erase_cpld_cfm0(ipmi_client)

    # ── Step 3: flash cfm0 with NEW version (index 1) ─────────────────
    for cpld_name in test_cpld_list:
        entries = cfm0_conf.get(cpld_name, [])
        if len(entries) < 2:
            pytest.skip(f'Need 2 cfm0 images for {cpld_name}')
        img_conf = entries[1]  # newer version
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = f'{cpld_name} cfm0 new',
        )
        try:
            flash_client.flash_cpld(image)
        except FwFlashToolError as exc:
            pytest.fail(f'Step 3 cfm0 flash failed for {cpld_name}:\n{exc}')

    # ── Step 4: virtual reseat — CPLD should fall back to cfm1 ────────
    virtual_reseat(ipmi_client)
    time.sleep(120)
    wait_for_bmc_online(ipmi_client, max_wait=60)

    # ── Step 5: verify active version = OLD (cfm1 fallback) ───────────
    for cpld_name in test_cpld_list:
        old_version = cfm1_conf[cpld_name][0]['version']
        board_type  = cfm1_conf[cpld_name][0].get(
            'board_type', CPLD_BOARD_TYPES.get(cpld_name, '0x0')
        )
        verified = fw_verifier.verify_cpld(
            cpld_name        = cpld_name,
            expected_version = old_version,
            board_type       = board_type,
        )
        assert verified, (
            f'TC_BMC_0_0303 FAILED Step 5: {cpld_name} should be '
            f'running old cfm1 version {old_version} after fallback, '
            f'but version does not match. CPLD fallback mechanism may '
            f'not be working correctly.'
        )

    # ── Step 6: flash cfm0 with NEW version again ─────────────────────
    for cpld_name in test_cpld_list:
        entries  = cfm0_conf.get(cpld_name, [])
        img_conf = entries[1]
        image = FwImage(
            path    = img_conf['path'],
            version = img_conf['version'],
            label   = f'{cpld_name} cfm0 new (second flash)',
        )
        try:
            flash_client.flash_cpld(image)
        except FwFlashToolError as exc:
            pytest.fail(f'Step 6 cfm0 re-flash failed for {cpld_name}:\n{exc}')

    # ── Step 7: virtual reseat — CPLD should prefer cfm0 now ──────────
    virtual_reseat(ipmi_client)
    time.sleep(120)
    wait_for_bmc_online(ipmi_client, max_wait=60)

    # ── Step 8: verify active version = NEW (cfm0 preferred) ──────────
    for cpld_name in test_cpld_list:
        new_version = cfm0_conf[cpld_name][1]['version']
        board_type  = cfm0_conf[cpld_name][1].get(
            'board_type', CPLD_BOARD_TYPES.get(cpld_name, '0x0')
        )
        verified = fw_verifier.verify_cpld(
            cpld_name        = cpld_name,
            expected_version = new_version,
            board_type       = board_type,
        )
        assert verified, (
            f'TC_BMC_0_0303 FAILED Step 8: {cpld_name} should be '
            f'running new cfm0 version {new_version} after second flash, '
            f'but version does not match.'
        )


# ---------------------------------------------------------------------------
# TC_BMC_0_0310 — AMC firmware update (PLDM)
# ---------------------------------------------------------------------------

def test_tc_bmc_0_0310_amc_firmware_update(
        flash_client, fw_verifier, ipmi_client,
        fw_config, sys_conf, test_config):
    """
    TC_BMC_0_0310 — AMC (Accelerator Module Controller) firmware update
    via PLDM (Platform Level Data Model) using FwFlashTool.

    Key difference from other firmware updates:
        AMC EIDs (MCTP Endpoint IDs) are DYNAMIC — the BMC assigns
        them at boot time. This test discovers EIDs at runtime using
        the BMC's MCTP endpoint table rather than using fixed values.

    Test sequence:
        1. Discover AMC EIDs from BMC MCTP endpoint table
        2. For each present AMC slot:
           a. Flash older version image with discovered EID
           b. Flash newer version image with discovered EID
           (FwFlashTool receives EID via stdin pipe)

    Version verification:
        AMC version verification is platform-specific. This test
        checks that FwFlashTool reports success and does not validate
        the AMC version via a separate command, as AMC version
        readback mechanisms vary by platform.

    Requires:
        - AMC modules present per sys_conf.json (OAM0-OAM7)
        - AMC image files in fw_update_configs.json amc_images
        - Host powered on (AMC modules require main DC power)
    """
    is_authorized('TC_BMC_0_0310', test_config)

    any_amc = any(sys_conf.get(f'OAM{i}', 0) for i in range(8))
    if not any_amc:
        pytest.skip(
            'No AMC modules present per sys_conf.json. '
            'Set OAM0-OAM7 to 1 to enable AMC firmware update test.'
        )

    amc_images = fw_config.get('amc_images', [])
    if len(amc_images) < 2:
        pytest.skip(
            'fw_update_configs.json must contain at least 2 amc_images '
            '(index 0 = older, index 1 = newer).'
        )

    # ── Step 1: discover AMC EIDs at runtime ──────────────────────────
    eids = get_amc_eids(ipmi_client)

    if not eids:
        pytest.skip(
            'No AMC EIDs discovered from BMC MCTP endpoint table. '
            'Ensure host is powered on and AMC modules are initialized. '
            'AMC modules require main DC power and BMC initialization '
            'before EIDs are assigned.'
        )

    # ── Step 2: flash each present AMC slot ───────────────────────────
    for slot, eid in sorted(eids.items()):
        if not sys_conf.get(f'OAM{slot}', 0):
            continue  # EID exists but sys_conf says absent — skip

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
                    f'AMC flash failed for OAM{slot} '
                    f'(EID={eid}, image[{j}] {image.label}):\n{exc}'
                )

            # brief pause between flash operations on same slot
            time.sleep(5)
