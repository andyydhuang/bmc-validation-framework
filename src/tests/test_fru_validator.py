"""
src/tests/test_fru_validator.py

Unit tests for src/protocol/fru_validator.py

Tests verify:
    1. FruIpmiVerifier.verify()     — IPMI path field verification
    2. FruIpmiVerifier.verify_nic() — NIC manufacturer verification
    3. verify_io_nic_fru()          — IOBU/IOBD loop with bug fix verified
    4. FruCheckResult               — accumulator correctness
    5. FruDevice dataclass          — device mapping correctness

All tests use MockBmcClient — no hardware required.

The most important test in this file:
    test_iobd_loop_uses_iobd_prefix_not_iobu_prefix()
    This directly verifies the bug fix: the second loop in
    verify_io_nic_fru() checks IOBD presence, not IOBU presence.

Run with:
    python -m pytest src/tests/test_fru_validator.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))

import pytest
from mock.mock_bmc import MockBmcClient
from src.protocol.fru_validator import (
    FruDevice,
    FruCheckResult,
    FruIpmiVerifier,
    verify_io_nic_fru,
    FRU_DEV_IDS_IOBU,
    FRU_DEV_IDS_IOBD,
    VALID_NIC_MANUFACTURERS,
)


# ---------------------------------------------------------------------------
# FruDevice dataclass tests
# ---------------------------------------------------------------------------

class TestFruDevice:
    """Tests for the FruDevice mapping dataclass."""

    def test_fru_device_creation(self):
        device = FruDevice(
            device_id        = 0,
            expected_product = 'Generic-Server-MB',
            expected_chassis = 'Rack Mount Chassis',
            web_device_name  = 'MB_FRU',
        )
        assert device.device_id        == 0
        assert device.expected_product == 'Generic-Server-MB'
        assert device.expected_chassis == 'Rack Mount Chassis'
        assert device.web_device_name  == 'MB_FRU'

    def test_fru_device_frozen(self):
        """FruDevice is immutable — field modification must raise."""
        device = FruDevice(0, 'Product', 'Chassis')
        with pytest.raises(Exception):
            device.device_id = 99

    def test_web_device_name_optional(self):
        """web_device_name has a default empty string value."""
        device = FruDevice(0, 'Product', 'Chassis')
        assert device.web_device_name == ''


# ---------------------------------------------------------------------------
# FruCheckResult tests
# ---------------------------------------------------------------------------

class TestFruCheckResult:
    """Tests for the FRU-specific check result accumulator."""

    def test_initial_state(self):
        result = FruCheckResult(board='MB')
        assert result.board       == 'MB'
        assert result.passed      == 0
        assert result.failures    == []
        assert result.result_code == 0

    def test_ok_increments_passed(self):
        result = FruCheckResult(board='MB')
        result.ok('Chassis Type found')
        assert result.passed      == 1
        assert result.result_code == 0

    def test_fail_adds_failure(self):
        result = FruCheckResult(board='MB')
        result.fail('Board Product mismatch')
        assert len(result.failures) == 1
        assert result.result_code   == -1

    def test_multiple_failures(self):
        result = FruCheckResult(board='FP')
        result.fail('failure 1')
        result.fail('failure 2')
        assert result.result_code == -2

    def test_summary_contains_board_name(self):
        result = FruCheckResult(board='UBB')
        result.ok('field verified')
        assert 'UBB' in result.summary()

    def test_summary_contains_failure_messages(self):
        result = FruCheckResult(board='MB')
        result.fail('specific failure message')
        assert 'specific failure message' in result.summary()


# ---------------------------------------------------------------------------
# FruIpmiVerifier.verify() tests
# ---------------------------------------------------------------------------

class TestFruIpmiVerifierVerify:
    """
    Tests for FRU field verification via IPMI fru print command.
    MockBmcClient provides canned fru print output.
    """

    def setup_method(self):
        self.mock   = MockBmcClient()
        self.verify = FruIpmiVerifier(self.mock)

    def test_motherboard_fru_passes_when_fields_match(self):
        """
        MB FRU (device ID 0) with correct product and chassis type.
        Mock returns: Board Product='Generic-Server-MB',
                      Chassis Type='Rack Mount Chassis'
        """
        device = FruDevice(
            device_id        = 0,
            expected_product = 'Generic-Server-MB',
            expected_chassis = 'Rack Mount Chassis',
        )
        result = self.verify.verify(device, board_label='MB')
        assert result.result_code == 0, result.summary()
        assert result.passed == 2  # chassis type + board product

    def test_front_panel_fru_passes_when_fields_match(self):
        """
        FP FRU (device ID 1) with correct fields from mock.
        """
        device = FruDevice(
            device_id        = 1,
            expected_product = 'Generic-Server-FP',
            expected_chassis = 'Rack Mount Chassis',
        )
        result = self.verify.verify(device, board_label='FP')
        assert result.result_code == 0, result.summary()

    def test_wrong_product_name_fails(self):
        """
        Expected product name 'WRONG-MB' does not match mock output
        'Generic-Server-MB'. Should record one failure.
        """
        device = FruDevice(
            device_id        = 0,
            expected_product = 'WRONG-MB',
            expected_chassis = 'Rack Mount Chassis',
        )
        result = self.verify.verify(device, board_label='MB')
        assert result.result_code < 0
        assert any('Board Product' in f for f in result.failures)

    def test_wrong_chassis_type_fails(self):
        """
        Expected chassis type does not match mock output.
        """
        device = FruDevice(
            device_id        = 0,
            expected_product = 'Generic-Server-MB',
            expected_chassis = 'Tower Chassis',   # wrong
        )
        result = self.verify.verify(device, board_label='MB')
        assert result.result_code < 0
        assert any('Chassis Type' in f for f in result.failures)

    def test_both_fields_wrong_produces_two_failures(self):
        """Two wrong fields produce two separate failure records."""
        device = FruDevice(
            device_id        = 0,
            expected_product = 'WRONG-PRODUCT',
            expected_chassis = 'WRONG-CHASSIS',
        )
        result = self.verify.verify(device, board_label='MB')
        assert result.result_code == -2

    def test_empty_fru_output_fails_with_helpful_message(self):
        """
        Device ID with no FRU data (empty mock response) produces
        a failure with a helpful diagnostic message.
        """
        device = FruDevice(
            device_id        = 0x99,  # not in mock — returns empty
            expected_product = 'Any-Product',
            expected_chassis = 'Any-Chassis',
        )
        result = self.verify.verify(device, board_label='MISSING')
        assert result.result_code < 0
        assert any('empty' in f.lower() or 'blank' in f.lower()
                   for f in result.failures)

    def test_ubb_fru_device_id_hex(self):
        """UBB FRU uses device ID 0x22 (34 decimal)."""
        device = FruDevice(
            device_id        = 0x22,
            expected_product = 'Generic-Server-UBB',
            expected_chassis = 'Rack Mount Chassis',
        )
        result = self.verify.verify(device, board_label='UBB')
        assert result.result_code == 0, result.summary()

    def test_board_label_appears_in_log_output(self):
        """
        board_label parameter is stored in FruCheckResult.board
        so diagnostic messages can identify which board failed.
        """
        device = FruDevice(0, 'Generic-Server-MB', 'Rack Mount Chassis')
        result = self.verify.verify(device, board_label='TESTBOARD')
        assert result.board == 'TESTBOARD'


# ---------------------------------------------------------------------------
# FruIpmiVerifier.verify_nic() tests
# ---------------------------------------------------------------------------

class TestFruIpmiVerifierVerifyNic:
    """
    Tests for NIC card FRU verification.
    NIC tests check manufacturer name rather than product name,
    because multiple NIC vendors may be installed in OCP slots.
    """

    def setup_method(self):
        self.mock   = MockBmcClient()
        self.verify = FruIpmiVerifier(self.mock)

    def test_intel_nic_passes(self):
        """
        Mock NIC FRU (device 0x0C) contains 'Intel' in Board Mfg
        and Board Product fields.
        """
        result = self.verify.verify_nic(
            device_id           = 0x0C,
            valid_manufacturers = {'Intel', 'Mellanox'},
            board_label         = 'IOBU_OCP1',
        )
        assert result.result_code == 0, result.summary()

    def test_mellanox_nic_would_also_pass(self):
        """
        Mellanox is in valid_manufacturers — any response containing
        'Mellanox' in Board Mfg or Board Product should pass.
        We configure the mock to return Mellanox output for this test.
        """
        mock = MockBmcClient()
        mock._fru_outputs['0xd'] = (
            "Board Mfg      : Mellanox\n"
            "Board Product  : Mellanox ConnectX NIC\n"
        )
        verify = FruIpmiVerifier(mock)
        result = verify.verify_nic(
            device_id           = 0x0D,
            valid_manufacturers = {'Intel', 'Mellanox'},
            board_label         = 'IOBU_OCP2',
        )
        assert result.result_code == 0, result.summary()

    def test_unknown_manufacturer_fails(self):
        """
        A manufacturer not in valid_manufacturers should fail.
        This catches wrong NIC types installed in OCP slots.
        """
        mock = MockBmcClient()
        mock._fru_outputs['0xd'] = (
            "Board Mfg      : UnknownVendor\n"
            "Board Product  : UnknownVendor NIC\n"
        )
        verify = FruIpmiVerifier(mock)
        result = verify.verify_nic(
            device_id           = 0x0D,
            valid_manufacturers = {'Intel', 'Mellanox'},
            board_label         = 'IOBU_OCP2',
        )
        assert result.result_code < 0

    def test_valid_manufacturers_is_extensible(self):
        """
        valid_manufacturers is a set parameter — callers can add
        new vendors without modifying the verifier code.
        """
        mock = MockBmcClient()
        mock._fru_outputs['0xd'] = (
            "Board Mfg      : NewVendor\n"
            "Board Product  : NewVendor 100G NIC\n"
        )
        verify  = FruIpmiVerifier(mock)

        # fails with default manufacturers
        result1 = verify.verify_nic(
            0x0D, {'Intel', 'Mellanox'}, 'slot'
        )
        assert result1.result_code < 0

        # passes when NewVendor is added to the set
        result2 = verify.verify_nic(
            0x0D, {'Intel', 'Mellanox', 'NewVendor'}, 'slot'
        )
        assert result2.result_code == 0, result2.summary()


# ---------------------------------------------------------------------------
# FRU device ID lookup table tests
# ---------------------------------------------------------------------------

class TestFruDeviceIdTables:
    """
    Tests for IOBU and IOBD FRU device ID lookup tables.
    These tables map slot number → FRU device ID.
    """

    def test_iobu_table_has_six_entries(self):
        """6 entries: index 0 (unused sentinel) + slots 1-5."""
        assert len(FRU_DEV_IDS_IOBU) == 6

    def test_iobu_index_zero_is_sentinel(self):
        """Index 0 is a dummy value — slots are 1-based."""
        assert FRU_DEV_IDS_IOBU[0] == -1

    def test_iobu_slot_1_device_id(self):
        assert FRU_DEV_IDS_IOBU[1] == 0x0C

    def test_iobu_slot_4_and_5_out_of_order(self):
        """
        Slot 4 and 5 are intentionally non-sequential.
        This matches the BMC's internal FRU repository assignment
        which does not follow a simple sequential pattern.
        Verify the actual values are correct per platform spec.
        """
        assert FRU_DEV_IDS_IOBU[4] == 0x10
        assert FRU_DEV_IDS_IOBU[5] == 0x0F
        # note: 0x10 (16) before 0x0F (15) — non-sequential by design

    def test_iobd_table_has_six_entries(self):
        assert len(FRU_DEV_IDS_IOBD) == 6

    def test_iobd_index_zero_is_sentinel(self):
        assert FRU_DEV_IDS_IOBD[0] == -1

    def test_iobd_slot_1_device_id(self):
        assert FRU_DEV_IDS_IOBD[1] == 0x15

    def test_iobu_and_iobd_device_ids_do_not_overlap(self):
        """
        IOBU and IOBD FRU device IDs must be completely distinct.
        Overlapping IDs would mean reading the wrong FRU EEPROM.
        """
        iobu_ids = set(FRU_DEV_IDS_IOBU[1:])  # skip sentinel
        iobd_ids = set(FRU_DEV_IDS_IOBD[1:])
        assert iobu_ids.isdisjoint(iobd_ids), (
            f'IOBU and IOBD FRU device IDs overlap: '
            f'{iobu_ids & iobd_ids}'
        )


# ---------------------------------------------------------------------------
# verify_io_nic_fru() — bug fix verification
# ---------------------------------------------------------------------------

class TestVerifyIoNicFru:
    """
    Tests for the IO NIC FRU verification function.

    The critical test here verifies the prefix fix:
    Each loop checks its own prefix:
        IOBU loop uses iobu_prefix → checks sys_conf[IOBU_OCP{slot}]
        IOBD loop uses iobd_prefix → checks sys_conf[IOBD_OCP{slot}]

    Mixing them causes two failure modes:
        IOBU prefix on IOBD loop, IOBU present, IOBD empty:
            reads empty IOBD slot → no FRU data → FAIL
        IOBU prefix on IOBD loop, IOBU empty, IOBD present:
            skips IOBD slot entirely → defect undetected → PASS
    """

    def setup_method(self):
        """Configure mock with NIC FRU data for all device IDs."""
        self.mock = MockBmcClient()
        # Add NIC FRU responses for all IOBU and IOBD device IDs
        for dev_id in FRU_DEV_IDS_IOBU[1:] + FRU_DEV_IDS_IOBD[1:]:
            self.mock._fru_outputs[hex(dev_id)] = (
                "Board Mfg      : Intel\n"
                "Board Product  : Intel Generic NIC\n"
            )

    def test_only_iobu_populated_reads_only_iobu_slots(self):
        """
        IOBU slot 1 present, all IOBD slots empty.
        Should read IOBU FRU and pass, without touching IOBD slots.
        """
        sys_conf = {
            'IOBU_OCP1': 1, 'IOBU_OCP2': 0, 'IOBU_OCP3': 0,
            'IOBU_OCP4': 0, 'IOBU_OCP5': 0,
            'IOBD_OCP1': 0, 'IOBD_OCP2': 0, 'IOBD_OCP3': 0,
            'IOBD_OCP4': 0, 'IOBD_OCP5': 0,
        }
        result = verify_io_nic_fru(self.mock, sys_conf)
        assert result.result_code == 0, result.summary()

    def test_only_iobd_populated_reads_only_iobd_slots(self):
        """
        All IOBU slots empty, IOBD slot 1 present.
        Should read IOBD FRU and pass.

        With incorrect prefix, IOBD loop reads IOBU_OCP1=0 → skips IOBD
        even though IOBD slot 1 has a NIC and should be verified.
        Correct prefix reads IOBD_OCP1=1 → reads and verifies IOBD FRU.
        """
        sys_conf = {
            'IOBU_OCP1': 0, 'IOBU_OCP2': 0, 'IOBU_OCP3': 0,
            'IOBU_OCP4': 0, 'IOBU_OCP5': 0,
            'IOBD_OCP1': 1, 'IOBD_OCP2': 0, 'IOBD_OCP3': 0,
            'IOBD_OCP4': 0, 'IOBD_OCP5': 0,
        }
        result = verify_io_nic_fru(self.mock, sys_conf)
        assert result.result_code == 0, result.summary()
        assert result.passed > 0  # at least one IOBD slot was verified

    def test_iobd_loop_uses_iobd_prefix_not_iobu_prefix(self):
        """
        Scenario: IOBU_OCP1=1 (NIC present), IOBD_OCP1=0 (empty slot).

        IOBD loop must use iobd_prefix not iobu_prefix.
        If it used iobu_prefix it would read IOBU_OCP1=1 → try to read
        the empty IOBD slot → get no FRU data → incorrect FAIL.

        With iobd_prefix it reads IOBD_OCP1=0 → skips empty slot → PASS.
        """
        sys_conf = {
            'IOBU_OCP1': 1,   # IOBU has a NIC
            'IOBU_OCP2': 0,
            'IOBU_OCP3': 0,
            'IOBU_OCP4': 0,
            'IOBU_OCP5': 0,
            'IOBD_OCP1': 0,   # IOBD is empty
            'IOBD_OCP2': 0,
            'IOBD_OCP3': 0,
            'IOBD_OCP4': 0,
            'IOBD_OCP5': 0,
        }

        # Make IOBD FRU device return empty (simulates empty slot)
        iobd_dev_id = FRU_DEV_IDS_IOBD[1]  # 0x15
        self.mock._fru_outputs[hex(iobd_dev_id)] = ''

        result = verify_io_nic_fru(self.mock, sys_conf)

        assert result.result_code == 0, (
            f'BUG DETECTED: IOBD loop read empty IOBD slot '
            f'using IOBU presence flag.\n{result.summary()}'
        )

    def test_iobd_present_but_iobu_empty_is_not_skipped(self):
        """
        Converse of the bug scenario:
            IOBU_OCP1 = 0 (IOBU empty)
            IOBD_OCP1 = 1 (IOBD has NIC)

        Scenario: IOBU_OCP1=0 (empty), IOBD_OCP1=1 (NIC present).

        IOBD loop with iobd_prefix reads IOBD_OCP1=1 → verifies IOBD FRU.
        If it used iobu_prefix it would read IOBU_OCP1=0 → skip IOBD
        entirely → defective NIC goes undetected.
        """
        sys_conf = {
            'IOBU_OCP1': 0,   # IOBU is empty
            'IOBU_OCP2': 0,
            'IOBU_OCP3': 0,
            'IOBU_OCP4': 0,
            'IOBU_OCP5': 0,
            'IOBD_OCP1': 1,   # IOBD has a NIC — must be verified
            'IOBD_OCP2': 0,
            'IOBD_OCP3': 0,
            'IOBD_OCP4': 0,
            'IOBD_OCP5': 0,
        }

        # Track which device IDs were actually queried
        queried_ids = []
        original_run = self.mock.run

        def tracking_run(*args):
            cmd = ' '.join(str(a) for a in args)
            if 'fru print' in cmd:
                queried_ids.append(args[-1])
            return original_run(*args)

        self.mock.run = tracking_run

        result = verify_io_nic_fru(self.mock, sys_conf)

        # Verify that IOBD slot 1 FRU device was actually queried
        iobd_dev_1 = hex(FRU_DEV_IDS_IOBD[1])  # '0x15'
        assert iobd_dev_1 in queried_ids, (
            f'BUG DETECTED: IOBD slot 1 FRU device {iobd_dev_1} '
            f'was never queried. IOBD verification was incorrectly skipped.\n'
            f'Queried IDs: {queried_ids}'
        )

    def test_all_slots_empty_passes_with_no_reads(self):
        """No NICs installed anywhere — nothing should be read, nothing fails."""
        sys_conf = {
            f'IOBU_OCP{i}': 0 for i in range(1, 6)
        }
        sys_conf.update({
            f'IOBD_OCP{i}': 0 for i in range(1, 6)
        })

        result = verify_io_nic_fru(self.mock, sys_conf)
        assert result.result_code == 0, result.summary()
        assert result.passed == 0  # nothing verified, nothing failed

    def test_all_slots_populated_all_pass(self):
        """All 10 NIC slots installed with valid Intel NICs — all pass."""
        sys_conf = {
            f'IOBU_OCP{i}': 1 for i in range(1, 6)
        }
        sys_conf.update({
            f'IOBD_OCP{i}': 1 for i in range(1, 6)
        })

        result = verify_io_nic_fru(self.mock, sys_conf)
        assert result.result_code == 0, result.summary()
        # 10 slots × 2 fields (Board Mfg + Board Product) = 20 passed
        assert result.passed == 20

    def test_custom_prefix_parameters_work(self):
        """
        iobu_prefix and iobd_prefix are injectable parameters.
        This allows the function to be used on different platforms
        that use different sys_conf key naming conventions.
        """
        sys_conf = {
            'UPPER_NIC1': 1, 'UPPER_NIC2': 0,
            'LOWER_NIC1': 0, 'LOWER_NIC2': 0,
        }

        # Should not raise even with unusual prefix — just skip
        # slots that are not in sys_conf (returns 0 from .get())
        result = verify_io_nic_fru(
            self.mock, sys_conf,
            iobu_prefix='UPPER_NIC',
            iobd_prefix='LOWER_NIC',
        )
        # UPPER_NIC1=1 → reads IOBU slot 1 FRU → passes
        # others are 0 or missing → skipped
        assert result.result_code == 0, result.summary()


# ---------------------------------------------------------------------------
# Valid NIC manufacturers set tests
# ---------------------------------------------------------------------------

class TestValidNicManufacturers:
    """Tests for the default NIC manufacturer validation set."""

    def test_intel_in_valid_manufacturers(self):
        assert 'Intel' in VALID_NIC_MANUFACTURERS

    def test_mellanox_in_valid_manufacturers(self):
        assert 'Mellanox' in VALID_NIC_MANUFACTURERS

    def test_valid_manufacturers_is_a_set(self):
        assert isinstance(VALID_NIC_MANUFACTURERS, set)

    def test_valid_manufacturers_is_not_empty(self):
        assert len(VALID_NIC_MANUFACTURERS) > 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    pytest.main([__file__, '-v'])
