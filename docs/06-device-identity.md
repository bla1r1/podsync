# 06 — Device identity: `podsync.device` (models, capabilities, discovery)

This chapter specifies how podsync names an iPod model, what each model can
do, how a mounted iPod is found and identified, and how virtual iPods are
created. The write-safety half of `podsync.device` (write guard, path and
storage safety, filesystem profiles, durability, eject, recovery, dumps,
metadata writes, Linux udev integration) is chapter 07. Artwork format
*definitions* (the `ArtworkFormat` registry) are owned by chapter 05 §4;
this chapter only says how devices select them.

Conventions:

* Family and generation are **strings** everywhere (`"iPod"`, `"5th Gen"`,
  `"iPod Classic"`, `"6.5th Gen"`, …). There are no numeric family ids in
  lookups.
* Capacity is a marketing **string** such as `"60GB"` or `"512MB"`.
* "Source" names (provenance labels such as `sysinfo`, `vpd`, `usb_pid`) are
  listed in §10.2.
* Identification **never writes to a real iPod**. The only device writes in
  this chapter are the explicit ones: creating a virtual iPod (§12) and
  repairing its missing database when it is identified (§9.1), creating an
  empty database for an identified device (§11), and the user-invoked VPD
  tool that writes SysInfo (§14.6).
* Every module imports without side effects on every OS. Platform-only
  imports (`winreg`, `ctypes.windll`, `fcntl`, IOKit, `usb`) happen inside
  functions or behind platform checks; `vpd_iokit` alone refuses to import
  outside macOS (§14.3).

---

## 1. Package surface (`podsync/device/__init__.py`)

Importing `podsync.device` imports its submodules eagerly and re-exports the
names below. Tests patch several of these **at package level** (for example
`podsync.device.get_current_device_for_path`, `podsync.device.resolve_itdb_path`,
`podsync.device.itdb_write_filename`), so other packages must look them up
through `podsync.device` at call time.

| Origin module | Re-exported names |
|---|---|
| `artwork` | `ARTWORK_FORMATS_BY_ID`, `ITHMB_FORMAT_MAP`, `ITHMB_SIZE_MAP`, `cover_art_format_definitions_for_device`, `ithmb_formats_for_device`, `photo_formats_for_device`, `resolve_cover_art_format_definitions`, `resolve_cover_art_format_definitions_for_device` |
| `bootstrap` | `ensure_device_itunes_database` |
| `capabilities` | `ArtworkFormat`, `DeviceCapabilities`, `capabilities_for_family_gen`, `checksum_type_for_family_gen`, `cover_art_formats_for_family_gen` |
| `checksum` | `CHECKSUM_MHBD_SCHEME`, `MHBD_SCHEME_TO_CHECKSUM`, `ChecksumType` |
| `info` | `DeviceInfo`, `UnidentifiedDeviceError`, `clear_current_device`, `detect_checksum_type`, `enrich`, `get_current_device`, `get_current_device_for_path`, `get_firewire_id`, `has_exact_model_number`, `itdb_write_filename`, `read_sysinfo`, `require_exact_model_number`, `resolve_itdb_path`, `set_current_device` |
| `lookup` | `extract_model_number`, `get_friendly_model_name`, `get_model_info`, `infer_generation`, `lookup_by_serial`, `match_serial_suffix` |
| `models` | `IPOD_MODELS`, `IPOD_RECOVERY_USB_PIDS`, `IPOD_USB_PIDS`, `SERIAL_LAST3_TO_MODEL`, `SERIAL_SUFFIX_TO_MODEL`, `USB_PID_TO_MODEL`, `canonicalize_model_identity` |
| `sysinfo` | `DeviceEvidence`, `EvidenceValue`, `ParsedSysInfoExtended`, `identity_from_sysinfo`, `identity_from_sysinfo_extended`, `parse_sysinfo_extended`, `parse_sysinfo_text` |
| `virtual` | `VIRTUAL_IPOD_INFO_FILENAME`, `available_virtual_ipod_models`, `create_virtual_ipod`, `ensure_virtual_itunes_database`, `has_virtual_ipod_info`, `load_virtual_ipod_info`, `virtual_ipod_info_path` |
| `vpd_libusb` | `identify_via_vpd`; aliases `usb_query_all_ipods` (= `query_all_ipods`), `usb_query_ipod_vpd` (= `query_ipod_vpd`), `usb_write_sysinfo` (= `write_sysinfo`) |
| `vpd_usb_control` | `query_all_ipod_usb_sysinfo_extended`, `query_ipod_usb_sysinfo_extended` |
| `scanner` | `identify_ipod_at_path`, `scan_for_ipods` |

Two optional aliases are bound only when their module imports; if the import
raises `ImportError` the name is simply absent (not `None`):
`linux_query_ipod_vpd_for_path` (from `vpd_linux.query_ipod_vpd_for_path`) and
`windows_query_ipod_vpd_for_path` (from `vpd_windows.query_ipod_vpd_for_path`).
`vpd_iokit` is never imported by the package; callers import it directly.

Chapter 07's modules are imported by their full module paths and are not part
of this re-export list.

---

## 2. `models` — the model registry

### 2.1 `IPOD_MODELS`

A dict: **model number → `(family, generation, capacity, color)`**, all four
strings. Keys are canonical Apple order numbers without region suffix
(`MA147`, `MB565`, `MKMV2`). The family is always one of `iPod`,
`iPod Classic`, `iPod Mini`, `iPod Nano`, `iPod Shuffle`; U2 editions are a
**color** (`"U2"`), never a family. Recognised generations per family:

| Family | Generations |
|---|---|
| iPod | 1st Gen, 2nd Gen, 3rd Gen, 4th Gen (mono), 4th Gen (photo), 4th Gen (color), 5th Gen, 5.5th Gen |
| iPod Classic | 6th Gen, 6.5th Gen, 7th Gen |
| iPod Mini | 1st Gen, 2nd Gen |
| iPod Nano | 1st Gen … 7th Gen |
| iPod Shuffle | 1st Gen … 4th Gen |

The complete table (data, in table order):

210 rows. Each group lists `model number = capacity / color`, in table order.

| Family | Generation | Rows |
|---|---|---|
| iPod Classic | 6th Gen | `MB029`=80GB/Silver, `MB147`=80GB/Black, `MB145`=160GB/Silver, `MB150`=160GB/Black |
| iPod Classic | 6.5th Gen | `MB562`=120GB/Silver, `MB565`=120GB/Black |
| iPod Classic | 7th Gen | `MC293`=160GB/Silver, `MC297`=160GB/Black |
| iPod | 1st Gen | `M8513`=5GB/White, `M8541`=5GB/White, `M8697`=5GB/White, `M8709`=10GB/White |
| iPod | 2nd Gen | `M8737`=10GB/White, `M8740`=10GB/White, `M8738`=20GB/White, `M8741`=20GB/White |
| iPod | 3rd Gen | `M8976`=10GB/White, `M8946`=15GB/White, `M8948`=30GB/White, `M9244`=20GB/White, `M9245`=40GB/White, `M9460`=15GB/White |
| iPod | 4th Gen (mono) | `M9268`=40GB/White, `M9282`=20GB/White, `ME436`=40GB/White, `M9787`=20GB/U2 |
| iPod | 4th Gen (photo) | `M9585`=40GB/White, `M9586`=60GB/White, `M9829`=30GB/White, `M9830`=60GB/White, `MS492`=30GB/White |
| iPod | 4th Gen (color) | `MA079`=20GB/White, `MA127`=20GB/U2, `MA215`=20GB/White |
| iPod | 5th Gen | `MA002`=30GB/White, `MA003`=60GB/White, `MA146`=30GB/Black, `MA147`=60GB/Black, `MA452`=30GB/U2 |
| iPod | 5.5th Gen | `MA444`=30GB/White, `MA446`=30GB/Black, `MA448`=80GB/White, `MA450`=80GB/Black, `MA664`=30GB/U2 |
| iPod Mini | 1st Gen | `M9160`=4GB/Silver, `M9434`=4GB/Green, `M9435`=4GB/Pink, `M9436`=4GB/Blue, `M9437`=4GB/Gold |
| iPod Mini | 2nd Gen | `M9800`=4GB/Silver, `M9801`=6GB/Silver, `M9802`=4GB/Blue, `M9803`=6GB/Blue, `M9804`=4GB/Pink, `M9805`=6GB/Pink, `M9806`=4GB/Green, `M9807`=6GB/Green |
| iPod Nano | 1st Gen | `MA004`=2GB/White, `MA005`=4GB/White, `MA099`=2GB/Black, `MA107`=4GB/Black, `MA350`=1GB/White, `MA352`=1GB/Black |
| iPod Nano | 2nd Gen | `MA426`=4GB/Silver, `MA428`=4GB/Blue, `MA477`=2GB/Silver, `MA487`=4GB/Green, `MA489`=4GB/Pink, `MA497`=8GB/Black, `MA725`=4GB/Red, `MA726`=8GB/Red, `MA899`=8GB/Red |
| iPod Nano | 3rd Gen | `MA978`=4GB/Silver, `MA980`=8GB/Silver, `MB249`=8GB/Blue, `MB253`=8GB/Green, `MB257`=8GB/Red, `MB261`=8GB/Black, `MB453`=8GB/Pink |
| iPod Nano | 4th Gen | `MB480`=4GB/Silver, `MB651`=4GB/Blue, `MB654`=4GB/Pink, `MB657`=4GB/Purple, `MB660`=4GB/Orange, `MB663`=4GB/Green, `MB666`=4GB/Yellow, `MB598`=8GB/Silver, `MB732`=8GB/Blue, `MB735`=8GB/Pink, `MB739`=8GB/Purple, `MB742`=8GB/Orange, `MB745`=8GB/Green, `MB748`=8GB/Yellow, `MB751`=8GB/Red, `MB754`=8GB/Black, `MB903`=16GB/Silver, `MB905`=16GB/Blue, `MB907`=16GB/Pink, `MB909`=16GB/Purple, `MB911`=16GB/Orange, `MB913`=16GB/Green, `MB915`=16GB/Yellow, `MB917`=16GB/Red, `MB918`=16GB/Black |
| iPod Nano | 5th Gen | `MC027`=8GB/Silver, `MC031`=8GB/Black, `MC034`=8GB/Purple, `MC037`=8GB/Blue, `MC040`=8GB/Green, `MC043`=8GB/Yellow, `MC046`=8GB/Orange, `MC049`=8GB/Red, `MC050`=8GB/Pink, `MC060`=16GB/Silver, `MC062`=16GB/Black, `MC064`=16GB/Purple, `MC066`=16GB/Blue, `MC068`=16GB/Green, `MC070`=16GB/Yellow, `MC072`=16GB/Orange, `MC074`=16GB/Red, `MC075`=16GB/Pink |
| iPod Nano | 6th Gen | `MC525`=8GB/Silver, `MC688`=8GB/Graphite, `MC689`=8GB/Blue, `MC690`=8GB/Green, `MC691`=8GB/Orange, `MC692`=8GB/Pink, `MC693`=8GB/Red, `MC526`=16GB/Silver, `MC694`=16GB/Graphite, `MC695`=16GB/Blue, `MC696`=16GB/Green, `MC697`=16GB/Orange, `MC698`=16GB/Pink, `MC699`=16GB/Red |
| iPod Nano | 7th Gen | `MD475`=16GB/Pink, `MD476`=16GB/Yellow, `MD477`=16GB/Blue, `MD478`=16GB/Green, `MD479`=16GB/Purple, `MD480`=16GB/Silver, `MD481`=16GB/Slate, `MD744`=16GB/Red, `ME971`=16GB/Space Gray, `MKMV2`=16GB/Pink, `MKMX2`=16GB/Gold, `MKN02`=16GB/Blue, `MKN22`=16GB/Silver, `MKN52`=16GB/Space Gray, `MKN72`=16GB/Red |
| iPod Shuffle | 1st Gen | `M9724`=512MB/White, `M9725`=1GB/White |
| iPod Shuffle | 2nd Gen | `MA546`=1GB/Silver, `MA564`=1GB/Silver, `MA947`=1GB/Pink, `MA949`=1GB/Blue, `MA951`=1GB/Green, `MA953`=1GB/Orange, `MB225`=1GB/Silver, `MB227`=1GB/Blue, `MB228`=1GB/Blue, `MB229`=1GB/Green, `MB231`=1GB/Red, `MB233`=1GB/Purple, `MB518`=2GB/Silver, `MB520`=2GB/Blue, `MB522`=2GB/Green, `MB524`=2GB/Red, `MB526`=2GB/Purple, `MB811`=1GB/Pink, `MB813`=1GB/Blue, `MB815`=1GB/Green, `MB817`=1GB/Red, `MB681`=2GB/Pink, `MB683`=2GB/Blue, `MB685`=2GB/Green, `MB779`=2GB/Red, `MC167`=1GB/Gold |
| iPod Shuffle | 3rd Gen | `MB867`=4GB/Silver, `MC164`=4GB/Black, `MC306`=2GB/Silver, `MC323`=2GB/Black, `MC381`=2GB/Green, `MC384`=2GB/Blue, `MC387`=2GB/Pink, `MC303`=4GB/Stainless Steel, `MC307`=4GB/Green, `MC328`=4GB/Blue, `MC331`=4GB/Pink |
| iPod Shuffle | 4th Gen | `MC584`=2GB/Silver, `MC585`=2GB/Pink, `MC749`=2GB/Orange, `MC750`=2GB/Green, `MC751`=2GB/Blue, `MD773`=2GB/Pink, `MD774`=2GB/Yellow, `MD775`=2GB/Blue, `MD776`=2GB/Green, `MD777`=2GB/Purple, `MD778`=2GB/Silver, `MD779`=2GB/Slate, `MD780`=2GB/Red, `ME949`=2GB/Space Gray, `MKM72`=2GB/Pink, `MKM92`=2GB/Gold, `MKME2`=2GB/Blue, `MKMG2`=2GB/Silver, `MKMJ2`=2GB/Space Gray, `MKML2`=2GB/Red |

The 60 GB iPod Video 5G is `MA003` (white) or `MA147` (black).

### 2.2 `canonicalize_model_identity(family, generation, *, capacity="", color="", model_number=None) -> (family, generation, color)`

Returns the canonical family, generation and color.

1. If `model_number` resolves in `IPOD_MODELS` — first as given (stripped,
   upper-cased), then with its first character replaced by `M` (SysInfo
   sometimes stores `xA147` or `P9804`) — the table row wins and its
   family, generation and color are returned.
2. Otherwise the text is normalised for comparison (trimmed, case-folded,
   internal whitespace collapsed) and repaired:
   * family `ipod classic` → `iPod Classic`; generation spellings `6th gen`,
     `6.5 gen`/`6.5th gen`, `7th gen` → `6th Gen`, `6.5th Gen`, `7th Gen`;
     other generations are returned as given (trimmed);
   * families `ipod nano`, `ipod mini`, `ipod shuffle` → `iPod Nano`,
     `iPod Mini`, `iPod Shuffle` with the generation as given;
   * family `ipod` → `iPod`, with generation repairs `4th gen`,
     `4th gen mono`, `4th gen (mono)` → `4th Gen (mono)`; `4th gen photo`,
     `4th gen (photo)` → `4th Gen (photo)`; `4th gen color`,
     `4th gen (color)` → `4th Gen (color)`; `5.5 gen`, `5.5th gen` →
     `5.5th Gen`;
   * any other family is returned trimmed but otherwise unchanged.
3. Color is returned trimmed (empty when not supplied).

Example: `("IPOD CLASSIC", "7TH GEN")` → `("iPod Classic", "7th Gen", "")`;
`("IPOD", "5.5TH GEN")` → `("iPod", "5.5th Gen", "")`.

### 2.3 `USB_PID_TO_MODEL`, `IPOD_USB_PIDS`, `IPOD_RECOVERY_USB_PIDS`

`USB_PID_TO_MODEL` maps an Apple (`0x05AC`) USB product id to
**`(family, generation)`**. An empty generation means the PID is shared by
several generations and only identifies the family ("coarse" PID). Several
PIDs belong to DFU/recovery modes; they are listed in
`IPOD_RECOVERY_USB_PIDS` (a frozenset). `IPOD_USB_PIDS` is the frozenset of
all keys.

| PID | Family | Generation | Recovery |
|---|---|---|---|
| `0x1201` | iPod | 3rd Gen |  |
| `0x1202` | iPod | (empty — coarse) |  |
| `0x1203` | iPod | 4th Gen (mono) |  |
| `0x1204` | iPod | 4th Gen (photo) |  |
| `0x1205` | iPod Mini | (empty — coarse) |  |
| `0x1206` | iPod | (empty — coarse) |  |
| `0x1207` | iPod | (empty — coarse) |  |
| `0x1208` | iPod | (empty — coarse) |  |
| `0x1209` | iPod | (empty — coarse) |  |
| `0x120A` | iPod Nano | (empty — coarse) |  |
| `0x1220` | iPod Nano | 2nd Gen | yes |
| `0x1223` | iPod | (empty — coarse) | yes |
| `0x1224` | iPod Nano | 3rd Gen | yes |
| `0x1225` | iPod Nano | 4th Gen | yes |
| `0x1231` | iPod Nano | 5th Gen | yes |
| `0x1232` | iPod Nano | 6th Gen | yes |
| `0x1233` | iPod Shuffle | 4th Gen | yes |
| `0x1234` | iPod Nano | 7th Gen | yes |
| `0x1240` | iPod Nano | 2nd Gen | yes |
| `0x1241` | iPod Classic | 6th Gen | yes |
| `0x1242` | iPod Nano | 3rd Gen | yes |
| `0x1243` | iPod Nano | 4th Gen | yes |
| `0x1245` | iPod Classic | 6.5th Gen | yes |
| `0x1246` | iPod Nano | 5th Gen | yes |
| `0x1247` | iPod Classic | 7th Gen | yes |
| `0x1248` | iPod Nano | 6th Gen | yes |
| `0x1249` | iPod Nano | 7th Gen | yes |
| `0x124A` | iPod Nano | 7th Gen | yes |
| `0x1255` | iPod Nano | 4th Gen | yes |
| `0x1260` | iPod Nano | 2nd Gen |  |
| `0x1261` | iPod Classic | (empty — coarse) |  |
| `0x1262` | iPod Nano | 3rd Gen |  |
| `0x1263` | iPod Nano | 4th Gen |  |
| `0x1265` | iPod Nano | 5th Gen |  |
| `0x1266` | iPod Nano | 6th Gen |  |
| `0x1267` | iPod Nano | 7th Gen |  |
| `0x1300` | iPod Shuffle | 1st Gen |  |
| `0x1301` | iPod Shuffle | 2nd Gen |  |
| `0x1302` | iPod Shuffle | 3rd Gen |  |
| `0x1303` | iPod Shuffle | 4th Gen |  |

The iPod Video (5th and 5.5th Gen) reports the coarse PID `0x1209`, which
only says "iPod".

### 2.4 `SERIAL_SUFFIX_TO_MODEL` / `SERIAL_LAST3_TO_MODEL`

A dict: **serial-number suffix → model number**. Keys are upper-case
alphanumerics of length 3 or 4; no 3-character key equals the last three
characters of any 4-character key. `SERIAL_LAST3_TO_MODEL` is the **same
dict object** (legacy alias).

288 keys (57 of length 4, the rest length 3), in table order grouped by target model.

| Model | Suffixes |
|---|---|
| `MB029` | `Y5N` |
| `MB147` | `YMV` |
| `MB145` | `YMU` |
| `MB150` | `YMX` |
| `MB562` | `2C5` |
| `MB565` | `2C7` |
| `MC293` | `9ZS` |
| `MC297` | `9ZU` |
| `M8541` | `LG6`, `NAM`, `MJ2` |
| `M8709` | `ML1`, `MME` |
| `M8737` | `MMB` |
| `M8738` | `MMC` |
| `M8740` | `NGE`, `NGH` |
| `M8741` | `MMF` |
| `M8946` | `NLW` |
| `M8976` | `NRH` |
| `M9460` | `QQF` |
| `M9244` | `PQ5`, `PNT` |
| `M8948` | `NLY`, `NM7` |
| `M9245` | `PNU` |
| `M9282` | `PS9`, `Q8U` |
| `M9268` | `PQ7` |
| `M9787` | `S2X` |
| `MA079` | `TDU`, `TDS` |
| `MA127` | `TM2` |
| `MA215` | `U5H` |
| `M9830` | `SAZ`, `SB1` |
| `M9829` | `SAY` |
| `M9585` | `R5Q` |
| `M9586` | `R5R`, `R5T` |
| `M9160` | `PFW`, `PRC` |
| `M9436` | `QKL`, `QKQ` |
| `M9435` | `QKK`, `QKP` |
| `M9434` | `QKJ`, `QKN` |
| `M9437` | `QKM`, `QKR` |
| `M9800` | `S41`, `S4C` |
| `M9802` | `S43` |
| `M9804` | `S45` |
| `M9805` | `S4G`, `S4H` |
| `M9806` | `S47`, `S4J` |
| `M9801` | `S42` |
| `M9803` | `S44` |
| `M9807` | `S48` |
| `M9724` | `RS9`, `QGV`, `TSX`, `PFV`, `R80` |
| `M9725` | `RSA`, `TSY`, `C60` |
| `MA546` | `VTE`, `VTF` |
| `MA947` | `XQ5`, `XQS` |
| `MA949` | `XQV`, `XQX` |
| `MB227` | `YX7`, `YXH` |
| `MA951` | `XQY`, `YX8` |
| `MA953` | `XR1` |
| `MB233` | `YXA`, `YXL` |
| `MB225` | `YX6`, `YX9` |
| `MB229` | `YXJ` |
| `MB231` | `YXK` |
| `MC167` | `8CQ` |
| `MB518` | `1ZH` |
| `MB520` | `1ZK` |
| `MB522` | `1ZM` |
| `MB524` | `1ZP` |
| `MB526` | `1ZR` |
| `MB811` | `436` |
| `MB681` | `3FK` |
| `MB813` | `437` |
| `MB683` | `3FL` |
| `MB815` | `438` |
| `MB685` | `3FM` |
| `MB817` | `439` |
| `MB779` | `3W6` |
| `MC306` | `A1S` |
| `MC323` | `A78` |
| `MC381` | `ALB` |
| `MC384` | `ALD` |
| `MC387` | `ALG` |
| `MB867` | `4NZ` |
| `MC164` | `891` |
| `MC303` | `A1L` |
| `MC307` | `A1U` |
| `MC328` | `A7B` |
| `MC331` | `A7D` |
| `MC584` | `DCMJ` |
| `MC585` | `DCMK` |
| `MC749` | `DFDM` |
| `MC750` | `DFDN` |
| `MC751` | `DFDP` |
| `MD773` | `F4RT` |
| `MD774` | `F4RV` |
| `MD775` | `F4RW` |
| `MD776` | `F4RY` |
| `MD777` | `F4T0` |
| `MD778` | `F4T1` |
| `MD779` | `F4VF` |
| `MD780` | `F4VG` |
| `ME949` | `FJDH` |
| `MKM72` | `GK67` |
| `MKM92` | `GK68` |
| `MKME2` | `GK69` |
| `MKMG2` | `GK6C` |
| `MKMJ2` | `GK6D` |
| `MKML2` | `GK6F` |
| `MA004` | `TUZ`, `SZB`, `SZV`, `SZW` |
| `MA005` | `TV0`, `SZC`, `SZT` |
| `MA099` | `TUY`, `TJT`, `TJU` |
| `MA107` | `TV1`, `TK2`, `TK3` |
| `MA350` | `UYN`, `UNA`, `UNB` |
| `MA352` | `UYP`, `UPR`, `UPS` |
| `MA477` | `VQ5`, `VQ6` |
| `MA426` | `V8T`, `V8U` |
| `MA428` | `V8W`, `V8X` |
| `MA487` | `VQH`, `VQJ` |
| `MA489` | `VQK`, `VQL`, `VKL` |
| `MA725` | `WL2`, `WL3` |
| `MA726` | `X9A`, `X9B` |
| `MA497` | `VQT`, `VQU` |
| `MA899` | `YER`, `YES` |
| `MA978` | `Y0P` |
| `MA980` | `Y0R` |
| `MB249` | `YXR` |
| `MB257` | `YXV` |
| `MB253` | `YXT` |
| `MB261` | `YXX` |
| `MB453` | `13F` |
| `MB663` | `37P` |
| `MB666` | `37Q` |
| `MB651` | `37G` |
| `MB654` | `37H` |
| `MB480` | `1P1` |
| `MB657` | `37K` |
| `MB660` | `37L` |
| `MB598` | `2ME` |
| `MB732` | `3QS` |
| `MB735` | `3QT` |
| `MB739` | `3QU` |
| `MB742` | `3QW` |
| `MB745` | `3QX` |
| `MB748` | `3QY` |
| `MB754` | `3R0` |
| `MB751` | `3QZ` |
| `MB903` | `5B7` |
| `MB905` | `5B8` |
| `MB907` | `5B9` |
| `MB909` | `5BA` |
| `MB911` | `5BB` |
| `MB913` | `5BC` |
| `MB915` | `5BD` |
| `MB917` | `5BE` |
| `MB918` | `5BF` |
| `MC027` | `71V` |
| `MC031` | `71Y` |
| `MC034` | `721` |
| `MC037` | `726` |
| `MC040` | `72A` |
| `MC043` | `72D` |
| `MC046` | `72F` |
| `MC049` | `72K` |
| `MC050` | `72L` |
| `MC060` | `72Q` |
| `MC062` | `72R` |
| `MC064` | `72S` |
| `MC066` | `72X` |
| `MC068` | `734` |
| `MC070` | `738` |
| `MC072` | `739` |
| `MC074` | `73A` |
| `MC075` | `73B` |
| `MC525` | `DCMN` |
| `MC526` | `DCMP` |
| `MC688` | `DDVX` |
| `MC689` | `DDVY` |
| `MC690` | `DDW0` |
| `MC691` | `DDW1` |
| `MC692` | `DDW2` |
| `MC693` | `DDW3` |
| `MC694` | `DDW4` |
| `MC695` | `DDW5` |
| `MC696` | `DDW6` |
| `MC697` | `DDW7` |
| `MC698` | `DDW8` |
| `MC699` | `DDW9` |
| `MD475` | `F0GD`, `F0GM` |
| `MD476` | `F0GF`, `F0GN` |
| `MD477` | `F0GG`, `F0GP` |
| `MD478` | `F0GH`, `F0GQ` |
| `MD479` | `F0GJ`, `F0GR` |
| `MD480` | `F0GK`, `F0GT` |
| `MD481` | `F0GL`, `F0GV` |
| `MD744` | `F4LN`, `F4LP` |
| `ME971` | `FJQ1` |
| `MKMV2` | `GK60` |
| `MKMX2` | `GK61` |
| `MKN02` | `GK62` |
| `MKN22` | `GK63` |
| `MKN52` | `GK64` |
| `MKN72` | `GK65` |
| `MA002` | `SZ9`, `WEC`, `WED`, `WEG`, `WEH`, `WEL` |
| `MA146` | `TXK`, `TXM`, `WEF`, `WEJ`, `WEK` |
| `MA003` | `SZA`, `SZU` |
| `MA147` | `TXL`, `TXN` |
| `MA452` | `V9V` |
| `MA444` | `V9K`, `V9L`, `WU9` |
| `MA446` | `VQM`, `V9M`, `V9N`, `WEE` |
| `MA448` | `V9P`, `V9Q` |
| `MA450` | `V9R`, `V9S`, `V95`, `V96`, `WUC` |
| `MA664` | `W9G`, `WEM` |

---

## 3. `lookup` — lookups over the registry

| Function | Contract |
|---|---|
| `extract_model_number(model_str)` | Normalises a SysInfo `ModelNumStr`. Empty → `None`. A leading lower-case `x` becomes `M`. Then the value is upper-cased and matched against the pattern "`M`, optionally one letter, then 3–4 digits" from the start; a match returns that part (`"xA623"` → `"MA623"`, `"M9282"` → `"M9282"`). If that fails, the first character is replaced by `M` and the match is retried (`"P9804"` → `"M9804"`). If both fail: the first five characters upper-cased when the input has at least five, else the whole input upper-cased. |
| `get_model_info(model_number)` | `IPOD_MODELS` row for the number; else, if the number does not start with `M`, the row for the number with its first character replaced by `M`; else the first table row (in table order) whose key's first four characters are a prefix of the input; else `None`. Falsy input → `None`. |
| `get_friendly_model_name(model_number)` | With a row: family, generation (when non-empty), capacity (when non-empty) and color (when non-empty) joined by single spaces — `"MA664"` → `"iPod 5.5th Gen 30GB U2"`. Without a row: `"Unknown iPod (<number>)"`, or `"Unknown iPod"` for empty input. |
| `match_serial_suffix(serial)` | Trims and upper-cases the serial; tries suffix lengths present in the table from longest to shortest; returns the first tail that is a key, else `None`. A 4-character key therefore wins over a 3-character key. |
| `lookup_by_serial(serial)` | `(model_number, (family, generation, capacity, color))` for the matched suffix, or `None` when no suffix matches or the model has no row. |
| `infer_generation(family, capacity="")` | If the capability table (§4.3) has exactly one generation for the family, that generation. Otherwise, if a capacity is given and exactly one generation of the family is sold with that capacity in `IPOD_MODELS`, that generation. Otherwise `None`. |
| `usb_pid_identity_conflicts(model_family, generation, pid_family, pid_generation)` | Whether an exact model identity is impossible for a PID identity. Both sides are canonicalised (§2.2) and compared case-insensitively. No conflict when either family is empty, or when the PID side is family `iPod` with an empty generation. Different families conflict. Same family: no conflict if either generation is empty or they are equal; a PID generation `5th Gen` accepts a model `5.5th Gen`; a PID generation `4th Gen (photo)` accepts `4th Gen (photo)` and `4th Gen (color)`; everything else conflicts. |

---

## 4. `capabilities` — what a model can do

### 4.1 `DeviceCapabilities`

A frozen dataclass. Every field has a default; a table row (§4.3) overrides
only what differs. Fields in declaration order:

| Field | Default |
|---|---|
| `checksum` | `NONE` |
| `is_shuffle` | False |
| `shadow_db_version` | 0 |
| `supports_compressed_db` | False |
| `supports_video` | False |
| `supports_tx3g_subtitles` | False |
| `supports_cea608_captions` | False |
| `supports_podcast` | True |
| `supports_gapless` | False |
| `supports_artwork` | True |
| `supports_photo` | False |
| `photo_formats` | () |
| `supports_chapter_image` | False |
| `supports_sparse_artwork` | False |
| `supports_alac` | True |
| `cover_art_formats` | () |
| `music_dirs` | 20 |
| `max_database_bytes` | 32 MiB |
| `uses_sqlite_db` | False |
| `db_version` | 0x30 |
| `byte_order` | "le" |
| `has_screen` | True |
| `max_video_width` | 0 |
| `max_video_height` | 0 |
| `max_video_fps` | 30 |
| `max_video_bitrate` | 0 |
| `h264_level` | "3.0" |

Meaning of the less obvious fields:

| Field | Meaning |
|---|---|
| `checksum` | `ChecksumType` the firmware requires on the database (§5). |
| `is_shuffle`, `shadow_db_version` | Shuffle models; `1` = first iTunesSD format, `2` = second. iTunesSD itself is out of scope (chapter 01 §5). |
| `supports_compressed_db` | Firmware reads `iTunesCDB` (zlib) instead of `iTunesDB`. |
| `supports_tx3g_subtitles`, `supports_cea608_captions` | Independent of `supports_video`: the 5th/5.5th Gen play video but show neither. |
| `supports_podcast` | Datasets of podcast type (MHSD 3) are written. |
| `supports_gapless` | Gapless fields in MHIT are meaningful (from 5.5th Gen). |
| `supports_artwork`, `cover_art_formats` | Cover formats as `ArtworkFormat` objects (chapter 05 §4), in the order listed. |
| `supports_photo`, `photo_formats` | Photo-database formats; photos are out of scope, the values are data only. |
| `music_dirs` | Number of `Fxx` folders under `iPod_Control/Music`. |
| `max_database_bytes` | Ceiling for the database file size used by the writer's preflight. |
| `uses_sqlite_db` | Firmware reads SQLite databases; podsync refuses to write such devices (chapter 08 §7.3.3). |
| `db_version` | Minimum database version the writer emits (chapter 04 §5.1); also selects the MHIT header size. |
| `byte_order` | Always `"le"` for the models in the table. |
| `max_video_*`, `h264_level` | Video decode limits; data only (transcoding is out of scope). |

### 4.2 Database-size rule

`_DEFAULT_MAX_DATABASE_BYTES` = 32 MiB, `_LARGE_MAX_DATABASE_BYTES` = 64 MiB.
Rows default to 32 MiB; the rows in §4.3 that say 64 MiB carry it directly.
In addition, for family `iPod` with generation `5th Gen` or `5.5th Gen`, the
limit is raised to 64 MiB when either the capacity is at least 60 GB or the
model number (trimmed, upper-cased) is one of `MA003`, `MA147`, `MA448`,
`MA450` (the set `_HIGH_MEMORY_VIDEO_MODELS`). The capacity number is the
first run of digits in the capacity string (`"60GB"` → 60; no digits → 0).
Examples: iPod 5.5th Gen 30GB → 32 MiB; iPod 5.5th Gen 80GB → 64 MiB;
iPod 5th Gen 60GB → 64 MiB.

### 4.3 `_FAMILY_GEN_CAPABILITIES`

Keyed by `(family, generation)`. Cover and photo formats are listed by format
id and resolve through the global registry of chapter 05 §4.2, except the
Nano 7th Gen cover list, which uses `NANO_7G_COVER_ART_OVERRIDES`
(1010 global, 1013/1015/1016 overridden). Order inside each list matters.

| Family | Generation | Overrides of the defaults |
|---|---|---|
| iPod | 1st Gen | supports_podcast=False; supports_artwork=False; db_version=0x13 |
| iPod | 2nd Gen | supports_podcast=False; supports_artwork=False; db_version=0x13 |
| iPod | 3rd Gen | supports_podcast=False; supports_artwork=False; db_version=0x13 |
| iPod | 4th Gen (mono) | supports_artwork=False; db_version=0x13 |
| iPod | 4th Gen (photo) | supports_photo=True; photo_formats=(1009, 1013, 1015, 1019); cover_art_formats=(1017, 1016); db_version=0x13 |
| iPod | 4th Gen (color) | supports_photo=True; photo_formats=(1009, 1013, 1015, 1019); cover_art_formats=(1017, 1016); db_version=0x13 |
| iPod | 5th Gen | supports_video=True; supports_photo=True; photo_formats=(1036, 1024, 1015, 1019); cover_art_formats=(1028, 1029); db_version=0x19; max_video_width=640; max_video_height=480 |
| iPod | 5.5th Gen | supports_video=True; supports_gapless=True; supports_photo=True; photo_formats=(1036, 1024, 1015, 1019); cover_art_formats=(1028, 1029); db_version=0x19; max_video_width=640; max_video_height=480 |
| iPod Classic | 6th Gen | checksum=`HASH58`; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1067, 1024, 1066); supports_chapter_image=True; supports_sparse_artwork=True; cover_art_formats=(1055, 1060, 1061, 1068); music_dirs=50; max_database_bytes=64 MiB; max_video_width=640; max_video_height=480; max_video_bitrate=2500 |
| iPod Classic | 6.5th Gen | checksum=`HASH58`; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1067, 1024, 1066); supports_chapter_image=True; supports_sparse_artwork=True; cover_art_formats=(1055, 1060, 1061, 1068); music_dirs=50; max_database_bytes=64 MiB; max_video_width=640; max_video_height=480; max_video_bitrate=2500 |
| iPod Classic | 7th Gen | checksum=`HASH58`; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1067, 1024, 1066); supports_chapter_image=True; supports_sparse_artwork=True; cover_art_formats=(1055, 1060, 1061, 1068); music_dirs=50; max_database_bytes=64 MiB; max_video_width=640; max_video_height=480; max_video_bitrate=2500 |
| iPod Mini | 1st Gen | supports_artwork=False; music_dirs=6; db_version=0x13 |
| iPod Mini | 2nd Gen | supports_artwork=False; music_dirs=6; db_version=0x13 |
| iPod Nano | 1st Gen | supports_photo=True; photo_formats=(1032, 1023); cover_art_formats=(1031, 1027); music_dirs=14; db_version=0x13 |
| iPod Nano | 2nd Gen | supports_photo=True; photo_formats=(1032, 1023); cover_art_formats=(1031, 1027); music_dirs=14; db_version=0x13 |
| iPod Nano | 3rd Gen | checksum=`HASH58`; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1067, 1024, 1066); supports_sparse_artwork=True; cover_art_formats=(1061, 1055, 1068, 1060); max_video_width=320; max_video_height=240; max_video_bitrate=768; h264_level="1.3" |
| iPod Nano | 4th Gen | checksum=`HASH58`; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1024, 1066, 1079, 1083); supports_chapter_image=True; supports_sparse_artwork=True; cover_art_formats=(1055, 1068, 1071, 1074, 1078, 1084); max_video_width=480; max_video_height=320; max_video_bitrate=768; h264_level="1.3" |
| iPod Nano | 5th Gen | checksum=`HASH72`; supports_compressed_db=True; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1087, 1079, 1066); supports_sparse_artwork=True; cover_art_formats=(1056, 1078, 1073, 1074); music_dirs=14; max_database_bytes=64 MiB; uses_sqlite_db=True; max_video_width=640; max_video_height=480 |
| iPod Nano | 6th Gen | checksum=`HASHAB`; supports_compressed_db=True; supports_gapless=True; supports_photo=True; photo_formats=(1092, 1093); supports_sparse_artwork=True; cover_art_formats=(1073, 1085, 1089, 1074); max_database_bytes=64 MiB; uses_sqlite_db=True |
| iPod Nano | 7th Gen | checksum=`HASHAB`; supports_compressed_db=True; supports_video=True; supports_tx3g_subtitles=True; supports_cea608_captions=True; supports_gapless=True; supports_photo=True; photo_formats=(1007, 1005); supports_sparse_artwork=True; cover_art_formats=(1010, 1013, 1015, 1016); max_database_bytes=64 MiB; uses_sqlite_db=True; max_video_width=720; max_video_height=576 |
| iPod Shuffle | 1st Gen | is_shuffle=True; shadow_db_version=1; supports_artwork=False; music_dirs=3; db_version=0xc; has_screen=False |
| iPod Shuffle | 2nd Gen | is_shuffle=True; shadow_db_version=1; supports_artwork=False; music_dirs=3; db_version=0x13; has_screen=False |
| iPod Shuffle | 3rd Gen | is_shuffle=True; shadow_db_version=2; supports_artwork=False; music_dirs=3; db_version=0x19; has_screen=False |
| iPod Shuffle | 4th Gen | is_shuffle=True; shadow_db_version=2; supports_artwork=False; music_dirs=3; db_version=0x19; has_screen=False |

For the user's device (iPod 5th Gen 60 GB): no database checksum, database
version 0x19 (MHIT header 0x148 unless an existing database has a higher
version), video yes, gapless no, covers 1028 (100×100) and 1029 (200×200)
RGB565, 20 music folders, 64 MiB database ceiling.

### 4.4 Lookup functions

All three first canonicalise the pair with `canonicalize_model_identity`
(passing `capacity` and `model_number` where they accept them).

| Function | Contract |
|---|---|
| `capabilities_for_family_gen(family, generation, *, capacity=None, model_number=None)` | The table row with the §4.2 rule applied. If there is no row and the generation is empty, and every row of that family is identical, that shared row (with §4.2 applied). Otherwise `None`. Every model in `IPOD_MODELS` must resolve to a row. |
| `cover_art_formats_for_family_gen(family, generation, *, capacity=None, model_number=None)` | The row's `cover_art_formats`, or `()` when the row has `supports_artwork` false. Without a row and with an empty generation: the family's shared cover tuple when all rows of the family agree. Otherwise `()`. |
| `checksum_type_for_family_gen(family, generation)` | The row's `checksum`. Without a row and with an empty generation: the family's checksum when all rows of the family agree. Otherwise `None`. |

---

## 5. `checksum`

`ChecksumType` is an `IntEnum`:

| Name | Value | Used by |
|---|---|---|
| `NONE` | 0 | iPod 1G–5.5G, Mini, Nano 1G–2G, Shuffle |
| `HASH58` | 1 | iPod Classic (all), Nano 3G, Nano 4G |
| `HASH72` | 2 | Nano 5G |
| `HASHAB` | 3 | Nano 6G, Nano 7G (unsupported by the podsync writer, chapter 04 §6.4) |
| `UNSUPPORTED` | 98 | reserved |
| `UNKNOWN` | 99 | not identified |

`CHECKSUM_MHBD_SCHEME` maps the enum to the MHBD `hashing_scheme` word:
NONE → 0, HASH58 → 1, HASH72 → 2, **HASHAB → 4** (enum 3, wire 4).
`MHBD_SCHEME_TO_CHECKSUM` is its inverse.

---

## 6. Artwork format registries (`artwork_presets`, `artwork`)

`artwork_presets` holds the data defined in chapter 05 §4.1–§4.2:
`ArtworkFormat`, `ARTWORK_FORMATS_BY_ID`, `CLASSIC_COVER_ART_FORMATS`,
`NANO_7G_COVER_ART_OVERRIDES`, `NANO_7G_COVER_ART_FORMATS` (same object as
the overrides) and `artwork_format_candidates()`. Chapter 05 is the
authority for the values.

`artwork`:

| Name | Contract |
|---|---|
| `ITHMB_FORMAT_MAP` | The same object as `ARTWORK_FORMATS_BY_ID`. |
| `ITHMB_SIZE_MAP` | Built at import: for each registry entry in order, frame size = `row_bytes × height`; entries with size > 0 are added under that size unless the size is already present. |
| `cover_art_format_definitions_for_device(family, generation, *, capacity=None, model_number=None)` | `{format_id: ArtworkFormat}`: from the capability row's cover formats; `{}` when the row has no artwork; when there is no row, from `cover_art_formats_for_family_gen`. |
| `ithmb_formats_for_device(family, generation, *, capacity=None, model_number=None)` | Same selection as `{format_id: (width, height)}`. iPod 5th Gen → keys `[1028, 1029]`; iPod Classic 6th Gen → `[1055, 1060, 1061, 1068]`. |
| `resolve_cover_art_format_definitions(family="", generation="", *, capacity=None, model_number=None, observed_formats=None)` | Without `observed_formats`: the device definitions above. With `observed_formats` (`{id: (width, height)}`, e.g. from SysInfoExtended or an existing ArtworkDB): the observed id list is authoritative; each id resolves to the device definition for that id if its width and height match exactly, else to the global registry entry if that matches exactly, else to a generic definition `ArtworkFormat(id, width, height, width × 2, "RGB565_LE", "cover", "Device artwork format <id>")`. |
| `resolve_cover_art_format_definitions_for_device(device)` | `{}` for `None`; otherwise the function above with the device's family, generation, capacity, model number and, when non-empty, its `artwork_formats` as observed formats. |
| `photo_formats_for_device(family, generation, *, capacity=None, model_number=None)` | `{id: ArtworkFormat}` from the row's `photo_formats`, `{}` when none. |

---

## 7. `DeviceInfo` and the current-device store (`info`)

### 7.1 `DeviceInfo`

A mutable dataclass. Everything has a default; unknown values stay at the
default. Fields in order:

| Group | Field | Type | Default |
|---|---|---|---|
| Identity | `path` | str | `""` (mount root, e.g. `D:\`) |
| | `mount_name` | str | `""` (volume label or `D:`) |
| | `ipod_name` | str | `""` (title of the master playlist) |
| | `model_number` | str | `""` (normalised, e.g. `MA147`) |
| | `model_family` | str | `"iPod"` (sentinel: family not resolved) |
| | `generation`, `capacity`, `color` | str | `""` |
| Hardware | `firewire_guid` | str | `""` (16 hex digits) |
| | `serial` | str | `""` (Apple product serial, never the GUID) |
| | `firmware`, `board` | str | `""` |
| | `family_id`, `updater_family_id` | int or str | `0` |
| | `product_type` | str | `""` |
| | `usb_pid`, `usb_vid` | int | `0` |
| | `usb_serial`, `usbstor_instance_id`, `usb_parent_instance_id`, `usb_grandparent_instance_id` | str | `""` |
| | `scsi_vendor`, `scsi_product`, `scsi_revision`, `connected_bus` | str | `""` |
| | `reported_volume_format` | str | `""` (SysInfoExtended hint) |
| | `filesystem_type` | str | `""` (host-observed filesystem) |
| | `volume_identity_key` | str | `""` (chapter 07 volume lock key) |
| Reported capabilities | `db_version`, `shadow_db_version` | int | `0` |
| | `uses_sqlite_db`, `supports_sparse_artwork` | bool | `False` |
| | `max_tracks`, `max_transfer_speed` | int | `0` |
| | `max_file_size_gb` | int or float | `0` |
| | `podcasts_supported`, `voice_memos_supported` | bool | `False` |
| | `audio_codecs`, `power_information`, `apple_drm_version` | dict | `{}` |
| Hashing | `checksum_type` | int | `99` (UNKNOWN) |
| | `hashing_scheme` | int | `-1` (not read) |
| | `hash_info_iv` | bytes | `b""` (16 bytes when known) |
| | `hash_info_rndpart` | bytes | `b""` (12 bytes when known) |
| Storage | `disk_size_gb`, `free_space_gb` | float | `0.0` |
| Artwork | `artwork_formats`, `photo_formats`, `chapter_image_formats` | dict `{id: (w, h)}` | `{}` |
| Evidence | `sysinfo` | dict | `{}` (parsed SysInfo) |
| | `raw_identity_evidence` | dict of lists | `{}` |
| | `identity_conflicts` | list of dicts | `[]` |
| Provenance | `identification_method` | str | `"unknown"` |
| | `_field_sources` | dict field → source | `{}`; not an init argument, not in `repr` |

Properties:

| Property | Behavior |
|---|---|
| `firewire_id_bytes` | The GUID (an optional `0x` prefix removed) decoded from hex; `None` when empty, not hex, or all zero bytes. |
| `drive_letter` | On Windows, the first character of `path` when it is a letter; otherwise `""`. |
| `volume_format` | Legacy alias reading/writing `reported_volume_format`. |
| `display_name` | Family, then generation, capacity and color when non-empty, joined by spaces. |
| `subtitle` | `mount_name` (when set) and, when the disk size is known, `"<free> of <total> GB free"` (one decimal each), joined by `" — "`; `""` when neither. |
| `icon` | An emoji: 📱 for any family containing "classic" and for family `iPod` with generation 4th Gen (photo), 4th Gen (color), 5th Gen or 5.5th Gen; 🎵 for nano; 🔀 for shuffle; 🎶 for mini; 🎵 otherwise. Comparisons are case-insensitive. |
| `capabilities` | The capability row for the device's family, generation, capacity and model number (or a default `DeviceCapabilities` when there is none), with device-reported overrides: a non-zero `db_version` or `shadow_db_version` replaces the row value; `uses_sqlite_db`, `supports_sparse_artwork` and `podcasts_supported` (→ `supports_podcast`) replace the row value only when that field has a recorded source in `_field_sources`. |

### 7.2 Current device

A process-wide holder of the **selected** device, created lazily and
thread-safely.

| Function | Contract |
|---|---|
| `get_current_device()` | The stored `DeviceInfo` or `None`. |
| `get_current_device_for_path(path)` | The stored device only when its `path` is non-empty and resolves (real path, OS case-normalised) to the same location as `path`; otherwise `None`. Any error while resolving → `None`. |
| `set_current_device(info)` | `None` clears the store. A device must have a non-empty `model_number`, otherwise `UnidentifiedDeviceError` (§7.3). Logs one INFO line describing the stored device (family, generation, model, the last four serial characters, GUID, checksum type, method, capacity, artwork ids) or `Device cleared`. |
| `clear_current_device()` | Same as storing `None`. |

### 7.3 `UnidentifiedDeviceError`, `has_exact_model_number`, `require_exact_model_number`

`UnidentifiedDeviceError` is a `ValueError`. `has_exact_model_number(info)` is
true when the object's `model_number` attribute is a non-blank string.
`require_exact_model_number(info)` raises
`UnidentifiedDeviceError("Refusing to activate unidentified iPod at <path>: no exact model number was resolved")`
(path `"unknown mount"` when empty) when that is false.

### 7.4 Database file names

| Function | Contract |
|---|---|
| `resolve_itdb_path(ipod_path)` | Path of the database to **read**, or `None`. When the stored current device matches `ipod_path` and its capabilities are known, the "required" name is `iTunesCDB` if `supports_compressed_db` else `iTunesDB`, and the other is the "alternate". The first of (required, alternate) that is a non-empty regular file is returned; choosing the alternate logs a WARNING that it is used as the recovery source until the next guarded write restores the required name. If neither is non-empty, the first that merely exists is returned. Without a matching device: the first **non-empty** file of `iTunesCDB`, `iTunesDB`, then the first existing one. Any `OSError` while inspecting candidates in the device branch → `DeviceWriteSafetyError("Could not safely inspect the iPod database filenames: <exc>")`. |
| `itdb_write_filename(ipod_path)` | Name of the database to **write**: the required name when a matching device with capabilities exists; else the basename of the resolved database when it is non-empty; else `"iTunesDB"`. |

A zero-byte file with the other name is a deliberate firmware marker left by
the writer (chapter 04 §7 step 12) and must never be preferred over a
non-empty database.

### 7.5 `read_sysinfo(ipod_path)`

Reads `iPod_Control/Device/SysInfo` as text (undecodable bytes ignored) and
parses it with `parse_sysinfo_text` (§8.1). A missing file raises
`FileNotFoundError("SysInfo not found at <path>")`.

### 7.6 `detect_checksum_type(ipod_path)`

Decides the checksum a database write needs, in this order:

1. A stored current device for this path with `checksum_type` ≠ 99 → that.
2. A virtual iPod at the path whose loaded `checksum_type` ≠ 99 → that
   (errors in this step are ignored).
3. SysInfo missing → `UNKNOWN`.
4. `ModelNumStr` → `extract_model_number` → `get_model_info` →
   `checksum_type_for_family_gen`; a non-`None` result → that.
5. `iPod_Control/Device/HashInfo` exists → `HASH72`. An `OSError` other than
   "not found" while checking → `DeviceWriteSafetyError("Could not inspect the iPod HashInfo checksum material: <exc>")`.
6. Otherwise `UNKNOWN` (also when `visibleBuildID` has major version ≥ 2 or
   a `FirewireGuid` is present). Empty or unidentifiable metadata is never
   guessed as `NONE`.

### 7.7 `get_firewire_id(ipod_path, *, known_guid=None) -> bytes`

Sources in order; each yields bytes only when the hex decodes and is not all
zeros (an optional `0x` prefix is removed first):

1. `known_guid`;
2. the stored current device for the path (`firewire_id_bytes`);
3. a virtual iPod's metadata at the path (errors ignored);
4. SysInfo `FirewireGuid`;
5. SysInfoExtended: the first `<key>FireWireGUID</key>` followed by a
   `<string>` of hex digits.

Nothing found → `RuntimeError` whose message lists the four source kinds and
ends with "Connect the iPod and try again."

---

## 8. `sysinfo` — parsing SysInfo and SysInfoExtended

### 8.1 SysInfo text

`parse_sysinfo_text(content) -> dict[str, str]`: each line is trimmed; blank
lines, lines starting with `#`, and lines without `:` are skipped; the rest is
split at the **first** colon into key and value, both trimmed. Later
duplicates overwrite earlier ones.

`identity_from_sysinfo(sysinfo, source="sysinfo") -> dict` returns DeviceInfo
field values plus `_sources` (field → `source`) for present keys:

| SysInfo key | Field | Rule |
|---|---|---|
| `BoardHwName` | `board` | as is |
| `pszSerialNumber` | `serial` | trimmed |
| `FirewireGuid` | `firewire_guid` | `normalize_guid` (below); dropped when empty |
| `visibleBuildID`, else `VisibleBuildID`, else `BuildID` | `firmware` | first present |
| `ModelNumStr` | `model_number` | `extract_model_number`; the raw value is also returned under `model_raw` |
| `ModelFamily`, `Generation`, `Capacity`, `Color` | same names in snake case | as is |
| `USBProductID` | `usb_pid` | integer with base prefix detection (`0x1261` or `4705`); unparsable → absent |
| `FamilyID` (or `iPodFamily`), `UpdaterFamilyID` | `family_id`, `updater_family_id` | leading numeric token of the value parsed with base detection (`"0x00000003 (3.0 0)"` → 3); non-numeric → the text itself; empty → 0 |

`normalize_guid(value)`: `None` → `""`; otherwise trimmed, spaces removed, an
optional `0x` prefix removed; empty or all-`0` → `""`; not valid hex →
`""`; else the **upper-case** hex string (any even length).

### 8.2 SysInfoExtended

SysInfoExtended is an XML property list. The same payload may come from the
file, from SCSI VPD pages, or from the USB vendor command, and may carry
leading junk, trailing NULs or a missing end.

`parse_sysinfo_extended(content, *, source="sysinfo_extended", live=False) -> ParsedSysInfoExtended`:

1. Text is encoded as UTF-8. The bytes are stripped of NUL/CR/LF/TAB/space at
   both ends; if `<?xml` occurs, everything before it is dropped, else if
   `<plist` occurs, everything before that; trailing NULs are removed.
2. The plist parser is tried on the bytes; if `</plist>` is missing it is
   also tried on the bytes with a closing `</dict></plist>` appended. The
   first attempt that yields a dict wins (and its bytes become `raw_xml`).
3. If no dict results, a tolerant scan extracts `<key>K</key>` followed by
   `<string>`, `<integer>` (base detection; kept as text when unparsable),
   `<true/>` or `<false/>`; `used_regex_fallback` is true when that scan found
   anything.

`ParsedSysInfoExtended` (dataclass): `plist: dict`, `raw_xml: bytes = b""`,
`source = "sysinfo_extended"`, `live = False`, `used_regex_fallback = False`;
properties `identity` (= `identity_from_sysinfo_extended(self, source, live=live)`),
`cover_art_formats`, `photo_formats`, `chapter_image_formats`
(`extract_image_formats` with the key lists below).

`identity_from_sysinfo_extended(parsed_or_plist, source="sysinfo_extended", *, live=False)`
returns DeviceInfo field values with `_sources`:

| Plist key(s) | Field | Rule |
|---|---|---|
| `SerialNumber` | `serial` | trimmed; **dropped when it starts with `RAND`** (case-insensitive) |
| `FireWireGUID` / `FirewireGuid` / `FireWireGuid` | `firewire_guid` | `normalize_guid` |
| `FireWireVersion` / `scsi_revision` / `VisibleBuildID` / `BuildID` / `visibleBuildID` | `firmware` | first present |
| `BoardHwName` / `BoardHwID` | `board` | first present |
| `ModelNumStr` | `model_number` (+ `model_raw`) | `extract_model_number` |
| `FamilyID`, `UpdaterFamilyID`, `DBVersion`, `ShadowDBVersion`, `MaxTracks`, `MaxTransferSpeed` | `family_id`, `updater_family_id`, `db_version`, `shadow_db_version`, `max_tracks`, `max_transfer_speed` | numeric rule of §8.1 |
| `ProductType`, `ConnectedBus`, `VolumeFormat`, `scsi_vendor`, `scsi_product`, `scsi_revision`, `usb_serial` | `product_type`, `connected_bus`, `reported_volume_format`, same names | as text |
| `usb_pid`, `usb_vid`, `MaxFileSizeInGB` | `usb_pid`, `usb_vid`, `max_file_size_gb` | numeric rule |
| `SQLiteDB`, `SupportsSparseArtwork`, `PodcastsSupported`, `VoiceMemosSupported` | `uses_sqlite_db`, `supports_sparse_artwork`, `podcasts_supported`, `voice_memos_supported` | booleans: `1/true/yes/y/on` (case-insensitive) or a truthy number |
| `AudioCodecs`, `PowerInformation`, `AppleDRMVersion` | `audio_codecs`, `power_information`, `apple_drm_version` | only when the value is a dict |
| image keys (below) | `artwork_formats`, `photo_formats`, `chapter_image_formats` | only when non-empty |

For a `ParsedSysInfoExtended` input the result also has
`sysinfo_extended_raw_xml` (when non-empty) and
`sysinfo_extended_used_regex_fallback`.

`extract_image_formats(plist, keys)`: every list value under the given keys
contributes its dict entries. An entry's id is `FormatId`, else
`CorrelationID`, else `format_id`; width `RenderWidth`/`DisplayWidth`/`Width`/`width`;
height likewise. Entries with a missing, non-integer or non-positive id,
width or height are skipped. Result `{id: (width, height)}`, later entries
overwriting earlier ones. Key lists: `COVER_ART_KEYS` = `AlbumArt`,
`AlbumArt2`, `ArtworkFormats`, `CoverArt`, `ArtworkCoverArtFormats`;
`PHOTO_ART_KEYS` = `ImageSpecifications`, `PhotoFormats`;
`CHAPTER_ART_KEYS` = `ChapterImageSpecs`, `ChapterImageSpecifications`.

### 8.3 Evidence containers

`EvidenceValue` (frozen): `value`, `source`, `live=False`, `raw_key=""`.
`DeviceEvidence`: `fields: dict[str, EvidenceValue]`, `blobs: dict`;
`add(name, value, source, *, live=False, raw_key="", replace=False)` ignores
`None`/`""`/`b""` and keeps an existing field unless `replace`;
`as_flat_dict()` returns field values plus `_sources`.
`evidence_from_identity(identity, *, source, live=False)` builds a
`DeviceEvidence` from an identity dict, skipping keys starting with `_`,
`model_raw` and `sysinfo_extended_raw_xml`, taking each field's source from
`_sources` (default `source`), replacing existing entries.

---

## 9. `scanner` — finding and identifying mounted iPods

### 9.1 Entry points

* `scan_for_ipods() -> list[DeviceInfo]`: logs INFO `iPod scan started`;
  gets candidate volumes (`_find_ipod_volumes`); identifies each with
  `_identify_ipod_mount(mount_path, display_name)`; collapses duplicates
  (§9.6), logging INFO `Deduplicated iPod scan results: before=<n> after=<m>`
  when the count changed; always clears the macOS USB cache
  (`_clear_macos_usb_cache`) at the end, even on errors; logs INFO
  `iPod scan finished: count=<n> mounts=<display names or none>`.
* `identify_ipod_at_path(ipod_path, mount_name=None) -> DeviceInfo | None`:
  empty path → `None`. The path is user-expanded; on Windows a bare drive
  (`D:` or `D:.`) becomes `D:\`, otherwise the absolute path is used. A
  virtual iPod at that path is returned directly: its database is first
  re-created when missing (`ensure_virtual_itunes_database`, §12.4) and then
  the marker is loaded; any error there logs a WARNING
  `Virtual iPod metadata could not be loaded: <exc>` and the path is treated
  as a normal mount. A path without an
  `iPod_Control` directory → INFO `Selected path is not an iPod root: <path>`
  and `None`. Otherwise the mount is identified with the given or derived
  display name (Windows: the drive, e.g. `D:`; elsewhere the last path
  component) and the macOS USB cache is cleared afterwards.

The names `_find_ipod_volumes`, `_identify_ipod_mount`,
`_clear_macos_usb_cache`, `_probe_hardware`, `_probe_filesystem`,
`_resolve_model`, `_parse_macos_ioreg_bsd_serials`, `probe_linux_identity`
(imported into the scanner module) and the module's `sys` are looked up at
call time; tests replace them.

### 9.2 Candidate volumes (`_find_ipod_volumes() -> list[(mount_path, display_name)]`)

A volume qualifies when `<root>/iPod_Control` is a directory. Permission
errors skip the candidate.

* Windows: every drive letter the system reports; path `X:\`, display `X:`.
* macOS: every directory in `/Volumes`; display = its name.
* Linux and others: directories inside `/run/media/<user>`, `/media/<user>`,
  `/mnt`, and each directory directly under `/media`; each candidate is
  de-duplicated by real path; display = its name.

A DEBUG line lists the platform and the candidates.

### 9.3 Per-mount pipeline (`_identify_ipod_mount`)

1. A virtual iPod at the mount → returned as loaded.
2. Create `DeviceInfo(path, mount_name)`; record disk size and free space in
   GiB (`0.0` on error).
3. **Hardware evidence** — `_probe_hardware(mount_path, display_name)` (§9.4).
4. **Filesystem evidence** — `_probe_filesystem(mount_path)` (§9.5).
5. **Resolution** — `_resolve_model(hardware, filesystem, disk_size_gb)`
   (§9.7) and copy the result onto the `DeviceInfo`: model number, family
   (`"iPod"` default), generation, capacity, color, GUID, serial, firmware,
   USB PID, hashing scheme (`-1` default), identification method
   (`"filesystem"` default), `raw_identity_evidence =
   {"hardware": [hw], "filesystem": [fs]}`, `identity_conflicts`, every
   non-empty "extra" field (§9.7 list), and the resolved sources into
   `_field_sources`.
6. Not on Windows, no model number yet and a USB PID known →
   VPD identification (§14.5) with `write_sysinfo_to_device=False`: a
   returned model number sets model, family, generation, capacity, color
   and method `usb_vpd` (source `vpd`); serial, GUID and firmware fill only
   empty fields (source `vpd`); a different returned mount path replaces
   `path` (INFO log).
7. `ipod_name` = title of the master playlist (§9.8).
8. Capacity still empty and disk size known → estimated from the disk size
   (§10.3), source `disk_size`.
9. `enrich(info)` (§10).
10. INFO summary line starting `iPod identified:`.

### 9.4 Hardware evidence per OS (`_probe_hardware(mount_path, mount_name)`)

Returns a dict that may hold `serial`, `firewire_guid`, `firmware`,
`usb_pid`, `usb_vid`, `model_family`, `generation`, SCSI strings, instance
ids, and `_sources`. Afterwards sources are added where missing: for a GUID
and a USB PID the source is `device_tree` on Windows and the method name
elsewhere; for serial and firmware the method name (`ioctl`, `wmi`, `ioreg`,
`linux_identity`).

**Windows** (drive letter = first letter of `mount_name`; none → `{}`):

1. *Direct storage query* (method `ioctl`): open the volume `\\.\X:` for
   reading with shared read/write; request the standard storage-device
   property; the descriptor gives vendor, product, revision and serial
   strings at offsets stored in the descriptor. A vendor other than
   `Apple`/`Apple Inc`/`Apple Inc.` (case-insensitive) → no result. Revision
   → `firmware`. A serial that is exactly 16 hex digits after removing spaces
   → `firewire_guid` (upper case); any other non-empty serial → `serial`.
   Then walk the PnP tree (step 2) and copy its USB VID, instance ids, GUID
   (overrides), PID and PID family/generation (family/generation only when
   not set).
2. *PnP tree walk*: the volume's physical device number; the present disk
   interface with the same number; its device instance id is the USBSTOR id
   (`usbstor_instance_id`), whose third `\`-separated part contains the GUID
   as the first `&`-separated segment that is exactly 16 hex digits
   (`_extract_guid_from_instance_id`, upper case). The parent instance id
   (`usb_parent_instance_id`) holds `VID_xxxx` and `PID_xxxx` (hex) → `usb_vid`,
   `usb_pid`, and the PID's family/generation from `USB_PID_TO_MODEL`. When
   the parent is a composite interface (`MI_` in its id) and no GUID was
   found yet, the grandparent id (`usb_grandparent_instance_id`) is used for
   the GUID. 64-bit handles must be declared pointer-sized for every Win32
   call (setup once per process).
3. *Fallback* when step 1 produced nothing (method `wmi`): ask the OS
   management service for the disk behind the drive letter (logical disk →
   partition → disk drive) and read its PnP device id, serial and model. A
   16-hex serial → GUID, another serial → `serial`. From a USBSTOR PnP id:
   `REV_<x>` → firmware, the instance part → GUID. Then find the USB PID
   by searching `HKLM\SYSTEM\CurrentControlSet\Enum\USB` for keys with
   `VID_05AC` and `PID_` (skipping `MI_` interface keys) that contain an
   instance whose name includes the GUID.

**macOS** (method `ioreg`): `diskutil info -plist <mount>`; a bus protocol
other than USB → `{}`; `ParentWholeDisk` gives the BSD disk. A per-scan cache
maps BSD disks to USB serials (text `ioreg -r -c IOMedia`, parsed with
`_parse_macos_ioreg_bsd_serials`) and USB serials to Apple (`idVendor`
0x05AC) device property dicts (plist `ioreg -a -r -c IOUSBHostDevice`,
walked recursively through `IORegistryEntryChildren`; serial from
`USB Serial Number` or `kUSBSerialNumberString`, spaces removed, upper
case). The device matched through BSD disk → serial is used; if that fails
and exactly one Apple device exists, that one. From it: `idProduct` → PID
and PID family/generation; a 16-hex USB serial → GUID; `bcdDevice` →
firmware `"<major>.<minor two digits>"`. The cache is guarded by a lock and
cleared by `_clear_macos_usb_cache`.

`_parse_macos_ioreg_bsd_serials(text) -> {bsd_disk: serial}`: line by line,
a `"USB Serial Number" = "<s>"` line sets the current serial (spaces removed,
upper case); a line containing `Apple iPod Media` remembers the current serial
as pending; a following `"BSD Name" = "diskN"` line pairs `diskN` with the
pending serial and clears it. Serials of other Apple devices are never paired
because only iPod media nodes set a pending serial; each hub-attached iPod
keeps its own serial.

**Linux** (method `linux_identity`): `probe_linux_identity(mount_path)` (§15).

### 9.5 Filesystem evidence (`_probe_filesystem(ipod_path)`)

1. `detect_filesystem_type(path)` (chapter 07). Inspect the filesystem profile
   (chapter 07); failure → WARNING `Could not capture scan-time iPod volume identity: mount=<path> error=<exc>`.
   A profile with a complete volume identity yields `volume_identity_key`
   (= chapter 07's volume lock key) with source `mounted_volume_identity`
   (INFO line). When the type detection gave nothing, the profile's type is used.
2. A known type logs INFO `iPod mounted filesystem detected: mount=<path> filesystem=<type>`.
   On Linux, a type whose iTunesDB platform is Mac (chapter 07) additionally logs
   WARNING `Mac-formatted iPod filesystem detected on Linux: mount=<path> filesystem=<type>. Linux may mount journaled HFS+ read-only; verify write support before syncing.`
3. SysInfo evidence (§9.5.1) merged in.
4. Database header evidence (§9.5.2): `hashing_scheme` with source `itunes`;
   the class guess is stored as `hash_model_family` / `hash_generation`.
5. `filesystem_type` with source `mounted_filesystem`.

#### 9.5.1 SysInfo evidence

SysInfoExtended first (when the file exists): parsed with source
`sysinfo_extended`; every identity field except internal/raw ones is taken
with its source; meta keys `_sysinfo_extended_present`,
`_sysinfo_extended_keys` (plist size), `_sysinfo_extended_regex_fallback`.
Then SysInfo (when it exists): identity fields fill only fields not already
set; if a model number resolves in `IPOD_MODELS`, missing family,
generation, capacity and color are filled from the row; meta keys
`_sysinfo_present`, `_sysinfo_keys`. Parse errors are logged at INFO and
ignored. The result is `None` when nothing but `_sources` was collected.

#### 9.5.2 Database header evidence

Uses `resolve_itdb_path`. Reads the first 0x72 bytes; fewer than 0x32 bytes
or no `mhbd` magic → nothing. The u16 at 0x30 is the scheme; through
`MHBD_SCHEME_TO_CHECKSUM` it maps to a coarse class: none/unknown →
family `iPod`, generation `(pre-2007)`; HASH58 → `iPod`,
`(Classic or Nano 3G/4G)`; HASH72 → `iPod Nano`, `(5th gen)`; HASHAB →
`iPod Nano`, `(6th/7th gen)`. Any error → nothing.

### 9.6 Duplicate collapsing

Several mount paths may show the same physical iPod (Linux often mounts under
both `/media/<user>` and `/run/media/<user>`). The key of a device is the
first of: `("guid", normalize_guid(firewire_guid))`, then the same for
`usb_serial`; `("serial", serial upper-cased)`; `("path", real path)`.
Devices without any key are always kept. The first device with a key wins;
later ones are dropped with a DEBUG line `Skipping duplicate iPod mount alias: kept=… skipped=… key=…`.
GUID comparison is therefore case-insensitive.

### 9.7 Model resolution (`_resolve_model(hw, fs, disk_size_gb) -> dict`)

Pure function over the two evidence dicts; returns the resolved fields,
`_sources` and `_conflicts` (a list of
`{"field", "winner", "rejected_source", "rejected_value", "reason"}`).

1. **Extra fields** (`family_id`, `updater_family_id`, `product_type`,
   `usb_vid`, `usb_serial`, the three instance ids, `scsi_vendor`,
   `scsi_product`, `scsi_revision`, `connected_bus`,
   `reported_volume_format`, `filesystem_type`, `volume_identity_key`,
   `db_version`, `shadow_db_version`, `uses_sqlite_db`,
   `supports_sparse_artwork`, `max_tracks`, `max_file_size_gb`,
   `max_transfer_speed`, `podcasts_supported`, `voice_memos_supported`,
   `audio_codecs`, `power_information`, `apple_drm_version`,
   `artwork_formats`, `photo_formats`, `chapter_image_formats`): a
   non-empty hardware value wins (source from the hardware sources, default
   `hardware`); otherwise the filesystem value (default source
   `sysinfo_extended`).
2. **GUID**: hardware, else filesystem, else `""`.
3. **Serial** (Apple product serial): a hardware serial not starting with
   `RAND` wins; if the filesystem serial is also present, not `RAND…` and
   differs case-insensitively, a conflict is recorded (winner = hardware
   source, reason "cached product serial conflicts with live hardware
   serial"). Else a non-`RAND` filesystem serial. Else `""`.
4. **Firmware**: hardware, else filesystem, else `""`.
5. **USB PID**: hardware only (0 default). **Hashing scheme**: filesystem
   (−1 default).
6. **Model**:
   * Layer 1: SysInfo model number → `get_model_info`.
   * Layer 2: serial → `lookup_by_serial`.
   * If the hardware carries a PID family, each layer whose family/generation
     conflicts with it (`usb_pid_identity_conflicts`) is discarded with a
     conflict record (winner `usb_pid`) and a WARNING.
   * Both layers present and naming different models → the serial wins; a
     conflict records the rejected SysInfo model.
   * SysInfo layer used → model, family, generation, capacity, color from the
     row; method `sysinfo`; the model's source (default `sysinfo`) is also
     used for the four derived fields when they have none.
   * Serial layer used → the same from the serial row; method `serial`;
     the model number's source is `serial_lookup`; derived fields take the
     serial's source.
   * Otherwise: a hardware PID with a family gives family and generation
     (method `usb_pid`, source `usb_pid`; no disk-size check because modded
     iPods have other storage); if the family is still `iPod`, the header
     class guess may set family/generation (method `hashing`). Defaults:
     model number = the SysInfo model (possibly unresolved), family `iPod`,
     empty generation/capacity/color, method `filesystem`.

Examples pinned by tests: hardware `{usb_pid 0x1262, iPod Nano, 3rd Gen}`
plus SysInfo serial `Q9X772613F` → `MB453`, 8GB, Pink; a hardware serial
`ZZTOPPP2C7` (source `udev_scsi_id`) with an empty SysInfo → `MB565`
(iPod Classic 6.5th Gen 120GB Black); the same hardware serial against a
SysInfo serial `C8P04100F0GD` keeps the hardware serial and records a serial
conflict with the rejected value.

### 9.8 Master playlist title (`ipod_name`)

Uses `resolve_itdb_path`; no database → `""`. When the 32-bit value at 0x0C
is 2 (iTunesCDB) the whole file is read and decompressed with the parser's
`decompress_itunescdb` (chapter 02 §8.1); otherwise the file is read with
seeks so that only a few kilobytes are transferred. Walk the MHSD children:
the first dataset of type 2 is searched; if it exists but yields no name the
search stops; if there is no type 2, the first type 3 is searched. Inside
the playlist list, at most 16 playlists are examined; the first whose byte
at +0x14 is 1 (master) is searched for its first MHOD of type 1 among at most
64 MHODs; a string longer than 1024 bytes aborts; encoding 2 → UTF-8, else
UTF-16LE. Any read error, short read or unexpected tag → `""`.

---

## 10. `enrich(info)` — completing a `DeviceInfo` (read-only)

`enrich` fills derived fields from the sources available on the host and on
the iPod **without writing anything to the iPod**. Each step only fills
empty fields unless it states otherwise.

### 10.1 Steps

1. Load SysInfo into `info.sysinfo` when the path is known and the dict is
   empty (missing file or read error → left empty, DEBUG).
2. **Probe from strongest to weakest source**:
   1. hardware probe for the path (Windows: direct query, else management
      service; macOS: ioreg; Linux: Linux identity). Values fill empty GUID
      (not all-zero), serial, firmware, USB PID and model number; USB VID,
      USB serial, instance ids and SCSI strings go through the ranked setter
      (§10.2). Method becomes `hardware` when it was `unknown`. Any error is
      logged at DEBUG and ignored.
   2. live VPD (not on Windows): `identify_via_vpd(..., write_sysinfo_to_device=False)`
      (§14.5). Its serial, GUID, firmware and board go through the ranked
      setter with the VPD source; a model number sets model, family and
      generation, and capacity/color when present (VPD serials are
      authoritative). Its raw plist is parsed as SysInfoExtended (live) and
      applied like §10.1 step 2.3. A changed mount path replaces `path`.
      A known serial sets method `usb_vpd`. **The live payload is not cached
      on the iPod.**
   3. SysInfoExtended file: parsed and every identity field applied through
      the ranked setter with its source.
   4. SysInfo: fills empty board, serial (skipped with a WARNING when it
      equals the GUID), firmware, GUID (`0x` removed; not all-zero), model
      number (`extract_model_number`); `ModelFamily` replaces the sentinel
      family unless the family came from a USB PID and a generation is
      already known; `Generation`, `Capacity`, `Color`, `USBProductID`
      (base detection) fill empty fields; `family_id` and `updater_family_id`
      go through the ranked setter. All with source `sysinfo`.
   5. Windows only, GUID still empty: search
      `HKLM\SYSTEM\CurrentControlSet\Enum\USBSTOR` subkeys containing both
      `Apple` and `iPod`; each instance name is split at `&`; a 16-hex,
      non-zero segment is a GUID. With a known serial, only an instance whose
      name contains the serial is accepted (and returned immediately);
      without a serial the first valid GUID is used. With a serial but no
      matching instance, the first valid GUID is still used and a WARNING
      says it may be stale.
3. **Model table**: a known model number while the family is the sentinel or
   empty → family and generation from `get_model_info` (capacity and color
   only when empty), sources inherited from the model number.
4. **Serial suffix** (when a serial of at least 3 characters is known):
   `lookup_by_serial`; the serial's source (default `serial_lookup`) is
   compared by rank (§10.2) with the current source of each of model
   number, family, generation, capacity and color; a field is overwritten
   when the serial's rank is at least as good (a WARNING names a replaced
   different model number). Method becomes `serial` when it was `unknown` or
   `hardware`.
5. **USB PID**: family still the sentinel/empty and a PID in
   `USB_PID_TO_MODEL` → its family, and its generation when ours is empty
   (source `usb_pid`).
6. **Generation from capacity**: family known, generation empty → the
   capacity, or a capacity estimated from the disk size (§10.3), feeds
   `infer_generation`; a result sets the generation (source `inferred`).
7. **Canonicalise and sanitise** (§10.4).
8. Hashing scheme still −1 → read the database header (first 256 bytes of
   `resolve_itdb_path`; at least 0xA0 bytes with `mhbd`) and store the u16 at
   0x30.
9. Checksum type still 99 → §10.5.
10. HashInfo material still empty → read `iPod_Control/Device/HashInfo`
    (54+ bytes starting `HASHv0`: `iv` = bytes 38–53, `rndpart` = bytes
    26–37); failing that, when the database is at least 0xA0 bytes with
    `mhbd` and the marker `01 00` at 0x72, recover them from the existing
    HASH72 signature (chapter 04 §6.3 `extract_hash_info_to_dict`).
11. Artwork formats still empty and a family known →
    `ithmb_formats_for_device(family, generation, capacity, model_number)`;
    still empty → scan the first 64 KiB of the ArtworkDB (must start with
    `mhfd`) for format ids (chapter 05 `_extract_format_ids`) and keep the
    ids known to the registry with their dimensions.
12. Disk size unknown → total and free space in decimal GB (one decimal).
13. Capacity still empty → estimated from the disk size (source `disk_size`).
14. Sanitise again and infer a unique color (§10.4).
15. Every populated field among family, generation, capacity, color and USB
    PID without a source inherits the model number's source, else the
    identification method.

### 10.2 Ranked sources

Lower rank = more reliable. Order (rank 0 first): `scsi_vpd`,
`windows_scsi`, `linux_scsi`, `sysfs_vpd`, `udev_scsi_id`, `usb_vendor`,
`vpd`, `iokit`, `ioctl`, `device_tree`, `ioreg`, `sysfs`, `udev`, `wmi`,
`itunes`, `serial_lookup`, `usb_pid`, `disk_size`, `model_table`,
`inferred`, `sysinfo_extended`, `sysinfo`, `hashing`, `unknown`. Any other
name ranks after `unknown`. This table is `info.SOURCE_RANK` (dict name →
rank).

The ranked setter (`_set_field_from_source(info, field, value, source)`)
ignores `None`/`""`/`b""`. An empty field is set and its source recorded. An
equal value (GUIDs compared through `normalize_guid`, otherwise trimmed
text) only upgrades the recorded source when the new rank is at least as
good. A different value replaces the old one only when the new rank is at
least as good, with a WARNING naming both.

### 10.3 Capacity from disk size

Thresholds in GB of formatted size, first match wins: ≥140 → `160GB`;
≥100 → `120GB`; ≥65 → `80GB`; ≥50 → `60GB`; ≥35 → `40GB`; ≥25 → `30GB`;
≥17 → `20GB`; ≥14 → `16GB`; ≥12 → `15GB`; ≥8.5 → `10GB`; ≥6.5 → `8GB`;
≥5.2 → `6GB`; ≥4.2 → `5GB`; ≥3 → `4GB`; ≥1.5 → `2GB`; ≥0.7 → `1GB`;
≥0.3 → `512MB`; otherwise `""`.

### 10.4 Consistency repairs

* **Canonicalise**: with a model row, family, generation, capacity and color
  are set from it (source = the model number's, default `model_table`);
  without one, family, generation and color go through
  `canonicalize_model_identity` (source `model_table`). Only differing
  non-empty values are written.
* **Live PID anchor** (when a PID in `USB_PID_TO_MODEL` is known): a model
  number whose row conflicts with the PID identity is cleared (WARNING); a
  family different from the PID family is replaced by it (source
  `usb_pid`); a generation different from a non-empty PID generation is
  replaced when its source is one of `usb_pid`, `sysinfo`,
  `sysinfo_extended`, `hashing`, `unknown`.
* **Impossible variants**: when family and generation are known and a
  capacity or color is set, and the model table has rows for the pair but
  none with this capacity/color combination: a capacity no row has is
  cleared, a color no row has is cleared; when both exist separately but not
  together, the one with the worse source rank is cleared (both on a tie).
  Each clearing logs a WARNING.
* **Unique color**: color empty and every table row for family, generation
  (and capacity when known) has the same color → that color (source
  `model_table`).

### 10.5 Checksum type resolution

1. Family known and `checksum_type_for_family_gen(family, generation)` not
   `None` → that.
2. `iPod_Control/Device/HashInfo` exists → `HASH72`.
3. Header scheme 1 → `HASH58`; 2 → `HASH72`.
4. Firmware major version ≥ 2 → `UNKNOWN`.
5. A GUID is known → `UNKNOWN`.
6. Otherwise `NONE`.

---

## 11. `bootstrap.ensure_device_itunes_database(ipod_path, device_info) -> str | None`

Creates an empty database for an identified device when none exists.

1. `require_exact_model_number(device_info)`.
2. Verify the root without writing; each failure is a
   `DeviceWriteSafetyError`:
   * root not an accessible directory → "The selected iPod root is not an
     accessible directory: <root>";
   * `device_info.path` empty → "The identified iPod does not include a
     verified mount path. podsync stopped before creating device files.";
   * the identified path (resolved, OS case-normalised) differs from the
     root → "The selected iPod path does not match the device that was
     identified. Selected path: <root>; identified path: <path>.";
   * no `iPod_Control` directory → "The selected volume is not a verified
     iPod root: iPod_Control is missing. podsync stopped before creating
     device files.";
   * `iPod_Control` resolves outside the root (chapter 07 path safety) →
     "The selected iPod_Control directory resolves outside the iPod root.
     podsync stopped before creating device files."
3. An existing database (`resolve_itdb_path`) → return its path.
4. No capability row for the device → `None`.
5. Inspect write readiness (chapter 07) and enter the device write guard
   keyed by the volume lock key; re-validate readiness with a case-sensitivity
   probe. The hook `revalidate_volume` re-validates without the probe.
6. Inside the guard: an existing database (another writer may have created
   it) → return it. Missing checksum material (§11.1) → `None`.
7. Temporarily make this device the current device (restoring the previous
   one afterwards, even on errors); create `iPod_Control/{Device, iTunes,
   Music, Artwork}` (and `iTunes/iTunes Library.itlp` for SQLite devices),
   calling the hook before each directory; call `write_itunesdb` (chapter 04)
   with no tracks, `backup=False`, no artwork, the capabilities, master
   playlist name = `ipod_name`, else `mount_name`, else `"iPod"`, the guard's
   database-unchanged check as `before_database_replace`, and the hook as
   `before_device_mutation`.
8. A false result → `RuntimeError("Failed to create an empty iTunesDB for the selected iPod")`.
9. Refresh the guard's database generation, re-validate, flush the
   filesystem (chapter 07); a failed flush →
   `RuntimeError("Created the empty iTunesDB, but its durability barrier failed: <message>")`.
10. Return `resolve_itdb_path(root)`.

### 11.1 Checksum material

NONE → available. HASH58 and HASHAB → a FireWire id of 8–20 bytes from
`get_firewire_id(root, known_guid=device_info.firewire_guid)`
(`RuntimeError` → not available). HASH72 → the device carries a 16-byte IV
and 12-byte rndpart, or `iPod_Control/Device/HashInfo` is exactly 54 bytes
starting with `HASHv0`. Anything else → not available. (For HASHAB the
writer still refuses — chapter 04 §6.4 — so step 8 raises.)

---

## 12. Virtual iPods (`virtual`, `virtual_identity`)

A virtual iPod is an ordinary directory with a marker file at its **root**,
used for tests and dry runs.

### 12.1 Marker and discovery

`VIRTUAL_IPOD_INFO_FILENAME = "iPodInfo.json"`;
`virtual_ipod_info_path(path)` = `<path>/iPodInfo.json`;
`has_virtual_ipod_info(path)` = that path is a regular file (empty path →
false).

### 12.2 `available_virtual_ipod_models() -> list[dict]`

One row per `IPOD_MODELS` entry that has a serial suffix. The suffix per
model is chosen from `SERIAL_SUFFIX_TO_MODEL` preferring the **longest**
suffix, then the alphabetically first. Row keys: `model_number`,
`model_family`, `generation`, `capacity`, `color`, `serial_suffix`,
`display_name` (= non-empty parts of family, generation, capacity, color
joined by spaces, followed by ` (<model number>)`). Sorted by family,
generation, capacity, color, model number.

### 12.3 `create_virtual_ipod(ipod_path, model_number, *, ipod_name="iPod") -> DeviceInfo`

1. Root = the path expanded and resolved. Empty model →
   `ValueError("Choose an iPod model")`; unknown model →
   `ValueError("Unknown iPod model: <m>")`; no suffix →
   `ValueError("No known serial suffix for model <m>")`.
2. Capabilities from the model's family and generation; checksum = the
   row's (NONE without a row).
3. Random values: FireWire GUID = 16 upper-case hex digits, never all zero;
   serial = 8 random characters from `A–Z0–9` followed by the suffix;
   HashInfo IV 16 bytes and rndpart 12 bytes.
4. Create `iPod_Control/{Device, iTunes, Music, Artwork}` (and
   `iTunes/iTunes Library.itlp` for SQLite devices).
5. Write `iPodInfo.json` (UTF-8, indented by 2, keys sorted, trailing
   newline) with: `schema_version` 1, `created_by` `"podsync"`,
   `created_at` (UTC ISO-8601), `ipod_name` (trimmed, default `"iPod"`),
   `mount_name` (root name, default `"iPod"`), `model_number`,
   `model_family`, `generation`, `capacity`, `color`, `serial`,
   `serial_suffix`, `firewire_guid`, `firmware` (§12.5), `board` (§12.5),
   `family_id` and `updater_family_id` (§12.5), `product_type` = model
   number, `usb_vid` 0x05AC, `usb_pid` (§12.5), `usb_serial` = GUID,
   `connected_bus` `"USB"`, `reported_volume_format` `"FAT32"`,
   `filesystem_type` `"fat32"`, `scsi_vendor` `"Apple"`, `scsi_product`
   `"iPod"`, `scsi_revision` = firmware, `checksum_type` (int),
   `hashing_scheme` (wire value), `hash_info_iv` / `hash_info_rndpart`
   (upper-case hex), `db_version`, `shadow_db_version`, `uses_sqlite_db`,
   `supports_sparse_artwork`, `podcasts_supported` (row values; 0/false/true
   without a row), `voice_memos_supported` false, `artwork_formats` and
   `photo_formats` (`{id: [w, h]}` from the row), `chapter_image_formats` `{}`.
6. Write `iPod_Control/Device/SysInfo` as `Key: value` lines (skipping empty
   values) in this order: `ModelNumStr` (model number), `FirewireGuid` (the
   GUID, no prefix), `pszSerialNumber`, `BoardHwName`, `visibleBuildID`,
   `ModelFamily`, `Generation`, `Capacity`, `Color`, `USBProductID`,
   `FamilyID`, `UpdaterFamilyID` — the last three as `0x` plus 8 upper-case
   hex digits, omitted when zero.
7. Write HashInfo (chapter 04 §6.3 `write_hash_info`) with uuid = the GUID
   bytes zero-padded to 20; failures are ignored.
8. `ensure_virtual_itunes_database(root)`, then return
   `load_virtual_ipod_info(root)`.

### 12.4 `ensure_virtual_itunes_database(path)` and `load_virtual_ipod_info(path)`

`ensure_virtual_itunes_database` returns the existing database path, else
loads the virtual device and calls `ensure_device_itunes_database` (§11) for
it.

`load_virtual_ipod_info(path) -> DeviceInfo`:

* marker missing → `FileNotFoundError("Virtual iPod metadata not found at <path>")`;
  JSON that is not an object → `ValueError("Invalid virtual iPod metadata at <path>")`.
* `DeviceInfo(path=<resolved root>, mount_name=<mount_name or root name or "iPod">)`,
  `ipod_name` from the payload.
* Text fields copied when non-empty (source `iPodInfo.json`): model number,
  family, generation, capacity, color, GUID, serial, firmware, board,
  product type, connected bus, reported volume format, filesystem type,
  SCSI strings. A legacy `volume_format` key fills
  `reported_volume_format` when it is empty.
* Integer fields (base detection for text; booleans → 0): family ids, USB
  PID/VID, database versions, `checksum_type`, `hashing_scheme`.
* Volume identity: the host filesystem profile passed through §12.6; a
  complete identity sets `volume_identity_key` (chapter 07's lock key).
  `OSError` → skipped.
* Boolean fields when present: `uses_sqlite_db`, `supports_sparse_artwork`,
  `podcasts_supported`, `voice_memos_supported`.
* `usb_serial` = payload value or the GUID. HashInfo IV/rndpart from hex when
  the lengths are 16/12, else empty.
* Artwork/photo/chapter formats from `{id: [w, h]}` (bad entries skipped).
* With a capability row: `db_version` when 0, `checksum_type` when 99,
  artwork and photo formats when empty, `shadow_db_version` when 0, and the
  booleans OR-ed with the row.
* Disk size/free in decimal GB (one decimal) when available;
  `identification_method = "filesystem"`.

### 12.5 Defaults for seeded identity

* Firmware: `2.0.5` for iPod Classic; `1.0.4` for iPod Nano 5th–7th Gen;
  `1.3` for iPod 5th/5.5th Gen; `1.0` otherwise.
* Board: the letters and digits of `"<family> <generation>"` (e.g.
  `iPod5thGen`), `iPod` when empty.
* Family id (also the updater family id): shuffle 6, nano 10, classic 11,
  mini 8, otherwise 1.
* USB PID (`_usb_pid_for_identity(family, generation)`): among the
  **non-recovery** PIDs, the first whose family and generation equal the
  model's; else the first coarse PID (empty generation) of the family; else 0.
  Examples: Nano 3rd Gen → 0x1262, Nano 6th Gen → 0x1266, Nano 7th Gen →
  0x1267, Shuffle 4th Gen → 0x1303, iPod 5th Gen → 0x1202 (the first coarse
  `iPod` PID in table order).

### 12.6 `virtual_identity.virtual_ipod_profile(host_profile, ipod_path)`

Returns `host_profile` unchanged unless the root (real path) and its
`iPodInfo.json` can be `stat`ed and the marker is a regular file. Then it
returns a copy with `mount_path` = root, `filesystem_type` = the host's or
`"virtual"`, `mount_source` = the host's or the root, `inspection_path` =
root, and a volume identity with operating system `"virtual"`, device id =
the root's device number, volume id = the root's inode, and mount instance
`"<marker dev>:<marker inode>:<marker ctime ns>:<marker size>"`. Read-only and
unsafe-mount facts of the host profile are kept.

---

## 13. `usb_backend` — locating libusb for PyUSB

`get_libusb_backend()`: PyUSB not importable → `None`. The default backend
wins when it loads. Otherwise each candidate path (below) that exists is
tried by asking PyUSB's libusb1 backend to load with a library finder that
returns that path; the first success wins (DEBUG log); none → `None`.

`backend_diagnostic() -> str`, first matching case:
`"pyusb is not installed"`; `"system libusb backend available"`;
`"no libusb-1.0 library candidates found"`;
`"libusb candidates exist but failed to load: <existing paths, comma-separated>"`;
`"libusb candidates missing: <all paths, comma-separated>"`.

Candidate order: environment variables `PODSYNC_LIBUSB_DLL` and
`PYUSB_LIBUSB_DLL` (non-blank values); the `libusb_package` helper's
`find_library()` result when that package imports; the system library found
for the name `usb-1.0`; then bundled locations under the package root's
`vendor/libusb/…` and the interpreter's directory — Windows:
`vendor/libusb/windows/x64/libusb-1.0.dll` (or `x86` for 32-bit),
`<exe dir>/libusb-1.0.dll`, `<exe dir>/vendor/libusb/windows/<arch>/libusb-1.0.dll`;
macOS: `vendor/libusb/macos/libusb-1.0.dylib`, `<exe dir>/libusb-1.0.dylib`;
Linux: `vendor/libusb/linux/libusb-1.0.so`, `<exe dir>/libusb-1.0.so`.
Duplicates are removed case-insensitively keeping the first. The module's
`sys` and `ctypes.util` are looked up at call time (tests patch them).

---

## 14. Live SysInfoExtended over VPD and USB

iPods answer SCSI INQUIRY with vendor pages 0xC0–0xFF: page 0xC0 lists the
data pages; pages from 0xC2 carry consecutive fragments of the
SysInfoExtended XML (byte 3 = payload length, payload from byte 4). Page
0x80 is the standard unit-serial page, which on older iPods holds the Apple
serial. Standard INQUIRY (96 bytes) gives vendor (8–15), product (16–31) and
revision (32–35) as trimmed ASCII.

Common page-read rule for every SCSI transport: read 0xC0 (255 bytes); its
list gives the pages ≥ 0xC2 to read (all pages 0xC2–0xFF when the list is
empty or the read fails); keep payloads that contain a non-zero byte; join
them and remove trailing NULs. A failed page is skipped.

### 14.1 `vpd_windows.query_ipod_vpd_for_path(mount_path, *, usb_pid=0, serial_filter="")`

Not Windows → `None`. The drive letter comes from the path (none → `None`).
Opens `\\.\X:` with read/write sharing and sends 6-byte INQUIRY CDBs through
SCSI pass-through-direct (data-in, timeout 10 s). Result: `_source`
`windows_scsi`, `_transport` `windows_scsi_pass_through`, `usb_vid`/`usb_pid`
when a PID was given, the standard INQUIRY strings, `vpd_serial` from page
0x80 (text before the first NUL, trimmed), `vpd_raw_xml`, and all parsed
plist keys. No payload or an empty plist → `None`. When both
`normalize_guid(serial_filter)` and the normalised `FireWireGUID` (else
`usb_serial`, else `vpd_serial`) are non-empty and differ → `None`. The
handle is always closed.

### 14.2 `vpd_linux`

`_block_candidates(mount_path)`: the whole-disk device of the mount's block
device first, then the partition itself, without duplicates
(`/dev/sdf1` → `["/dev/sdf", "/dev/sdf1"]`); a failing lookup gives `[]`.
`query_ipod_vpd_for_path(mount_path, *, usb_pid=0, serial_filter="")`: not
Linux → `None`; for each candidate, open read-only non-blocking and send
INQUIRY through the SG_IO interface (timeout 10 s); build the same result
as §14.1 with `_source` `linux_scsi`, `_transport` `linux_sg_io_scsi_vpd` and
`block_device`; skip candidates without a payload, with an empty plist, or
failing the serial filter; permission and other errors are logged at INFO
and the next candidate is tried; the descriptor is always closed. Nothing
found → `None`.

### 14.3 `vpd_iokit` (macOS only)

Importing the module anywhere but macOS raises
`ImportError("podsync.device.vpd_iokit is macOS-only")`. It talks to
services matching `com_apple_driver_iPodSBCNub` through the SCSI task
interface of IOKit (CoreFoundation and IOKit frameworks via ctypes), walks up
to 10 registry parents for USB VID/PID/serial, and reads pages as above plus
page 0x80 into `vpd_serial`. `query_ipod_vpd(usb_pid=0, serial_filter="")`
returns the first matching iPod's dict (`_source` `scsi_vpd`, `_transport`
`iokit_scsi_vpd`) or `None`; `query_all_ipods()` returns all.

### 14.4 `vpd_usb_control` — Apple vendor request

`APPLE_VID = 0x05AC`. Devices: PyUSB devices with that vendor and a product
id in `IPOD_USB_PIDS`, through `get_libusb_backend()` (PyUSB missing or no
backend → none, INFO log). Reading: control transfers with request type 0xC0
(vendor, device-to-host), request 0x40, value 0x0002, index = chunk number
(0, 1, …), length 0x1000, timeout 5000 ms, until a chunk shorter than 0x1000
arrives (at most 0xFFFF chunks); trailing NULs removed.

`query_ipod_usb_sysinfo_extended(usb_pid=0, serial_filter="")`: candidates =
devices with the PID (all when 0); with a filter (spaces removed, upper
case) the device whose serial string matches, or — when none matches, a PID
was given and there is exactly one candidate — that candidate (some Windows
driver stacks refuse string descriptors); without a filter the first
candidate. Read errors (INFO log; "not supported"/"not implemented" gets a
specific message), empty payloads and empty plists → `None`. Result: parsed
plist keys plus `usb_pid`, `usb_serial`, `vpd_raw_xml`, `_source`
`usb_vendor`, `_transport` `usb_vendor_control`, `_used_usb_vendor` true.
`query_all_ipod_usb_sysinfo_extended()` queries each device by its own PID
and serial and returns the successes.

### 14.5 `vpd_libusb` — bulk-only SCSI and the identification entry point

* `query_ipod_vpd(usb_pid=0, serial_filter="")`: PyUSB required (else
  `None`); picks the first iPod device matching the PID and (case-insensitive)
  USB serial. Outside Windows the kernel driver on interface 0 is detached
  first ("Access denied"/"Operation not permitted" →
  `PermissionError("Root/sudo required to detach kernel driver for USB VPD query. Run with: sudo python -m podsync.device.vpd_libusb")`).
  It claims interface 0, finds the first bulk OUT and IN endpoints, and sends
  INQUIRY commands wrapped in USB mass-storage command blocks (signature
  `USBC`, increasing tags, data-in, LUN 0), reading the data and the 13-byte
  status block (status byte 12; non-zero skips a page). Result: `usb_vid`,
  `usb_pid`, `usb_serial`, `_source` `scsi_vpd`, `_transport`
  `usb_bulk_scsi_vpd`, standard INQUIRY strings, `vpd_raw_xml` and parsed
  plist keys (parsed from `<?xml` or `<plist`, closing tags appended when
  missing; on failure the `<string>`/`<integer>` pairs). The interface is
  released and the driver re-attached in all cases (failure to re-attach →
  WARNING asking to reconnect the iPod).
* `query_all_ipods()`: `[]` when PyUSB is not installed; queries every iPod
  device with its own PID and serial, pausing 3 s between devices;
  `PermissionError` propagates; other failures skip the device.
* `write_sysinfo(ipod_path, vpd_info, *, reported_volume_format="", expected_volume_identity_key="") -> bool`:
  builds SysInfo lines in this order, each only when present:
  `pszSerialNumber: <SerialNumber>`, `FirewireGuid: 0x<FireWireGUID or usb_serial>`,
  `visibleBuildID: <VisibleBuildID or BuildID>`, `BoardHwName`,
  `ModelNumStr`, `FamilyID`, `UpdaterFamilyID`. From `vpd_raw_xml` the part
  starting at `<?xml` (else `<plist`) is kept, with `</dict></plist>`
  appended when `</plist>` is missing. Nothing to write → `False`. Otherwise
  both files are written atomically under `iPod_Control/Device` inside
  chapter 07's guarded metadata session (with the two optional identity
  arguments) and the function returns `True`. This is the only function in
  this chapter that writes SysInfo, and only the CLI (§14.6) calls it.
* `_apple_product_serial(vpd_info)`: `SerialNumber`, else `vpd_serial`,
  trimmed; no further validation.
* `identify_via_vpd(mount_path="", usb_pid=0, firewire_guid="", *, write_sysinfo_to_device=True)`:
  returns `None` on Windows (the module's `sys.platform` is read at call
  time). Otherwise it gets a live payload (below); none, or no Apple serial
  in it → `None`. The serial is stored as `SerialNumber` when missing.
  Result: `serial`, `firewire_guid` (`FireWireGUID` or `usb_serial`,
  upper-cased, else the argument), `firmware` (`FireWireVersion`,
  `scsi_revision`, `VisibleBuildID`, `BuildID` — first present),
  `model_number`/`model_family`/`generation`/`capacity`/`color` from
  `lookup_by_serial` (empty strings when unknown), `mount_path`,
  `sysinfo_written` false, `vpd_info` (the payload), `source` (the payload's
  `_source`, default `vpd`). When the payload came from the bulk transport
  and a mount path is known, wait for the remount (12 × 1 s: look the mount
  up by USB serial, or accept the original path once it is a mount point).
  `write_sysinfo_to_device` true with an existing mount path → `write_sysinfo`
  (errors logged at DEBUG) and `sysinfo_written` = its result. podsync's own
  identification always passes `False`.
* Live payload (`_vpd_query_any_platform(usb_pid, firewire_guid, mount_path="", *, include_usb_vendor=None)`):
  the vendor request is included by default everywhere except Windows.
  The transport modules are imported inside this function (so a test can
  install stand-ins in `sys.modules`), and their query functions are called
  with keyword arguments (`usb_pid=`, `serial_filter=`). SCSI sources in
  order, the first with an Apple serial winning: macOS →
  `vpd_iokit.query_ipod_vpd` (the payload gets `SerialNumber` from
  `_apple_product_serial` when missing, `_source` `scsi_vpd`, `_transport`
  `iokit_scsi_vpd`); Windows with a mount path → `vpd_windows`; Linux with a
  mount path → `vpd_linux` (these two count only with a `SerialNumber`);
  then, not on Windows and only as root, the bulk transport (`_used_pyusb`
  true). Import errors and failures of each source are logged at DEBUG and
  skipped. Then the vendor request `vpd_usb_control.query_ipod_usb_sysinfo_extended`
  (serial filter = the given GUID, else the SCSI payload's GUID/USB serial). The two are merged: the SCSI
  payload wins conflicts, the vendor payload fills missing keys (recorded in
  `_raw_field_sources`), is kept as `_usb_vendor_info` and
  `_usb_vendor_raw_xml`, `_transport` = both transports joined by `+`,
  `_source` = the SCSI source, `_used_usb_vendor` true. Either alone is
  returned as is; neither → `None`.
* Mount lookup by USB serial (`_find_mount_point_for_usb_serial`): macOS via
  ioreg + `diskutil` (partitions of the matching disk, then `s1`–`s3`, then
  the `mount` table); Linux via `/proc/mounts` (octal escapes decoded) and a
  sysfs walk (up to 8 levels) to an Apple device whose serial matches;
  Windows via the management service (drive letter as `X:\`).

### 14.6 `python -m podsync.device.vpd_libusb`

`main() -> int` with options `--write-sysinfo`, `--pid <hex>`,
`--path <mount>`. It enables DEBUG logging on the root logger. Outside
Windows a non-root user gets "ERROR: Root privileges required. Run with:
sudo python -m podsync.device.vpd_libusb" and exit code 1. It prints
"Scanning for iPod USB devices..." and calls `query_all_ipods()` (the module
attribute, so tests can replace it); `PermissionError` → "ERROR: <exc>", 1;
no results → "No iPods found or query failed.", 1. For each result it prints
a block:

```
============================================================
iPod (USB PID 0x<pid, 4 hex digits>)
============================================================
  Apple Serial:    <SerialNumber or usb_serial or ?>
  FireWire GUID:   <FireWireGUID or usb_serial>
  FamilyID:        …
  UpdaterFamilyID: …
  BuildID:         <VisibleBuildID or BuildID or ?>
  SCSI Vendor:     …
  SCSI Product:    …
  SCSI Revision:   …
```

and, when the serial resolves through `lookup_by_serial`, the lines
`Model:`, `Capacity:`, `Color:`, `Model Number:` (same alignment). With
`--write-sysinfo`: outside Windows it waits 8 s for remounts; the mount is
`--path` or found by USB serial (3 attempts, 5 s apart); it prints
"Writing SysInfo for PID 0x<pid> to <mount>..." and calls
`write_sysinfo(mount, info)` (module attribute), printing "  Done!" or a
warning; without a mount it prints a warning suggesting `--path`. Exit code 0.

---

## 15. `linux_identity` — privilege-minimising Linux identity

The USB serial seen by the kernel is the FireWire GUID; the Apple product
serial comes from the cached SCSI page 0x80 or from podsync's udev rule
(chapter 07 §13). Module state: `_BY_ID_DIRECTORY = /dev/disk/by-id`,
`_UDEV_DATA_DIRECTORY = /run/udev/data` (both `Path`s, patched by tests),
and a set of mounts already warned about. `subprocess` and `os` are used
through the module (tests patch `linux_identity.subprocess.run` and
`linux_identity.os.path.realpath`/`exists`).

| Function | Contract |
|---|---|
| `whole_disk_device(device)` | Real path on POSIX; a basename `sd<letters><digits>` loses the digits; `mmcblk…p<n>` / `nvme…p<n>` loses `p<n>`. |
| `find_block_device(mount_path)` | `findmnt -n -o SOURCE --target <mount>` (5 s): first line, a trailing `[subpath]` removed when the source starts with `/dev/`; accepted when it starts with `/dev/`. Else `/proc/mounts` with octal escapes in the mount field decoded, first `/dev/…` source for the exact mount. Else `lsblk --json --output NAME,MOUNTPOINT`: any (nested) entry with that mount point → `/dev/<name>`. Else `None`. |
| `parse_vpd_page_80(data)` | Needs ≥ 4 bytes, byte 1 = 0x80, big-endian length at 2–3 > 0 and fully present; payload up to the first NUL decoded as ASCII and trimmed; empty, non-ASCII or containing control characters → `""`. |
| `_clean_product_serial(value)` | NULs removed, trimmed; empty, non-ASCII or with control characters → `""`. |
| `_parse_hex_guid(value)` | Spaces removed; exactly 16 hex digits → upper case; else `""`. |
| `_is_ipod_usb_identity(usb_pid, product_name)` | True when the PID is in `IPOD_USB_PIDS` or the product name (underscores → spaces, trimmed, case-folded) is exactly `ipod`. `(0x1261, "")` → true, `(None, "iPod")` → true, `(0x12A8, "iPhone")` → false. |
| `_is_ipod_scsi_device(base_disk)` | sysfs `vendor` starts with `apple` and `model` starts with `ipod` (case-insensitive); read errors → false. |
| `_cached_product_serial(base_disk)` | sysfs `device/serial` cleaned; else `device/vpd_pg80` parsed as page 0x80; else `""`. |
| `_udev_database_key(device)` | `b<major>:<minor>` for a block device node; `""` otherwise or on any error. |
| `_identity_from_udev(device)` | Properties from `udevadm info --query=property --name <device>` (5 s; errors ignored), completed (without overriding) by `E:` lines of `<_UDEV_DATA_DIRECTORY>/<key>`. `ID_PODSYNC_PRODUCT_SERIAL` cleaned → `serial` (source `udev_scsi_id`); when absent but `ID_PODSYNC_RULE_VERSION` is set, an INFO line notes the rule ran without publishing a serial. Transport fields only when `ID_VENDOR_ID` is `05ac` and `_is_ipod_usb_identity(int(ID_MODEL_ID, 16), ID_MODEL)`: `ID_SERIAL_SHORT` as a 16-hex GUID → `firewire_guid` (source `udev`); the PID → `usb_pid` (source `udev`) plus its family/generation from `USB_PID_TO_MODEL`. `_sources` present only when something was found. |
| `_identity_from_sysfs(base_disk)` | When `/sys/block/<disk>/device` does not exist: only the cached serial, and only for an iPod SCSI device (source `sysfs_vpd`). Otherwise walk up to 12 ancestors to the first with `idVendor` `05ac`; if that device is not an iPod identity (PID/product name) stop; else record the PID (source `sysfs`) with its family/generation and a 16-hex `serial` attribute as GUID (source `sysfs`). The cached serial is added (source `sysfs_vpd`) only when the device is an iPod by either test — so non-iPods never trigger a cached-serial read. |
| `_identity_from_usb_bus(base_disk)` | Fallback: the Apple device under `/sys/bus/usb/devices` whose real path is an ancestor of the disk's real path and which is an iPod identity; PID and GUID as above (source `sysfs`). |
| `_identity_from_by_id(whole_disk)` | Among `<_BY_ID_DIRECTORY>/ipod-*` links, the one whose real path equals the disk's; the name after `ipod-`, cleaned, must be 8–16 letters/digits → `serial` (source `udev_scsi_id`). |
| `probe_linux_identity(mount_path)` | No block device → `{}`. Merge, first value per field winning and keeping its source: sysfs(disk), udev(partition), udev(whole disk, when different), by-id(whole disk), USB bus(disk). With a serial the mount leaves the warned set; without one but with a PID or GUID, a single WARNING per mount says the Apple product serial is unavailable and points to the Linux identity setup (chapter 07 §13). |

---

## 16. `diagnostic_log` — compact log formatting

Field lists (`(field, label)` pairs, in order):

* `IDENTITY_FIELDS`: model_number→model, model_family→family,
  generation→gen, capacity, color, serial, firewire_guid→fwguid,
  firmware→fw, usb_vid→vid, usb_pid→pid, usb_serial, scsi_vendor,
  scsi_product, scsi_revision→scsi_rev.
* `CAPABILITY_FIELDS`: family_id, updater_family_id→updater_id,
  product_type→product, db_version, shadow_db_version→shadow_db,
  uses_sqlite_db→sqlite, supports_sparse_artwork→sparse_art, max_tracks,
  max_file_size_gb→max_file_gb, max_transfer_speed→max_transfer,
  podcasts_supported→podcasts, voice_memos_supported→voice_memos,
  artwork_formats→art_ids, photo_formats→photo_ids,
  chapter_image_formats→chapter_ids.
* `SOURCE_FIELDS`: serial, firewire_guid→fwguid, model_number→model,
  model_family→family, generation→gen, capacity, color, usb_pid→pid,
  firmware→fw, filesystem_type→filesystem.

| Function | Contract |
|---|---|
| `is_missing(v)` | `None`, `""`, `b""`, `{}` or `[]`. |
| `compact(v, *, max_chars=96)` | Text unchanged when short enough; else head of `max_chars // 2 − 2` characters, `...`, and a tail filling the rest. |
| `format_value(field, v)` | bytes → `<n bytes>`; bool → `yes`/`no`; `usb_vid`/`usb_pid` → `0x` + 4 hex digits (0 → `0`); `db_version`/`shadow_db_version` → `0x` + hex without padding (0 → `0`); non-numeric in those → `compact`; format dicts → `<count>[<first 12 sorted ids>...]`; other dicts → `<count> keys[<first 8 keys>...]`; other iterables → `<count>[<first 12>...]`; else `compact`. |
| `format_fields(data, fields=IDENTITY_FIELDS, *, include_false=False)` | `label=value` for fields present and not missing, skipping numeric zeros and — unless `include_false` — `False`; joined by `", "`; `none` when empty. |
| `format_sources(sources, fields=SOURCE_FIELDS)` | `label:source` for fields with a source, joined by `", "`; `none` when empty. |
| `format_conflicts(conflicts)` | `none` when empty; non-list → `compact`. At most 6 entries, each `field` plus ` winner=…`, ` rejected=<source or ?>:<value compacted to 36>`, ` reason=<compacted to 64>` when present; non-dict entries compacted to 96; more than 6 → `+<n> more`; joined by `"; "`. |

---

## 17. Errors and side effects

| Exception | Raised by | When |
|---|---|---|
| `UnidentifiedDeviceError` (`ValueError`) | `set_current_device`, `require_exact_model_number`, `ensure_device_itunes_database` | no exact model number |
| `DeviceWriteSafetyError` (chapter 07) | `resolve_itdb_path`, `detect_checksum_type`, `ensure_device_itunes_database` | unreadable database names / HashInfo; unverified root |
| `RuntimeError` | `get_firewire_id`, `ensure_device_itunes_database` | no GUID; empty database not created or not flushed |
| `FileNotFoundError` | `read_sysinfo`, `load_virtual_ipod_info` | file missing |
| `ValueError` | `create_virtual_ipod`, `load_virtual_ipod_info` | bad model / bad marker JSON |
| `PermissionError` | `vpd_libusb.query_ipod_vpd`, `query_all_ipods` | kernel driver detach needs root |
| `ImportError` | importing `vpd_iokit` outside macOS | always |

Writes to the iPod made by this chapter: `create_virtual_ipod` (folders,
`iPodInfo.json`, SysInfo, HashInfo, empty database);
`ensure_device_itunes_database` (folders, empty database, under the write
guard); `vpd_libusb.write_sysinfo` (SysInfo, SysInfoExtended, under the
guarded metadata session) — only from the CLI. Scanning, identification and
`enrich` only read — with one exception: identifying a **virtual** iPod
re-creates its database when it is missing (§9.1). Host side effects: subprocesses (`diskutil`, `ioreg`,
`findmnt`, `lsblk`, `udevadm`, `powershell`), registry reads, USB access when
PyUSB is installed, and the process-wide current-device store.

---

## 18. Not in podsync

* **No SysInfo authority file** and no `update_sysinfo`,
  `check_authority_coverage`, `read_authority`, `cache_sysinfo_extended`,
  `AUTHORITY_FILENAME` or `SYSINFO_FIELDS`: identification does not record
  provenance on the iPod.
* **No background live re-validation thread** and no caching of live
  SysInfoExtended payloads on the device.
* **No device images/colors** (`images` module, `MODEL_IMAGE`, `COLOR_MAP`,
  …): `DeviceInfo.icon` is the emoji of §7.1.
* No GUI, application, podcast, SQLite writer or transcoder imports.
