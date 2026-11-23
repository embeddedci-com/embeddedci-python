# prog_devices.json

The device table of the pin programmers (`BenchPod.flash(target="avr-updi" | "pic" | "efm8" |
"attiny-tpi" | "xmega")`, the pod's `prog_flash` command). The pod holds no device tables: it reads
the chip's ID bytes and the host passes the geometry. This file is that table, plain JSON, one chip
per line, so other clients (the CLI) can copy it unchanged. The SDK reads it in
`embeddedci/benchpod/prog.py`.

## Schema (`"schema": 1`)

```
{ "schema": 1, "about": "...",
  "families": { "<proto>": { "target": "...", "title": "...", "ignore": [...], "devices": [...] } } }
```

`<proto>` is the pod's `proto`: `updi`, `pic`, `c2`, `tpi`, `pdi`. `target` is the
`flash(target=...)` name of the family (`avr-updi`, `pic`, `efm8`, `attiny-tpi`, `xmega`); the
protocol names are accepted as aliases too.

`ignore` lists image regions to leave out without an error: `{"hex": address, "size": bytes,
"name": text}` (the AVR lock bits and signature row that an avr-gcc `.elf` carries).

A device:

| Key | Meaning |
|---|---|
| `name` | The chip, or the family of chips that share the ID and memories. Matched ignoring case, spaces and dashes. |
| `aliases` | (optional) other names for the same ID, for example `ATxmega16A4` next to `ATxmega16A4U`. |
| `id` | Hex bytes, in the order of the pod's `status.id`. `??` is a byte that varies (a revision). The ID is matched as a prefix of what the pod reports: the pod's `expect_id` is `id` up to its first `??`. |
| `family` | Free text, for messages. |
| `mem` | The chip's memories, keys `flash`, `eeprom`, `config` (AVR fuses, PIC configuration words, C2 lock byte) and `user` (AVR user row, PIC user ID); each omitted when the chip has none or the pod cannot write it. |
| `opts` | Extra fields of every `start` command: `flags` (protocol options) and `baud` (PIC: byte address of the configuration word that holds the code-protect bit). |
| `vmin_mv`, `vmax_mv` | The supply range the chip can be programmed in, in millivolts, at the pod's I/O voltage (1800 or 3300). A chip whose `vmin_mv` is above the pod's voltage is refused with `vnote` as the reason. |
| `erase_vmin_mv` | (optional) the supply the chip erase needs: below it the erase can be ignored, so a `chip` erase is refused. |
| `vnote` | (optional) the voltage remark of the datasheet. |
| `notes` | (optional) other remarks. |

A memory (`mem.<name>`):

| Key | Meaning |
|---|---|
| `size` | Bytes. |
| `page` | The write page or row in bytes: the `page` of `start`. |
| `addr` | The `addr` of `start` for the memory's first byte (the pod's address space: AVR memories start at 0; PIC addresses are the spec's byte address, 2 per word). |
| `hex` | The address of the memory's first byte in a `.hex` / `.elf` image (avr-gcc: flash 0, EEPROM 0x810000, fuses 0x820000, user row 0x850000; PIC: see below). |
| `align` | (optional) region start and end unit in bytes; default: `page` for flash, 1 otherwise. A partial unit is padded with 0xFF. |
| `stride` | (optional) hex bytes one byte of this memory takes; default 1. |

To write an image: read the chip's ID with `info`, find the device whose `id` matches, and for every
image run of bytes find the memory whose `hex` range holds it. The pod address of image byte `a`
is `mem.addr + (a - mem.hex) / stride`. Start the regions flash first, `config` last, each as one
`start` with `mem`, `addr`, `len`, `page`, `expect_id`, the `opts`, and `erase: "chip"` on the
first one only (later ones `none`).

## Sources

Every family's numbers come from the vendor documents below; where a vendor's tools were used to
cross-check, that is said. Nothing here was run on hardware yet.

### AVR UPDI (`updi`): tinyAVR 0/1/2, megaAVR 0, AVR DA/DB/DD

* Reference datasheets: ATtiny1614/1616/1617 DS40002204 (chapters NVMCTRL and UPDI) and AVR128DA
  DS40002183 (the same chapters for NVM version 2); the other chips' datasheets of each family give
  the signature (SIGROW), flash, EEPROM and user row sizes and pages.
* The IDs, sizes and pages were read from avrdude's `avrdude.conf` (`src/avrdude.conf.in`,
  github.com/avrdudes/avrdude), which carries them from the Atmel/Microchip device packs, and
  checked against the datasheets above.
* The flash base in the AVR data space (`opts.flags`) follows the firmware: 0x8000 for tinyAVR
  0/1/2, 0x4000 for megaAVR 0, 0x800000 for the NVM version 2 parts (DA, DB, DD).
* Left out: AVR DU, EA, EB, LA, SD and the "S" variants (other NVM versions, not handled by the pod
  firmware yet).
* Voltage: 1.8-5.5 V for the whole range (the datasheets' operating range; NVM writes are specified
  over it).

### AVR TPI (`tpi`): ATtiny4/5/9/10/20/40/102/104

* ATtiny4/5/9/10 DS40002060, ATtiny20 8235, ATtiny40 8263, ATtiny102/104 42505: signatures, flash
  size and page, the TPI programming chapters (memory map: lock bits 0x3F00, configuration 0x3F40,
  signature 0x3FC0, flash 0x4000), the number of words per flash write (ATtiny20: 2, ATtiny40: 4)
  and the page erase of the ATtiny102/104.
* IDs and sizes cross-checked with avrdude's `avrdude.conf`.
* **Voltage:** the ATtiny4/5/9/10, 20 and 40 can be programmed at 5 V only (datasheets: "Apply 5V",
  4.75-5.5 V in the programming tables). The pod's I/O is 1.8 or 3.3 V, so these are marked
  `vmin_mv: 4750` and refused with a message. The ATtiny102/104 datasheet gives 1.8-5.5 V in one
  section and 5 V in another: they are allowed and the note says to try it.

### XMEGA PDI (`pdi`)

* XMEGA AU manual 8331 and the ATxmega datasheets (A4U 8387 and the other chips'):
  signatures, flash (application + boot as one space from 0), EEPROM, fuse bytes, user signature row,
  the NVM controller. IDs, sizes and pages cross-checked with avrdude's `avrdude.conf`; parts that
  share an ID are one entry with `aliases`.
* Voltage: 1.8 to 3.6 V as the table says; the pod's 3.3 V is fine, 1.8 V works when BODLEVEL is
  below VCC (datasheet), which the chip's fuses decide.

### PIC (`pic`) and Silicon Labs C2 (`c2`)

See the sections below.
