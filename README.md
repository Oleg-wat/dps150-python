# dps150.py — Python CLI for the FNIRSI DPS-150 power supply

Control and monitor a FNIRSI DPS-150 from the command line over its
Micro-USB data port (115200 baud serial). Single-file Python script,
works on Linux, macOS, and Windows.

## Requirements

- Python 3.7+
- pyserial:  `pip install pyserial`

## Device auto-detection (no port needed)

The DPS-150 enumerates on USB as VID:PID `2E3C:5740`, and each unit carries a
unique serial number in its USB descriptor (e.g. `13BD898A2565`). The script
uses this to find the device automatically — no `-p COM5` / `/dev/ttyACM0`
needed, and it keeps working when the OS reassigns port numbers.

    python3 dps150.py list          # show detected units: port, serial, vid:pid
    python3 dps150.py status        # auto-detects a single connected unit

Selection priority:
1. `-p <port>` (or `$DPS150_PORT`) — explicit port always wins
2. `-s <serial>` (or `$DPS150_SERIAL`) — exact unit by USB serial number;
   errors out if that unit isn't connected (and lists what is)
3. If exactly one DPS-150 is connected, it is used automatically
4. If several are connected, you get a list and must select one with `-s` or `-p`

`status` and `-j monitor` include the USB serial in their output, so logs are
traceable to the physical unit that produced them — useful when you have more
than one supply on the bench.

## Setup (Linux)

1. Connect the DPS-150's **Micro-USB data port** (top right, next to the
   screen). Do NOT use the USB-C port — that one is power input only.
2. Use a real data cable (charge-only cables are the #1 cause of "no response").
3. The device shows up as `/dev/ttyUSB0` (CH340) or `/dev/ttyACM0`.
   Check with:  `dmesg | tail`  or  `ls /dev/ttyUSB* /dev/ttyACM*`
4. Serial permissions, once:  `sudo usermod -aG dialout $USER`  then re-login.
5. Optional:  `export DPS150_PORT=/dev/ttyUSB0`  to omit `-p`.

On Windows use `-p COM3` (check Device Manager). On macOS use
`-p /dev/cu.usbserial-XXXX`.

## Usage

    python3 dps150.py [-p port] [-s serial] [-j] [-v] <command> [args]

    dps150.py list                  # detected units: port, USB serial, vid:pid
    dps150.py status                # full status dump
    dps150.py set 12.0 1.5          # 12 V, 1.5 A limit
    dps150.py on                    # enable output (verifies, reports V/A)
    dps150.py off                   # disable output
    dps150.py voltage 5.0           # change only voltage
    dps150.py current 0.5           # change only current limit
    dps150.py monitor 0.5           # live table every 0.5 s (Ctrl-C stops)
    dps150.py -j monitor 1 >> log.jsonl   # JSON-lines logging
    dps150.py info                  # model / hardware / firmware
    dps150.py group 3 3.3 2.0       # program preset group M3
    dps150.py ovp 15                # thresholds: ovp ocp opp otp lvp
    dps150.py metering on           # Ah/Wh energy metering
    dps150.py raw 10                # dump decoded frames (debugging)
    dps150.py -v status             # raw TX/RX hex on stderr

`-j` prints machine-readable JSON (`status` and `monitor`) for scripting:

    python3 dps150.py -j status | jq .output_voltage

## Command-and-verify

`set`, `voltage`, and `current` don't just transmit the command — they read
the device state back and compare it against the request. The CLI stays a
transport layer (the command is always sent; the device remains the source
of truth), but you get a diagnostic when the hardware can't actually deliver:

    $ dps150.py voltage 12          # powered from a laptop USB port only
    voltage command sent: 12.00 V

    warning: requested voltage exceeds the current device limit
      requested     : 12.00 V
      device limit  : 3.53 V
      input voltage : 3.73 V
      protection    : LVP
      cause         : external input power is missing or too low
      action        : connect sufficient input power (input must exceed the desired output)

On success you get `voltage set and verified: 12.00 V` and exit code 0;
any mismatch (device clamped/rejected the value, protection active, limit
exceeded, no response) prints a warning to stderr and exits 1 — so shell
scripts and EOL automation can gate on it:

    dps150.py set 12 1.5 && dps150.py on || echo "supply not ready"

The same check catches OVP/OCP/OPP states, USB-PD wattage limits, values the
firmware silently rejects, and communication that succeeds without the
setting being applied.

For a fixed EOL station, set the expected instrument identity in the service environment:

    export DPS150_SERIAL=13BD898A2565

Tip: make it a command:  `chmod +x dps150.py && sudo cp dps150.py /usr/local/bin/dps150`

## Using it as a library

```python
from dps150 import DPS150

dev = DPS150("/dev/ttyUSB0")
dev.session_start()
dev.set_voltage(12.0)
dev.set_current(1.5)
dev.set_output(True)
dev.fetch_all()
print(dev.state["output_voltage"], dev.state["output_current"])
dev.close()   # also unlocks the front panel
```

## Troubleshooting

- **"no response from device"** — wrong port, charge-only cable, or device off.
  `-v` shows whether any bytes come back; `raw` shows decoded frames.
- **Nothing at /dev/ttyUSB0** — check `dmesg` after plugging in; on some
  distros the CH340 module (`ch341`) is blacklisted due to braille-display
  conflicts — remove the blacklist entry.
- **Front panel locked** — normal while a session is open; the script sends
  session-close on exit which unlocks it. If it was killed hard, power-cycle
  the device.
- **Output won't turn on, protection shows LVP** — input below 5 V; the
  DPS-150 is a buck converter and also needs input > desired output.

## Protocol notes

Frame: `F1 <cmd> <type> <len> <payload…> <checksum>`, checksum =
(type + len + payload bytes) mod 256. Device replies start `F0 A1`.
Floats are IEEE-754 little-endian. Key registers: 193 Vset, 194 Iset,
219 output enable, 255 "get everything" (139-byte reply). Full field map
is in `DPS150._parse()`. Credit to cho45/fnirsi-dps-150 and
svenk123/dps150tool for the reverse-engineering groundwork.
