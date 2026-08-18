# BMC Firmware Validation Framework

Python/pytest automation for validating BMC firmware and hardware protocols
on server platforms. Covers sensors, fans, FRU inventory, JTAG, PECI, I2C,
firmware update, and CPLD fault injection.

---

## Background

BMC validation on server platforms involves a lot of repetitive manual work —
running ipmitool commands, cross-referencing output against hardware configuration,
checking for phantom sensors on empty slots. This framework automates that loop.

The key design constraint was unit testability: all hardware-touching code is
isolated in transport classes that accept a mock client, so the parser and
validator logic can be tested without a BMC on the desk.

---

## Architecture

```
src/tests/          pytest integration + unit tests
src/protocol/       SDR parser, SEL decoder, FRU validator
src/transport/      IPMI, JTAG, PECI, fan controller, firmware update
mock/               MockBmcClient — canned responses for unit tests
config/             sys_conf.json (hardware bitmap), test_configs.json
```

Hardware tests (`test_bmc_*.py`) skip by default. Pass `--integration` plus
BMC credentials to run them against real hardware.

```
python -m pytest src/tests/ -v                        # unit tests only
python -m pytest src/tests/ -v --integration \
    --bmc-ip 192.168.1.100 --bmc-user admin \
    --bmc-password yourpassword                       # full hardware run
```

---

## sys_conf.json

The hardware presence bitmap that drives every test. Mark which slots are
physically populated in your unit:

```json
{
    "CPU0": 1, "CPU1": 0,
    "OAM0": 1, "OAM1": 1, "OAM2": 0,
    "PSU0": 1, "PSU1": 1,
    "NVMeSSD_0": 1
}
```

Tests check both directions: populated slots must produce valid readings,
empty slots must not produce readings at all. The second check catches BMC
firmware bugs that report phantom sensors for unpopulated hardware.

---

## Test categories

| TC prefix | What it tests                                 | Destructive |
|-----------|-----------------------------------------------|-------------|
| 0001-0002 | I2C bus stress (DC-ON and DC-OFF)             | No          |
| 0100-0109 | JTAG/ASD — IDCODE from CPU, PCH, accelerators | No          |
| 0150      | SMLINK / Intel Node Manager reachability      | No          |
| 0160-0165 | PECI — CPU temperature, Ping, GetDIB          | No          |
| 0180      | CPLD CATERR injection + BMC SEL verification  | Minor       |
| 0200-0252 | SDR sensors across power states               | No          |
| 0300-0314 | Firmware update (BIOS, BMC, CPLD, AMC)        | **Yes**     |
| 0400-0404 | FRU validation via IPMI                       | No          |
| 2400-2404 | FRU validation via Web UI (Selenium)          | No          |

Firmware update tests overwrite flash. They are disabled by default and
require explicit authorization in `test_configs.json`.

---

## Quick start

```bash
git clone https://github.com/andyydhuang/bmc-validation-framework
cd bmc-validation-framework
pip install -r requirements.txt
python -m pytest src/tests/ -v
```

---

## Protocols covered

- IPMI 2.0 (RMCP+ over UDP 623) — SDR, SEL, FRU, Master Write-Read
- JTAG IEEE 1149.1 — TAP state machine, IDCODE, daisy-chain
- PECI — Intel CPU thermal: Ping, GetTemp, GetDIB
- SMBus/SMLINK/IPMB — Node Manager bridge
- SEL binary format — standard (0x02), OEM (0xC0), PCIe AER errors
- FRU EEPROM — chassis/board/product areas per IPMI 2.0 Section 33
- I2C stress testing — multi-bus, power-state conditional device lists

---

## Requirements

- Python 3.9+
- ipmitool (integration tests only)
- Chrome + ChromeDriver (Web UI tests only)
- See `requirements.txt`

---

## OEM command note

Platform-specific OEM IPMI values (NetFn/Cmd codes, register addresses,
IDCODE values, authentication bytes) have been replaced with `0xNN`
placeholders. Each placeholder has a comment explaining what it represents.
Public protocol constants (PECI addresses, standard IPMI commands, IEEE
1149.1 sentinel values) are unchanged.

To use this on your platform, search for `0xNN` and replace each one with
the value from your BMC firmware OEM specification.
