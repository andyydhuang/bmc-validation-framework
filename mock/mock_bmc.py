"""
MockBmcClient — canned BMC responses for unit tests.

Same interface as IpmiClient: run() returns strings, run_raw() returns bytes.
Swap one for the other in test setup without touching test logic.

To add a new command: extend _dispatch() or override run() in a subclass.
"""

from typing import Optional


class MockBmcClient:
    """
    Simulates BMC IPMI command responses.

    Covers the commands exercised by the unit tests in src/tests/.
    Extend the _responses dict or override run() for additional commands.
    """

    def __init__(self, firmware_version: str = '2.10',
                 product_name: str = 'Generic-Server-MB'):
        self.firmware_version = firmware_version
        self.product_name     = product_name

        # canned SDR output — pipe-delimited, matches ipmitool sdr elist format
        self._sdr_output = (
            "CPU0             | A7h | ok  |  3.1 | Presence detected\n"
            "CPU1             | A8h | ok  |  3.2 | Presence detected\n"
            "Temp_CPU0        | 10h | ok  |  3.1 | 52 degrees C\n"
            "Temp_CPU0_VR     | 11h | ok  |  3.1 | 48 degrees C\n"
            "Temp_CPU1        | 12h | ns  |  3.2 | No Reading\n"
            "Vol_PVCCIN_CPU0  | 20h | ok  |  3.1 | 1.8 Volts\n"
            "Vol_PVCCIN_CPU1  | 21h | ns  |  3.2 | Disabled\n"
            "T_CPU_Highest    | 30h | ok  |  3.1 | 52 degrees C\n"
            "Power_CPU        | 31h | ok  |  3.1 | 150 Watts\n"
            "PSU0_Status      | C0h | ok  |  8.1 | Presence detected\n"
            "Fan_PSU0_0       | C1h | ok  |  8.1 | 3200 RPM\n"
            "PSU0_Current     | C2h | ok  |  8.1 | 12 Amps\n"
            "PSU0_Input       | C3h | ok  |  8.1 | 400 Watts\n"
            "Temp_PSU0_Inlet  | C4h | ok  |  8.1 | 30 degrees C\n"
            "Fan_SYS0_0       | 20h | ok  |  7.1 | 3600 RPM\n"
            "Fan_SYS0_1       | 21h | ok  |  7.1 | 3300 RPM\n"
            "Fan_SYS1_0       | 22h | ok  |  7.2 | 3600 RPM\n"
            "Fan_SYS1_1       | 23h | ok  |  7.2 | 3300 RPM\n"
            "NVMeSSD_0        | 30h | ns  |  4.1 | No Reading\n"
            "NVMeSSD_3        | 33h | ok  |  4.4 | Drive Present\n"
            "Temp_NVMeSSD3    | 34h | ok  |  4.4 | 38 degrees C\n"
            "DIMM_A0          | D0h | ok  | 12.1 | Presence detected\n"
            "Temp_DIMM_A0     | D1h | ok  | 12.1 | 35 degrees C\n"
            "DIMM_G0          | D8h | ns  | 12.7 | No Reading\n"
            "Temp_PESW0       | E0h | ok  |  5.1 | 45 degrees C\n"
            "T_PESW_Highest   | E4h | ok  |  5.1 | 45 degrees C\n"
            "Power_MB         | F0h | ok  |  7.1 | 350 Watts\n"
            "Temp_MB_OCP1     | F1h | ok  |  7.1 | 40 degrees C\n"
            "MB_OCP1_PRSNT    | F2h | ok  |  7.1 | Presence detected\n"
        )

        # canned FRU output — matches ipmitool fru print format
        self._fru_outputs = {
            '0':    (
                "Board Mfg Date        : Mon Jan  1 00:00:00 2024\n"
                "Board Mfg             : Generic Manufacturer\n"
                "Board Product         : Generic-Server-MB\n"
                "Board Serial          : SN0000000001\n"
                "Chassis Type          : Rack Mount Chassis\n"
            ),
            '1':    (
                "Board Mfg Date        : Mon Jan  1 00:00:00 2024\n"
                "Board Mfg             : Generic Manufacturer\n"
                "Board Product         : Generic-Server-FP\n"
                "Board Serial          : SN0000000002\n"
                "Chassis Type          : Rack Mount Chassis\n"
            ),
            '0x22': (
                "Board Mfg Date        : Mon Jan  1 00:00:00 2024\n"
                "Board Mfg             : Generic Manufacturer\n"
                "Board Product         : Generic-Server-UBB\n"
                "Board Serial          : SN0000000003\n"
                "Chassis Type          : Rack Mount Chassis\n"
            ),
            '0xc':  (
                "Board Mfg             : Intel\n"
                "Board Product         : Intel Generic NIC\n"
            ),
        }

        # canned SEL output for CATERR injection test
        self._sel_with_caterr = (
            "   1 | 01/01/2024 | 12:00:00 | Processor #0x00 | "
            "CATERR | Asserted\n"
        )
        self._sel_empty = ""
        self._sel_output = self._sel_empty

    # ------------------------------------------------------------------
    # Public interface — matches IpmiClient.run() signature
    # ------------------------------------------------------------------

    def run(self, *args, retries: int = 2,
            timeout: int = None) -> str:
        """
        Simulate an IPMI command and return canned output.

        Args:
            *args: command tokens e.g. 'mc', 'info' or 'sdr', 'elist', 'all'
        Returns:
            Simulated ipmitool stdout string
        """
        command = ' '.join(str(a) for a in args)
        return self._dispatch(command)

    def run_raw(self, netfn: int, cmd: int,
                *data: int, **kwargs) -> bytes:
        """
        Simulate a raw IPMI command and return response bytes.
        Returns minimal valid response for known commands.
        """
        # chassis power status — return 'on'
        if netfn == 0x00 and cmd == 0x01:
            return bytes([0x01])  # bit 0 set = power on

        # BIOS POST complete check (platform OEM)
        if netfn == 0x00 and cmd == 0x00:  # replace with platform OEM NetFn/Cmd for POST complete check
            return bytes([0x00])  # 0x00 = POST complete

        # JTAG IDCODE read (platform OEM) — generic 32-bit value
        if netfn == 0x00 and cmd == 0x00:  # replace with platform OEM NetFn/Cmd for JTAG IDCODE read
            # return ASCII hex representation of a generic IDCODE
            # matches the BMC OEM firmware convention documented in Phase 4
            idcode_str = '20044113'
            return bytes([ord(c) for c in idcode_str])

        # PECI bridge — ping response
        if netfn == 0x00 and cmd == 0x00:  # replace with platform OEM NetFn/Cmd for PECI ping
            return bytes([0x40])  # completion code 0x40 = pass

        # default: empty response
        return bytes()

    # ------------------------------------------------------------------
    # Test helpers — allow tests to configure mock state
    # ------------------------------------------------------------------

    def inject_caterr_sel_record(self):
        """Simulate BMC writing CATERR event to SEL after fault injection."""
        self._sel_output = self._sel_with_caterr

    def clear_sel(self):
        """Simulate SEL clear command."""
        self._sel_output = self._sel_empty

    def set_sdr_sensor(self, sensor_name: str, reading: str):
        """
        Override one SDR sensor reading for targeted test scenarios.

        Example:
            mock.set_sdr_sensor('Temp_CPU0', 'No Reading')
            # now verify_cpu_sdr will see CPU0 temp as unavailable
        """
        lines = self._sdr_output.splitlines()
        new_lines = []
        for line in lines:
            if line.startswith(sensor_name):
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 5:
                    parts[4] = reading
                    line = ' | '.join(parts)
            new_lines.append(line)
        self._sdr_output = '\n'.join(new_lines) + '\n'

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, command: str) -> str:
        if 'mc info' in command:
            return self._mc_info()

        if 'mc selftest' in command:
            return "Selftest: passed\n"

        if 'sdr elist' in command:
            return self._sdr_output

        if 'sel clear' in command:
            self.clear_sel()
            return ""

        if 'sel elist' in command or 'sel list' in command:
            return self._sel_output

        if 'fru print' in command:
            return self._fru_print(command)

        if 'power status' in command or 'chassis status' in command:
            return "Chassis Power is on\n"

        if 'chassis power on' in command:
            return ""

        if 'chassis power off' in command:
            return ""

        # IPMI raw — return empty (specific raw cmds handled by run_raw)
        if command.startswith('raw'):
            return ""

        return ""

    def _mc_info(self) -> str:
        return (
            "Device ID                 : 32\n"
            f"Firmware Revision         : {self.firmware_version}\n"
            "IPMI Version              : 2.0\n"
            "Manufacturer ID           : 343\n"
            "Manufacturer Name         : Generic BMC Vendor\n"
            f"Product ID                : 1000\n"
        )

    def _fru_print(self, command: str) -> str:
        """
        Extract device ID from command string and look up canned output.

        ipmitool is called with hex(device_id) which produces strings
        like '0x0', '0x1', '0x22', '0xc'. The mock dict uses both
        decimal strings ('0', '1') and hex strings ('0x22', '0xc').

        Normalization strategy:
            1. Try the raw token as-is (handles '0x22', '0xc')
            2. Try converting to int then to decimal string (handles '0x0'→'0')
            3. Try converting to int then to canonical hex (handles '0'→'0x0')
        """
        tokens    = command.split()
        raw_token = tokens[-1] if len(tokens) >= 3 else '0'

        # try raw token first (e.g. '0xc', '0x22')
        if raw_token in self._fru_outputs:
            return self._fru_outputs[raw_token]

        # normalize: convert hex string or decimal string to integer
        try:
            dev_int = int(raw_token, 16) if raw_token.startswith('0x') \
                      else int(raw_token)
        except ValueError:
            return ""

        # try decimal string key (e.g. '0', '1', '34')
        decimal_key = str(dev_int)
        if decimal_key in self._fru_outputs:
            return self._fru_outputs[decimal_key]

        # try canonical hex key (e.g. '0x0', '0x22')
        hex_key = hex(dev_int)
        if hex_key in self._fru_outputs:
            return self._fru_outputs[hex_key]

        return ""
