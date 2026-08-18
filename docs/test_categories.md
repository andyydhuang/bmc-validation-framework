# Test Categories Reference

This document describes every test category in the framework, the hardware
subsystem it validates, the protocols involved, and the specific failure
modes it is designed to catch.

---

## TC_BMC_0_0001 — I2C Stress Test (DC-ON state)

**Hardware:** All I2C buses and devices present in the unit
**Protocol:** IPMI Master Write-Read (NetFn=0x06 Cmd=0x52)
**Destructive:** No

**What it validates:**
Every I2C device in the platform is accessed repeatedly to verify bus
integrity. The test sends write-then-read transactions to each device
and compares responses against expected register values where known.

**Test vector generation:**
The full device list in `i2c_devices.ini` is filtered by two stages:
1. `gen_i2ctest_ini()` — removes devices absent per sys_conf.json
2. Result: `i2c_stress_test.ini` covering only installed hardware

**Why it runs first:**
I2C bus failures (stuck SCL, intermittent NACK, address collision) produce
intermittent failures across all other test categories. Running the I2C
stress test first as a baseline confirms the bus infrastructure is healthy
before functional tests begin.

**Failure modes caught:**
- I2C bus lockup (SCL held low by a failing device)
- Address collision between two devices sharing the same address
- Corrupt register values (EEPROM data corruption, CPLD reset state wrong)
- I2C MUX failure (channel not switching correctly)

---

## TC_BMC_0_0002 — I2C Stress Test (DC-OFF state)

**Hardware:** Standby-powered I2C devices only
**Protocol:** IPMI Master Write-Read
**Destructive:** No

**What it validates:**
Same as TC_BMC_0_0001 but with the host CPU powered off. A reduced test
vector is used that excludes devices requiring main DC power.

**Test vector generation:**
`gen_i2ctest_ini_dc_off()` further filters the DC-ON vector to remove:
- Devices on 3.3V main rail (CPLDs on non-standby domains)
- PCIe retimers (require 0.8V from VR)
- OCP NIC cards (require PCIe slot power)
- PCIe switches (require 3.3V main)
- DCDC converter PMBus interfaces (require converter to be running)

**Failure modes caught:**
- Accessing a device that should be unpowered (causes I2C bus hang)
- Standby power domain failures
- FRU EEPROMs on standby rail not accessible (board assembly defect)

---

## TC_BMC_0_0100 through TC_BMC_0_0105 — JTAG/ASD Interface

**Hardware:** CPUs, PCH, accelerator modules
**Protocol:** JTAG IEEE 1149.1 via BMC OEM IPMI commands
**Destructive:** No

| TC ID         | Target                | Expected IDCODE    |
|---------------|-----------------------|--------------------|
| TC_BMC_0_0100 | CPU0                  | Platform-specific  |
| TC_BMC_0_0101 | CPU1                  | Platform-specific  |
| TC_BMC_0_0102 | ASD software path     | N/A                |
| TC_BMC_0_0103 | ASD software path     | N/A                |
| TC_BMC_0_0104 | Accelerator slots 0-7 | Two valid variants |
| TC_BMC_0_0105 | PCH                   | Platform-specific  |

**What it validates:**
The BMC's JTAG master infrastructure can route to each target and
read the mandatory IEEE 1149.1 IDCODE register correctly.

**Three-step access sequence:**
1. Set ASD mode on BMC JTAG controller
2. Route hardware MUX to target scan chain
3. Read 32-bit IDCODE from target

**Failure modes caught:**
- Wrong silicon installed (IDCODE does not match spec)
- JTAG MUX routing failure (wrong target responds)
- Broken scan chain (0x00000000 or 0xFFFFFFFF returned)
- Wrong CPU stepping (version bits mismatch)
- ASD mode not setting correctly (subsequent JTAG commands fail)

---

## TC_BMC_0_0150 — SMLINK / Node Manager

**Hardware:** PCH running Intel Node Manager firmware
**Protocol:** SMBus via IPMB bridge (ipmitool -b 0x06 -t 0x2C)
**Destructive:** No

**What it validates:**
Intel Node Manager is alive on the SMLink bus and identifies itself
as Intel Corporation firmware.

**Protocol path:**
```
ipmitool → IPMI LAN → BMC → SMLink SMBus (bus 6) → PCH slave 0x2C
```

**Failure modes caught:**
- Node Manager firmware not initialized
- SMLink bus routing failure
- PCH not responding (power sequencing issue)
- Wrong manufacturer (identifies as non-Intel)

---

## TC_BMC_0_0160 through TC_BMC_0_0165 — PECI Thermal Protocol

**Hardware:** CPU0 and CPU1
**Protocol:** PECI via BMC OEM IPMI bridge
**Destructive:** No

| TC ID         | Operation   | CPU   |
|---------------|-------------|-------|
| TC_BMC_0_0160 | Ping        | CPU0  |
| TC_BMC_0_0161 | Ping        | CPU1  |
| TC_BMC_0_0162 | GetTemp     | CPU0  |
| TC_BMC_0_0163 | GetTemp     | CPU1  |
| TC_BMC_0_0164 | GetDIB      | CPU0  |
| TC_BMC_0_0165 | GetDIB      | CPU1  |

**What it validates:**
- Ping: PECI bus connectivity — CPU is reachable at its address
- GetTemp: thermal sensor returns a valid reading within expected range
- GetDIB: device identification block reports valid PECI revision

**Temperature validation:**
```
Completion code must be 0x40 (pass)
Temperature = Tjmax + (signed_raw / 64.0)
Valid range: -50°C to Tjmax
Below -50°C: sensor not initialized (BIOS POST not complete)
Above Tjmax: CPU in thermal emergency
```

**Failure modes caught:**
- PECI bus disconnected or broken
- CPU not responding to PECI (power issue)
- Temperature reading below -50°C (thermal subsystem not initialized)
- CPU overtemperature condition
- Wrong PECI protocol revision

---

## TC_BMC_0_0180 — CPLD Fault Injection (CATERR)

**Hardware:** CPLD, BMC GPIO, System Event Log
**Protocol:** I2C via IPMI Master Write-Read, IPMI SEL
**Destructive:** Minor (clears and modifies SEL, triggers ACD)

**What it validates:**
The complete CATERR detection chain: CPLD register write → signal
propagation → BMC GPIO interrupt → SEL record write.

**Injection sequence:**
1. Clear SEL (clean baseline)
2. Unlock CPLD write protection (four-byte key sequence)
3. Clear CATERR injection register
4. Assert CATERR injection register (active-low = write 0x00)
5. Unmask signal propagation to BMC
6. Poll SEL until CATERR record appears (30s timeout)
7. Wait for ACD crash dump collection to complete

**Active-low signal design:**
CATERR uses active-low signaling. Writing 0x00 to the injection register
drives the signal low (asserted). A pull-up resistor holds the line high
(deasserted) in normal operation. This design ensures broken wires or
unpowered chips fail safely to the deasserted state.

**Failure modes caught:**
- BMC CATERR GPIO interrupt handler disabled or misconfigured
- CPLD unlock sequence not working (CPLD firmware version mismatch)
- Signal mask register not propagating to BMC
- SEL write failure after CATERR detection
- ACD collection not completing

---

## TC_BMC_0_0200 through TC_BMC_0_0211 — SDR Sensor Validation

**Hardware:** All installed sensors per sys_conf.json
**Protocol:** IPMI SDR (ipmitool sdr elist all)
**Destructive:** No

| TC ID         | Subsystem                              |
|---------------|----------------------------------------|
| TC_BMC_0_0200 | System fan status and RPM              |
| TC_BMC_0_0201 | PSU status, fan, current, power, temp  |
| TC_BMC_0_0202 | Board VR voltage rails                 |
| TC_BMC_0_0203 | PCIe switch temperatures               |
| TC_BMC_0_0204 | Board power consumption                |
| TC_BMC_0_0206 | CPU status, temperature, power, VR     |
| TC_BMC_0_0207 | NVMe SSD presence and temperature      |
| TC_BMC_0_0208 | DIMM temperature and memory power      |
| TC_BMC_0_0210 | OCP NIC presence and temperature       |
| TC_BMC_0_0211 | Fan speed sweep (PWM + TACH readback)  |

**Bidirectional verification:**
Every SDR test checks both directions:
- Direction 1: component in sys_conf → sensor must appear with valid reading
- Direction 2: component absent in sys_conf → sensor must NOT appear

**TC_BMC_0_0211 — Fan Speed Sweep:**
Unique among SDR tests — actively controls hardware:
1. Disables BMC auto-fan control
2. Sweeps each fan through 100%, 50%, 10% duty cycles
3. Reads TACH via CPLD register (direct path)
4. Reads TACH via SDR (indirect path through BMC polling)
5. Validates both readings against datasheet acceptance bands
6. Re-enables BMC auto-fan control (guaranteed via try/finally)

**Failure modes caught:**
- Phantom sensors for unpopulated hardware slots
- Missing sensors for installed hardware
- Fan RPM outside datasheet acceptance band at commanded duty
- CPLD TACH reading inconsistent with SDR TACH reading
- CPU temperature sensor returning No Reading despite CPU presence
- VR voltage disabled despite CPU presence

---

## TC_BMC_0_0300 — BIOS Firmware Update

**Hardware:** BIOS SPI flash chip
**Protocol:** FwFlashTool over IPMI LAN
**Destructive:** YES — permanently rewrites BIOS flash

**What it validates:**
BIOS image can be flashed and verified in both directions:
- Downgrade: newer → older version
- Upgrade:   older → newer version

**Pre-requisite:** Host must be powered OFF before flashing.
Concurrent CPU access to SPI flash during erase/write → immediate crash.

**Verification method:**
After flash + power-on, reads BIOS version via IPMI Get System Info
Parameters (NetFn=0x06 Cmd=0x59, parameter 0x01 = firmware string).

---

## TC_BMC_0_0301 — BMC Firmware Update

**Hardware:** BMC NOR flash (dual-image architecture)
**Protocol:** FwFlashTool over IPMI LAN (-d not specified, BMC updates itself)
**Destructive:** YES — permanently rewrites BMC flash

**What it validates:**
Four update scenarios:
1. Image slot 1 update, no config preservation
2. Image slot 1 update, with config preservation
3. Image slot 2 update
4. Both slots updated simultaneously

**Dual-image architecture:**
BMC flash has two independent image slots. A boot selector register
(platform-specific command) controls which slot is active. This enables:
- Update slot 2 while running from slot 1 (live update)
- Verify slot 2 boots correctly
- Roll back to slot 1 if slot 2 has issues

**Post-flash verification:**
1. Poll BMC ping (ICMP) until network stack restores (~90-120s)
2. Re-authenticate (no-preserve-config resets credentials)
3. Read active image via platform-specific command, confirm expected slot
4. Read firmware version via mc info, confirm expected version

---

## TC_BMC_0_0302 through TC_BMC_0_0309 — CPLD Firmware Update

**Hardware:** 8 CPLD devices (fan boards, IO boards, power board, main board)
**Protocol:** FwFlashTool over IPMI LAN
**Destructive:** YES — permanently rewrites CPLD flash

**What it validates:**
- TC_BMC_0_0302: cfm0 sector update (active sector)
- TC_BMC_0_0303: cfm1-to-cfm0 fallback (recovery mechanism)

**Dual-sector CPLD architecture:**
Each CPLD has two flash sectors (cfm0 = active, cfm1 = backup).
TC_BMC_0_0303 tests the fallback mechanism:
1. Flash cfm1 with old version (establish baseline)
2. Erase cfm0 (simulate corrupted active sector)
3. Flash cfm0 with new version
4. Virtual reseat → CPLD should boot from cfm1 (old, trusted)
5. Verify active = old version (fallback worked)
6. Virtual reseat → CPLD should boot from cfm0 (new, preferred)
7. Verify active = new version (cfm0 priority confirmed)

**Activation:**
Virtual reseat command triggers CPLD reload from flash.
Without reseat, CPLDs continue running from internal flip-flops
regardless of what was written to flash.

---

## TC_BMC_0_0310 — AMC Firmware Update (PLDM)

**Hardware:** Accelerator Module Controllers (one per accelerator slot)
**Protocol:** PLDM (Platform Level Data Model) via FwFlashTool
**Destructive:** YES — permanently rewrites AMC EEPROM

**What it validates:**
Dynamic MCTP (Management Component Transport Protocol) EID discovery
followed by PLDM firmware update for each present accelerator module.

**EID discovery:**
AMC firmware update requires an EID (Endpoint ID) — a MCTP address
assigned by the BMC at boot time. EIDs are dynamic (not fixed) so the
test discovers them at runtime by querying the BMC's MCTP endpoint table
via a platform-specific command.

**Interactive stdin:**
FwFlashTool prompts for the EID interactively when using PLDM mode. The test
automatically supplies the EID to FwFlashTool's stdin pipe.

---

## TC_BMC_0_0400 through TC_BMC_0_0404 — FRU Inventory (IPMI path)

**Hardware:** FRU EEPROMs on each board
**Protocol:** IPMI Read FRU Data (NetFn=0x0A Cmd=0x11)
**Destructive:** No

| TC ID         | Board         | What it checks                    |
|---------------|---------------|-----------------------------------|
| TC_BMC_0_0400 | Front Panel   | Chassis type, board product name  |
| TC_BMC_0_0401 | Motherboard   | Chassis type, board product name  |
| TC_BMC_0_0402 | UBB board     | Chassis type, board product name  |
| TC_BMC_0_0403 | IO board NICs | Manufacturer (any valid vendor)   |
| TC_BMC_0_0404 | MB OCP NICs   | Manufacturer (any valid vendor)   |


---

## TC_BMC_2_0400 through TC_BMC_2_0404 — FRU Inventory (Web UI path)

**Hardware:** Same FRU EEPROMs as IPMI path
**Protocol:** HTTPS + Selenium browser automation
**Destructive:** No

**Dual-path design:**
The `TC_BMC_2_*` tests verify the same FRU data as `TC_BMC_0_*` but
through the BMC web UI. Both paths read the same physical EEPROM through
separate BMC firmware code modules.

**Additional field checked:**
Web UI tests check three fields (vs two for IPMI):
- FRU Device Name (shown in web UI dropdown — not in ipmitool output)
- Chassis Type
- Board Product Name (note: web UI adds "Name" suffix)

**Failure modes caught by dual-path:**
- IPMI FRU parser bug caught by web UI test passing
- Web UI rendering bug caught by IPMI test passing
- Both fail → likely FRU EEPROM data corruption
