# Architecture and Protocol Reference

---

## Layer architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                       Test layer                                 │
│  pytest, conftest.py fixtures, test_configs.json authorization   │
│  --integration flag gates hardware tests                         │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                    Protocol / parse layer                        │
│                                                                  │
│  sdr_parser.py    — SdrEntry, parse_all(), verify_cpu_sdr()      │
│                     PresenceState enum, SdrCheckResult           │
│                                                                  │
│  sel_decoder.py   — SelDecoder, SelStandardRecord,               │
│                     SelOemPcieRecord, PCIE_AER_ERRORS            │
│                                                                  │
│  fru_validator.py — FruIpmiVerifier, FruWebUiVerifier,           │
│                     verify_io_nic_fru(), FruDevice               │
└───────────────────────────┬──────────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────────┐
│                     Transport layer                              │
│                                                                  │
│  ipmi_client.py   — IpmiClient, typed exceptions,                │
│                     run(), run_raw(), retry logic                │
│                                                                  │
│  jtag_client.py   — JtagClient, JtagVerifier,                    │
│                     JtagIdcode dataclass                         │
│                                                                  │
│  peci_client.py   — PeciClient, SmlinkClient,                    │
│                     PeciTempReading, PeciDib                     │
│                                                                  │
│  fan_controller.py — FanController, FanSpeedVerifier,            │
│                      FanTray enum, FanRpmSpec                    │
└───────────────────────────┬──────────────────────────────────────┘
                            │
             ┌──────────────┴─────────────┐
             │                            │
┌────────────▼──────────┐   ┌─────────────▼──────────┐
│  Real BMC via IPMI    │   │  MockBmcClient         │
│  ipmitool lanplus     │   │  mock/mock_bmc.py      │
│  RMCP+ UDP port 623   │   │  no hardware needed    │
└───────────────────────┘   └────────────────────────┘
```

---

## Design decisions

### Return values instead of shared state

`IpmiClient.run()` returns a string. Each caller stores it in a local
variable. This makes unit testing straightforward — `MockBmcClient` swaps
in without any shared state to manage, and two tests running in parallel
cannot corrupt each other's results.

### Bidirectional hardware verification

Every verify function checks two directions against `sys_conf.json`:

```
sys_conf[CPU0] = 1 → BMC must report Presence detected
sys_conf[CPU1] = 0 → BMC must NOT report Presence detected
```

The second direction catches firmware bugs where sensors are reported
for unpopulated slots. Checking both directions is what catches
phantom sensors — the second direction is easy to overlook.

### try/finally for hardware state restoration

The fan speed sweep disables BMC auto-fan control to take manual PWM
control. `try/finally` guarantees auto-mode is restored even if the
sweep raises an exception — without it, fans can be stuck at 10% duty.

```python
fan_controller.set_auto_mode(enabled=False)
try:
    # sweep through duty points
    ...
finally:
    fan_controller.set_auto_mode(enabled=True)
```

The same pattern appears in the CPLD CATERR injection test to guarantee
the CPLD is always re-locked after injection regardless of what happens.

### struct.unpack for binary parsing

The format string documents the wire format directly:

```python
# '<HBI BBBBB bBBB' is IPMI 2.0 Table 32-1 written as code
(record_id, record_type, timestamp,
 gen_id_low, gen_id_high, ev_msg_rev,
 sensor_type, sensor_num, evt_dir_type,
 ev1, ev2, ev3) = struct.unpack('<HBI BBBBB bBBB', record[:16])
```

The format string maps directly to the spec table, readable left to right.
`struct.unpack_from('<I', record, 3)` handles byte ordering and offset
arithmetic without manual calculation.

### PresenceState enum for sensor presence

```python
class PresenceState(Enum):
    CONFIRMED_PRESENT  = auto()   # sys_conf=1, BMC=detected
    CONFIRMED_ABSENT   = auto()   # sys_conf=0, BMC=not detected
    UNEXPECTED_PRESENT = auto()   # sys_conf=0, BMC=detected  (phantom)
    UNEXPECTED_ABSENT  = auto()   # sys_conf=1, BMC=not detected (missing)
```

Four named states instead of a raw integer. Code that checks
`state == PresenceState.UNEXPECTED_PRESENT` is unambiguous about which
failure case it is handling.

### MockBmcClient for hardware-independent unit tests

`MockBmcClient` implements the same `run()` / `run_raw()` interface as
`IpmiClient` and returns canned responses. Protocol parsing and
verification logic is fully testable without a real BMC:

```python
# unit test — no BMC, no network
mock = MockBmcClient()
verifier = FruIpmiVerifier(mock)
result = verifier.verify(FRU_MOTHERBOARD, board_label='MB')
assert result.result_code == 0
```

Integration tests swap in the real `IpmiClient` via pytest fixtures.
The test logic is identical in both cases.

---

## Protocol reference

### IPMI over LAN (RMCP+)

```
Every command uses a NetFn/Cmd byte pair:
    0x06  App       — mc info, chassis power, I2C bridge (Cmd 0x52)
    0x0A  Storage   — Get SEL Entry, Read FRU Data

OEM NetFns are platform-specific and replaced with 0xNN placeholders.
```

### IPMI Master Write-Read (I2C bridge)

```
NetFn=0x06 Cmd=0x52 — BMC acts as I2C master

Request format:
    Byte 0: I2C bus number
    Byte 1: slave address (7-bit, write bit = 0)
    Byte 2: read count
    Byte 3+: write data (register address, then data bytes)

Example — write 0xAA to register 0x10 on device 0x40, bus 7:
    raw 0x06 0x52 0x07 0x40 0x00 0x10 0xAA
```

### JTAG IDCODE register

```
32-bit mandatory register (IEEE 1149.1):
    [31:28]  version     — silicon stepping
    [27:12]  part number — device identifier
    [11:1]   mfr ID      — JEDEC manufacturer
    [0]      1           — always set (IEEE requirement)

Sentinel values:
    0x00000000 — TDO floating or no device in chain
    0xFFFFFFFF — BYPASS register (CPU not in scan chain)
    bit[0]=0   — IEEE 1149.1 violation (TDO stuck or marginal)
```

### PECI GetTemp encoding

```
Response: 3 bytes [completion_code, temp_LSB, temp_MSB]

completion_code:
    0x40 = pass  (valid reading)
    0x80 = abort (CPU in deep C-state, try again)
    0x90 = error (PECI bus timeout)

temp: signed 16-bit little-endian, units of 1/64 °C below Tjmax
    actual_temp = Tjmax + (signed_value / 64.0)

Example: [0x40, 0xC0, 0xFE]
    raw = struct.unpack('<h', b'\xC0\xFE') = -320
    temp = 105 + (-320 / 64) = 100.0 °C
```

### SEL record types

```
Byte 2 of every 16-byte record identifies the type:
    0x02    Standard System Event — sensor events, power, CPU faults
            Fields: timestamp, generator_id, sensor_type,
                    sensor_num, event_dir_type, event_data[3]

    0xC0    OEM Timestamped — used by BIOS for PCIe AER events
            Fields: timestamp, manufacturer_id, vendor_id,
                    device_id, slot_number, pcie_error_id

    0xC1+   OEM Non-Timestamped — layout is entirely OEM-defined

Navigation: each Get SEL Entry response starts with a 2-byte
next_record_id. 0xFFFF signals end of SEL.
```

---

## Configuration files

### sys_conf.json

Hardware presence bitmap. `1` = installed, `0` = empty slot.

```json
{
    "CPU0": 1,      "CPU1": 0,
    "NVMeSSD_0": 1, "NVMeSSD_1": 0,
    "PSU0": 1,
    "FAN_SYS_0": 1,
    "IOBU_OCP1": 0, "IOBD_OCP1": 1,
    "OAM0": 0,
    "DIMM_A0": 1,   "DIMM_G0": 0
}
```

Every verify function reads this file to know what hardware to expect.
Copy `sys_conf_template.json` and fill it in for your unit.

### test_configs.json

Authorization registry. A test case only runs if its TC ID is present.

```json
{
    "TC_BMC_0_0200": "System fan status",
    "TC_BMC_0_0206": "CPU sensors"
}
```

`test_configs_example.json` authorizes the full regression suite.
Create a smaller file for targeted runs or daily smoke tests.

### i2c_devices.ini

I2C stress test vector. Each line defines one device:

```ini
B=<bus> A=<addr> WC=<write_count> RC=<read_count> [RM=<read_mask>]
```

Bus numbers above 9 use ASCII encoding (bus 10 = `:`, bus 15 = `?`).

`gen_i2ctest_ini()` filters this against sys_conf.json to exclude
absent hardware. `gen_i2ctest_ini_dc_off()` further excludes devices
that need main DC power (retimers, OCP NICs, NVMe, PCIe switches).
