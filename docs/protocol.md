# PeriPage A6 304dpi wire protocol

Independent documentation of the protocol used by the PeriPage A6 304dpi
(often sold as A6+ / `PeriPage+XXXX_BLE`). The printer does **not** speak a
full ESC/POS dialect. Raster output happens to use `GS v 0`; almost
everything else is a vendor opcode starting with `10 FF`.

Transport on current 304dpi units is **Bluetooth LE GATT**, not classic
RFCOMM. `bluetoothctl pair` fails with `AuthenticationFailed`. BlueZ
`Connect()` then tries the advertised Serial Port profile (BR/EDR) and
returns `br-connection-profile-unavailable`. This driver uses `gatttool`
for a real LE ATT link.

Write characteristic `0000ff02` value handle `0x0012`; notify `0000ff01`
value handle `0x000f`. The same command bytes as the old SPP firmware work
if each protocol packet is one ATT write.

## Geometry

| | |
|---|---|
| Native width | 576 pixels |
| Bytes per row | 72 (1 bit/pixel, MSB = leftmost) |
| Resolution | 304 dpi |
| Built-in ASCII columns | 48 |
| Printable width | ~48.5 mm on a 58 mm roll |
| Firmware string | `V2.11_304dpi` |
| SoC | BR2141e-s |

A 1-bit means "burn this pixel" (black). Pillow mode `1` stores white as 1,
so images must be inverted while still grayscale.

The 203dpi A6 is a different machine (384 px / 48 bytes/row). This driver
does not target it.

## Session

After `connect()`, send reset or the printer stays mute:

```
10 FF FE 01  00 00 00 00 00 00 00 00 00 00 00 00
```

After a job, the original Android/PC clients send `ESC J n` then:

```
10 FF FE 45
```

## Vendor queries (`10 FF …`)

| Command | Meaning | Notes |
|---|---|---|
| `10 FF 20 F0` | model id | e.g. `IP-300` |
| `10 FF 20 F1` | firmware | e.g. `V2.11_304dpi` |
| `10 FF 20 F2` | serial | |
| `10 FF 30 10` | hardware | |
| `10 FF 30 11` | Bluetooth name | `PeriPage+XXXX` |
| `10 FF 30 12` | MAC | first 6 bytes |
| `10 FF 50 F1` | battery | `{0, percent}` |

Do **not** send `10 FF 70 F1 00`. It returns a combined status string but
shifts the next raster job and injects a `█` into the ASCII buffer.

## Configuration

```
10 FF 10 00  ll     concentration: 00 light, 01 medium, 02 dark
10 FF 12     mm mm  auto-off timeout, minutes, big-endian
```

## Paper feed (ESC/POS `ESC J`)

```
1B 4A  nn     nn = 1..255 dots
```

255 dots ≈ 21 mm at 304 dpi.

## Raster (ESC/POS `GS v 0`)

```
1D 76 30  m  xL xH  yL yH  data…

m  = 0 (normal size)
x  = xL + 256*xH = 72
y  = yL + 256*yH = row count
data length = x * y
```

One row of 576 black pixels is 72 `FF` bytes. Bit 7 of each byte is the
leftmost pixel of that octet.

The printer's internal buffer is only a few hundred rows. This driver
sends at most 255 rows per `GS v 0`, resets, then continues. Long solid
black jobs overheat the head; split them and pause.

## Built-in ASCII

Raw 7-bit bytes, 48 characters per row. A lone `\n` flushes the line.
Two consecutive `\n` bytes freeze the firmware — send a small `ESC J`
instead of a blank line.

## BLE GATT

Observed on `PeriPage+B250_BLE`:

| Role | UUID |
|---|---|
| Service | `0000ff00-0000-1000-8000-00805f9b34fb` |
| Write | `0000ff02-0000-1000-8000-00805f9b34fb` |
| Notify | `0000ff01-0000-1000-8000-00805f9b34fb` |

An ISSC transparent-UART service (`49535343-fe7d-…`) is also advertised.
Do not concatenate a whole job and slice it by MTU — the printer accepts
the writes and prints nothing. One BLE write per protocol packet:

1. reset
2. concentration
3. `GS v 0` header
4. each 72-byte row

## Write pacing

Pace roughly one row every 15 ms (~11 mm/s), which matches the mechanical
speed of the A6.

## Sources

Reconstructed from public reverse-engineering of Bluetooth captures
(Elias Weingärtner, bitrate16) and from the `GS v 0` / `ESC J` layout in
the ESC/POS command set. This tree does not copy those implementations.
