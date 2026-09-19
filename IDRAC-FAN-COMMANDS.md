# Dell iDRAC 7/8 fan command reference

Rack/tower PowerEdge reference, checked September 2026. Covers documented fan/thermal controls and known OEM commands; no exhaustive public list of private OEM commands exists. Availability depends on server and firmware. Blade chassis fans are managed by CMC.

## Connecting

Commands below omit connection options. For remote IPMI, replace `ipmitool` with `ipmitool -I lanplus -H <idrac-ip> -U <user> -a` (password prompt; IPMI over LAN must be enabled). Run `racadm` locally on the managed server, use `racadm -r <idrac-ip> -u <user> -p <password> ...` (HTTPS), or log into iDRAC with `ssh <user>@<idrac-ip>` and run RACADM there. Angle-bracket values are placeholders.

## Readings, status and logs

| Command | Queries |
|---|---|
| `ipmitool sdr type Fan` | Fan RPM/status and sensor identifiers. |
| `ipmitool sdr elist all` | All sensors, including fan redundancy/discrete status where exposed. |
| `ipmitool sensor list` | Readings and thresholds for all sensors. |
| `ipmitool sensor get "<fan sensor name>"` | Detailed fan reading, thresholds and event state. |
| `ipmitool sensor reading "<fan sensor name>"` | Reading only; use the exact name discovered above. |
| `ipmitool sdr get "<fan sensor name>"` | Detailed SDR information for a fan. |
| `ipmitool sdr type Temperature` | Temperatures relevant to cooling. |
| `ipmitool chassis status` | Includes the cooling/fan fault flag. |
| `ipmitool sel elist` | Event history, including fan failures and redundancy events. |
| `racadm getsensorinfo` / `racadm getsensorinfo -c` | Fan RPM, health, thresholds and PWM where exposed; normal/compact output. |
| `racadm getsel` / `racadm lclog view` | System/Lifecycle logs, including cooling and PCI-card events. |

Sources: [ipmitool manual](https://raw.githubusercontent.com/ipmitool/ipmitool/master/doc/ipmitool.1.in), [Dell sensor reference](https://www.dell.com/support/manuals/en-us/idrac7-8-lifecycle-controller-v2.40.40.40/idrac%20racadm%202.40.40.40/getsensorinfo?guid=guid-b1cabb50-b8c0-496f-adbb-a559bbced990), [Dell RACADM guide](https://dl.dell.com/topicspdf/idrac8-lifecycle-controller-v2818181_cli-guide_en-us.pdf).

## RACADM automatic cooling settings

Read everything with `racadm get System.ThermalSettings`. For each property below, query with `racadm get System.ThermalSettings.<property>`; writable properties use `racadm set System.ThermalSettings.<property> <value>`.

| Property | Values / purpose |
|---|---|
| `FanSpeedOffset` | `0` low, `1` high, `2` medium, `3` maximum, `255` off. Available levels vary. |
| `MinimumFanSpeed` | PWM floor, bounded by the limits below; `255` means no custom floor. Not a fixed speed. |
| `MFSMinimumLimit` / `MFSMaximumLimit` | Read-only bounds for the custom floor. |
| `FanSpeedLowOffsetVal` / `FanSpeedHighOffsetVal` | Read-only PWM values for low/high offsets. |
| `FanSpeedMediumOffsetVal` / `FanSpeedMaxOffsetVal` | Read-only PWM values for medium/maximum offsets. |
| `ThermalProfile` | `0` automatic, `1` maximum performance, `2` minimum power. |
| `AirExhaustTemp` | Limit selector: `0`=40°C, `1`=45°C, `2`=50°C, `3`=55°C, `4`=60°C, `255`=70°C/default; supported limits vary. |
| `ThirdPartyPCIFanResponse` | `0` disables extra cooling for third-party PCIe cards; `1` enables it (default). |

Example: `racadm set System.ThermalSettings.FanSpeedOffset 255` removes the offset. Automatic cooling can still demand higher speeds. Older manuals contain contradictory PCI-response examples; verify the reported Enabled/Disabled state after changing it.

Sources: [Dell thermal settings](https://www.dell.com/support/manuals/en-us/idrac8-with-lc-v2.05.05.05/idrac8_2.05.05.05_ug/modifying-thermal-settings-using-racadm?guid=guid-476e0462-fb31-4dac-be4a-3af3801ae556), [later Dell guide, including PCI response](https://www.dell.com/support/manuals/en-uk/idrac8-lifecycle-controller-v2.75.75.75/idrac8_2.75.75.75_ug/modifying-thermal-settings-using-racadm?guid=guid-476e0462-fb31-4dac-be4a-3af3801ae556&lang=en-us).

## IPMI manual speed (undocumented OEM interface)

Manual mode bypasses normal temperature-based fan regulation: monitor temperatures and restore automatic control when finished. These commands are model/firmware-dependent, not a supported universal iDRAC API.

| Command | Action |
|---|---|
| `ipmitool raw 0x30 0x30 0x01 0x00` | Enable manual control. |
| `ipmitool raw 0x30 0x30 0x02 0xff 0x64` | All fans to 100% PWM; requires manual mode. |
| `ipmitool raw 0x30 0x30 0x02 0xff <PWM>` | All fans to the supplied percentage byte: e.g. `0x32`=50%, `0x64`=100%. This is not RPM. |
| `ipmitool raw 0x30 0x30 0x01 0x01` | Restore automatic fan regulation. |

No portable, verified manual-mode readback command is included; inspect actual RPM/PWM with the queries above. Do not assume per-fan selectors or settings persistence across resets work on every model.

Sources: [upstream OEM command discussion](https://github.com/ipmitool/ipmitool/issues/30), [iDRAC fan-controller implementation and compatibility notes](https://github.com/tigerblue77/Dell_iDRAC_fan_controller_Docker).

## IPMI PCIe cooling override

| Command | Action |
|---|---|
| `ipmitool raw 0x30 0xce 0x01 0x16 0x05 0x00 0x00 0x00` | Query third-party PCI-card cooling response (raw bytes). |
| `ipmitool raw 0x30 0xce 0x00 0x16 0x05 0x00 0x00 0x00 0x05 0x00 0x01 0x00 0x00` | Disable extra PCI-card cooling. |
| `ipmitool raw 0x30 0xce 0x00 0x16 0x05 0x00 0x00 0x00 0x05 0x00 0x00 0x00 0x00` | Enable extra PCI-card cooling. |

The raw disable flag has the opposite polarity to RACADM's enable setting. Disabling this response does not establish a fixed speed and may leave PCIe cards undercooled. [Command reference](https://github.com/ipmitool/ipmitool/issues/30).

## Legacy iDRAC 7 IPMI offsets

Prefer RACADM when available. These change the automatic cooling offset, not a fixed RPM:

| Command | Action |
|---|---|
| `ipmitool raw 0x30 0xce 0x00 0x09 0x07 0x00 0x00 0x00 0x07 0x00 0x02 0x02 0x02 0x00 0x00` | Low offset. |
| `ipmitool raw 0x30 0xce 0x00 0x09 0x07 0x00 0x00 0x00 0x07 0x00 0x02 0x02 0x02 0x01 0x00` | High offset. |

Source: [Dell support discussion documenting both commands](https://www.dell.com/community/en/conversations/poweredge-hardware-general/poweredge-r220-high-fan-speed/647f6738f4ccf8a8de436cf7?commentId=647f674df4ccf8a8de44c663).

## Thresholds and scope limits

`ipmitool sensor thresh "<fan sensor name>" <threshold> <RPM>` is the generic threshold-write command (`lnr`, `lcr`, `lnc`, `unc`, `ucr`, `unr`). Group forms: `... lower <lnr> <lcr> <lnc>` and `... upper <unc> <ucr> <unr>`. These change alarm thresholds, not fan speed; iDRAC may expose them as read-only or reject writes. [ipmitool implementation](https://github.com/ipmitool/ipmitool/blob/master/lib/ipmi_sensor.c).

`racadm getfanreqinfo` is **CMC-only**, despite appearing in combined iDRAC/CMC manuals; it is not an iDRAC 7/8 fan command. [Dell scope statement](https://dl.dell.com/manuals/all-products/esuprt_software/esuprt_remote_ent_sys_mgmt/esuprt_rmte_ent_sys_chassis_mgmt_cntrllr/dell-chassis-mgmt-cntrllr-v4.4_reference%20guide_en-us.pdf).

## Fake-server implementation status

The bundled `fake_idrac.py` implements IPMI v1.5 (`-I lan`) and IPMI v2.0/RMCP+ (`-I lanplus`) transport. The RMCP+ path supports ipmitool's default cipher suite 3 (RAKP-HMAC-SHA1 authentication, HMAC-SHA1-96 integrity, and AES-CBC-128 confidentiality) with the fake credentials `user` / `pass`. Dell OEM manual-control commands (`0x30 0x30 0x01 0x00`, `0x30 0x30 0x01 0x01`, and `0x30 0x30 0x02 0xff <PWM>`) update its six-fan in-memory state. `chassis status` and `sel elist` return basic synthetic responses. PCI-card cooling overrides and legacy offset raw commands update state and are visible at `/state`, but do not yet model their thermal effects. SDR enumeration, fan sensor readings with RPM conversion, and threshold-write requests work over the shared command dispatcher. Generic success responses for some discovery probes must not be mistaken for functional behavior.
