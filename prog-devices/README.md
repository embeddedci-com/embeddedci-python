# prog-devices: the pin programmers' chips as data

The device table of `BenchPod.flash(target="avr-updi" | "pic" | "efm8" | "attiny-tpi" | "xmega" | "msp430")` (the pod's
`prog_flash` command): one YAML file per chip. This directory is the only place the data is edited. The
generator `scripts/gen_prog_devices.py` turns it into

| Generated file | Used by |
|---|---|
| `packages/embeddedci/src/embeddedci/benchpod/prog_devices.json` | the SDK (`embeddedci.benchpod.prog`) |
| `benchpod-cli/internal/progdev/prog_devices.json` and `prog_devices.README.md` | the CLI (`--cli`) |
| `embeddedci-server/webapp/src/progIds.json` | the web app's detect (`--web`) |

`python scripts/gen_prog_devices.py --check` fails when any of them differs from the YAML. CI runs it
here, and the CLI and server repositories check their copy against the one this repository publishes
(see "Checks"). Never edit a generated file; change the YAML and run the generator.

## Layout

```
prog-devices/
  <proto>/                  updi, pic, c2, tpi, pdi, sbw
    _protocol.yaml          flash(target=) name, title, image regions to ignore
    <family>/<chip>.yaml    one chip
  pack-differences.yaml     known differences from Microchip's packs (open questions)
```

Chips are written in natural order of family and file name; the table is not order-sensitive.

## A chip file

```yaml
name: PIC16F18446
aliases: [..]               # optional: other names for the same ID
id: "d430"                  # hex bytes in the order of the pod's status.id, quoted; ?? = varying byte
family: PIC16F184xx
voltage: {min_mv: 2300, max_mv: 5500, erase_min_mv: 2700}
voltage_note: "..."         # optional
chip_erase_only: true       # optional
notes: "..."                # optional
source: "DS40001970"        # optional, provenance only, not shipped
prog:                       # the typed protocol fields, below
  cmd_set: 8bit
  cp: {addr: 0x10016, bit: 0, protected_when: 0}
mem:
  flash:  {size: 32768, page: 64, addr: 0,       hex: 0,       align: 64}
  config: {size: 10,    page: 2,  addr: 0x1000E, hex: 0x1000E, align: 2}
  user:   {size: 8,     page: 2,  addr: 0x10000, hex: 0x10000, align: 2}
```

`mem` keys are `flash`, `eeprom`, `config`, `user`; each takes `size`, `page`, `addr`, `hex` and optional
`align` and `stride` (meaning in `prog_devices.README.md`, next to the generated JSON). The generator refuses
unknown keys, wrong types, out of range values, memories that overlap in the image, and two chips of
one protocol with the same name.

### `prog` fields (typed; each one replaces a bit or a reused number of the old `flags` integer)

| Protocol | Field | Type | Meaning | Old `flags` |
|---|---|---|---|---|
| `updi` | `flash_base` | integer | the chip's flash in the UPDI data space: 0x8000 tinyAVR 0/1/2, 0x4000 megaAVR 0, 0x800000 AVR DA/DB/DD | the whole integer |
| `pic` | `cmd_set` | `6bit` (default), `8bit` | the ICSP command set | bit 0 |
| `pic` | `pc` | `word` (default), `byte` | PIC18 Q: the program counter counts bytes; needs `8bit` | bit 1 |
| `pic` | `ee_erase` | boolean | 6-bit set: chip erase also clears the data EEPROM | bit 2 |
| `pic` | `q10` | boolean | PIC18 Q10 erase and config words; needs `pc: byte` | bit 4 |
| `pic` | `id_split` | boolean | revision (0x8005) and device ID (0x8006) are separate words | bit 5 |
| `pic` | `key_flip` | boolean | entry key in the other command set's bit order | bit 6 |
| `pic` | `cp` | `{addr, bit, protected_when}` | the configuration word with the code-protect bit: byte `addr`, its `bit` (0..15) and the value (0 or 1) that means protected | `cp_addr`, bits 8..11, bit 3 |
| `tpi` | `write_words` | 1, 2, 4 | words per flash write | bits 0..2 |
| `tpi` | `page_erase` | boolean | the chip has a page erase | bit 3 |
| `c2` | `fpdat` | 1..255 | the FPDAT register address (default: by device ID) | bits 0..7 |
| `c2` | `no_setup` | boolean | skip the per-device setup writes | bit 8 |
| `pdi`, `sbw` | (none) | | | |

The generator turns them into the request fields the pod's descriptor takes (`cp` becomes `cp_addr`,
`cp_bit` and `cp_high`) and, next to them, the packed `legacy` form that pods before firmware 4.0.6
read. That mapping is in `gen_prog_devices.py` only. The SDK, the CLI and the web app send `prog` to a pod
with the `prog_named` feature and `legacy` to an older one; `legacy` goes away one release after.

Not in the YAML yet: the PIC and TPI/PDI timings (`T_PROG_US`, `T_ERASE_US`, ...) and the C2 per-device
setup sequences (`k_seq_*`) are still constants in the firmware protocol code, keyed by device ID.
They are the next fields to move if a chip needs different values (design study
`private-docs/programmer-dsl/README.md`, option (a), item 3).

## Workflow

Add or change a chip:

```bash
$EDITOR prog-devices/updi/tinyavr-0-1-2/attiny3226.yaml
python scripts/gen_prog_devices.py            # SDK table
python scripts/gen_prog_devices.py --cli ../benchpod-cli --web ../embeddedci-server/webapp/src/progIds.json
python -m pytest scripts packages/embeddedci/tests/test_prog_flash.py packages/embeddedci/tests/test_prog_named.py
```

Then commit the YAML and the generated files in this repository, and the copies in the CLI and the server.

## Where the numbers come from

| Fields | Source |
|---|---|
| `id`, `mem.*.size`, `mem.*.page` of AVR chips (tinyAVR, megaAVR 0, AVR Dx, ATtiny TPI parts, XMEGA) | Microchip's ATDF files in the device packs (`Microchip.ATtiny_DFP`, `ATmega_DFP`, `AVR-Dx_DFP`, `XMEGAA..E_DFP`), after avrdude's `avrdude.conf` (itself generated from the same files) and the datasheets; cross-checked by `scripts/prog_devices_packs.py check` |
| `id`, `mem.flash.size`, `mem.eeprom.size` of PIC chips | the `.PIC` files of `Microchip.PIC16F1xxxx_DFP` and `PIC18F-Q_DFP` (DEVID, `CodeSector`, `EEDataSector`); the same check. The older PIC16F1xxx parts' DFP was not part of the check |
| `mem.*.addr`, `mem.*.hex`, `align`, `stride` | the pod's address space and the toolchains' hex layouts (avr-gcc, XC8): programming specifications and the bench, not the packs |
| `prog.*` (entry style, flash base, code-protect word, TPI write width, C2 FPDAT) | the programming specifications (PIC: DS numbers in the families' `notes` and in `prog_devices.README.md`), the datasheets' NVM chapters and the bench; not in the packs |
| `voltage.*`, `voltage_note`, `chip_erase_only`, `notes` | datasheets and programming specifications; the erase and brown-out limits were set from the bench findings |
| C2 (EFM8) rows | Silicon Labs reference manuals (`prog_devices.README.md`); not Microchip |

`scripts/prog_devices_packs.py` has two modes (download packs from <https://packs.download.microchip.com/>):

```bash
python scripts/prog_devices_packs.py check Microchip.ATtiny_DFP.*.atpack Microchip.AVR-Dx_DFP.*.atpack ...
python scripts/prog_devices_packs.py new Microchip.ATtiny_DFP.*.atpack --like ATtiny1624 --name ATtiny424
```

`check` compares ID, flash/EEPROM/user row sizes and pages of every chip a pack names and exits 1 on a
difference; differences already known are listed with their reason in `pack-differences.yaml`
(open questions, not approvals). `new` starts a chip file from a template chip (`--like`: its `prog`,
voltage and notes) with the ID and the sizes and pages from the pack; check the addresses and voltage by
hand before you commit it.

Result of the first check (packs of 2026-11-25: ATtiny 3.4.278, ATmega 3.6.299, AVR-Dx 2.8.343, XMEGA A to E,
PIC16F1xxxx 1.32.471, PIC18F-Q 1.31.492): 201 chips compared, 54 known differences, 87 chips not in
those packs (older PIC16F1xxx, EFM8, some PIC18). The differences worth a look are the PIC18 Q83/Q84
device IDs (table 0x99.. second byte, DFP 0xA3..), the PIC18F25/45Q10 EEPROM size, and the AVR Dx
EEPROM page; they are unchanged here (the wire traffic must not change) and listed as open questions.

## Checks

* `python scripts/gen_prog_devices.py --check`: the SDK table is the generator's output (CI, `test` job).
* `python -m pytest scripts`: generator and pack tool tests (schema errors, determinism, outputs).
* `packages/embeddedci/tests/test_prog_named.py`: for every chip and memory the legacy `start` and `info`
  requests are byte identical to what SDK 4.0.5 sent (`tests/data/prog_wire_golden.json`, captured from
  the schema 1 table), and the named fields mean the same flags to the firmware (`fw_flags` mirrors
  `prog_desc.c`).
* benchpod-cli and embeddedci-server: a script compares their copy with this repository's `main`.
