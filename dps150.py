#!/usr/bin/env python3
"""
dps150.py - Linux/macOS/Windows command-line control utility for the
            FNIRSI DPS-150 programmable DC power supply.

Protocol reverse-engineered by the community (see cho45/fnirsi-dps-150
and svenk123/dps150tool). Serial: 115200 8N1 over the Micro-USB data port.

Frame format (both directions):
    [0] header   0xF1 host->device, 0xF0 device->host
    [1] command  0xA1 get, 0xB1 set, 0xB0 baud, 0xC1 session
    [2] type     register / field id
    [3] len      payload length
    [4..]        payload (floats are IEEE754 little-endian)
    [last]       checksum = (type + len + payload bytes) % 256

Dependency:  pip install pyserial
License: MIT
"""

import argparse
import json
import os
import signal
import struct
import sys
import time
from datetime import datetime

__version__ = "1.2.0"

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("error: pyserial is required ->  pip install pyserial")

# ---------------------------------------------------------------- device identity
# The DPS-150 enumerates as a USB CDC serial device with these IDs:
DPS150_USB_IDS = {(0x2E3C, 0x5740)}

# Serial number of *your* unit (from the USB descriptor). Used to prefer /
# verify the exact device when auto-detecting. Override with -s/--serial or
# the DPS150_SERIAL environment variable. Set to None to disable.
KNOWN_SERIAL = None

# ---------------------------------------------------------------- protocol
HDR_OUT = 0xF1
HDR_IN = 0xF0

CMD_GET = 0xA1
CMD_BAUD = 0xB0
CMD_SET = 0xB1
CMD_SESSION = 0xC1

# float registers
REG_VSET = 193
REG_ISET = 194
REG_GROUP_BASE = 197        # g1 V=197 I=198 ... g6 V=207 I=208
REG_OVP, REG_OCP, REG_OPP, REG_OTP, REG_LVP = 209, 210, 211, 212, 213
# byte registers
REG_BRIGHTNESS = 214
REG_VOLUME = 215
REG_METERING = 216
REG_OUTPUT_ENABLE = 219
# read-only ids
REG_INPUT_VOLTAGE = 192
REG_OUT_VIP = 195
REG_TEMPERATURE = 196
REG_CAPACITY = 217
REG_ENERGY = 218
REG_PROTECTION = 220
REG_CVCC = 221
REG_MODEL = 222
REG_HW_VER = 223
REG_FW_VER = 224
REG_UPPER_VLIMIT = 226
REG_UPPER_ILIMIT = 227
REG_ALL = 255

PROT_STATES = ["OK", "OVP", "OCP", "OPP", "OTP", "LVP", "REP"]
PROT_DESC = [
    "Normal operation",
    "Over-voltage protection tripped",
    "Over-current protection tripped",
    "Over-power protection tripped",
    "Over-temperature protection tripped",
    "Input under-voltage lockout (input too low)",
    "Reverse connection protection tripped",
]

VERBOSE = False


def log(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


def f32(data, off=0):
    return struct.unpack_from("<f", data, off)[0]


# ---------------------------------------------------------------- discovery
def find_devices(serial_number=None):
    """Return list of ListPortInfo for connected DPS-150s.
    If serial_number given, only that unit (case-insensitive match)."""
    found = []
    for p in list_ports.comports():
        if p.vid is None or (p.vid, p.pid) not in DPS150_USB_IDS:
            continue
        if serial_number and (p.serial_number or "").lower() != serial_number.lower():
            continue
        found.append(p)
    return found


def resolve_port(explicit_port, serial_number):
    """Decide which serial port to use.
    Priority: explicit -p > exact serial match > single detected device.
    Returns (port_name, usb_serial_or_None)."""
    if explicit_port:
        # even with an explicit port, report the USB serial if we can see it
        for p in list_ports.comports():
            if p.device == explicit_port:
                return explicit_port, p.serial_number
        return explicit_port, None

    want = serial_number or KNOWN_SERIAL

    if want:
        hits = find_devices(want)
        if len(hits) == 1:
            return hits[0].device, hits[0].serial_number
        if len(hits) > 1:  # should not happen (serials are unique)
            sys.exit(f"error: multiple devices report serial {want}: "
                     + ", ".join(p.device for p in hits))
        if serial_number:  # user explicitly asked for this unit -> hard fail
            others = find_devices()
            msg = f"error: no DPS-150 with serial {serial_number} found."
            if others:
                msg += "\ndetected DPS-150 units:\n" + "\n".join(
                    f"  {p.device}  serial={p.serial_number}" for p in others)
            sys.exit(msg)
        # KNOWN_SERIAL not found -> fall through to generic detection
        log(f"note: known unit {want} not present, scanning for any DPS-150")

    hits = find_devices()
    if len(hits) == 1:
        return hits[0].device, hits[0].serial_number
    if len(hits) > 1:
        sys.exit("error: multiple DPS-150 devices found, pick one with "
                 "-s <serial> or -p <port>:\n" + "\n".join(
                     f"  {p.device}  serial={p.serial_number}" for p in hits))
    sys.exit("error: no DPS-150 found on any USB port "
             "(VID:PID 2E3C:5740). Is it plugged into the Micro-USB DATA "
             "port with a data cable? You can also force a port with -p.")


# ---------------------------------------------------------------- device
class DPS150:
    def __init__(self, port, timeout=0.1):
        self.ser = serial.Serial(
            port=port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            rtscts=False,
        )
        self.buf = bytearray()
        self.state = {}
        self.usb_serial = None

    # ---- low level -------------------------------------------------
    def send(self, cmd, type_, data):
        if isinstance(data, int):
            data = bytes([data])
        frame = bytearray([HDR_OUT, cmd, type_, len(data)])
        frame += data
        frame.append((type_ + len(data) + sum(data)) % 256)
        log("tx: " + frame.hex(" "))
        self.ser.write(frame)
        self.ser.flush()
        time.sleep(0.05)  # device needs a short gap between commands

    def send_float(self, type_, value):
        self.send(CMD_SET, type_, struct.pack("<f", value))

    def read_frames(self, timeout, want_type=None, raw_dump=False):
        """Pump incoming bytes, parse valid frames into self.state.
        Returns number of valid frames. Stops early once want_type seen."""
        deadline = time.monotonic() + timeout
        frames = 0
        while time.monotonic() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                self.buf += chunk
            i = 0
            while i <= len(self.buf) - 5:
                if self.buf[i] != HDR_IN or self.buf[i + 1] != CMD_GET:
                    i += 1
                    continue
                type_, ln = self.buf[i + 2], self.buf[i + 3]
                if i + 5 + ln > len(self.buf):
                    break  # incomplete frame, wait for more bytes
                payload = bytes(self.buf[i + 4:i + 4 + ln])
                cks = self.buf[i + 4 + ln]
                if (type_ + ln + sum(payload)) % 256 != cks:
                    i += 1  # bad checksum, resync
                    continue
                if raw_dump:
                    print(f"frame type={type_:3d} len={ln:3d} data={payload.hex(' ')}")
                log(f"rx: type={type_} len={ln} ok")
                self._parse(type_, payload)
                frames += 1
                i += 5 + ln
                if want_type is not None and type_ == want_type:
                    del self.buf[:i]
                    return frames
            del self.buf[:i]
            if len(self.buf) > 8192:
                self.buf.clear()  # overflow safety
        return frames

    # ---- payload parsing --------------------------------------------
    def _parse(self, type_, d):
        s = self.state
        try:
            if type_ == REG_INPUT_VOLTAGE:
                s["input_voltage"] = f32(d)
            elif type_ == REG_OUT_VIP:
                s["output_voltage"] = f32(d, 0)
                s["output_current"] = f32(d, 4)
                s["output_power"] = f32(d, 8)
            elif type_ == REG_TEMPERATURE:
                s["temperature"] = f32(d)
            elif type_ == REG_CAPACITY:
                s["capacity_ah"] = f32(d)
            elif type_ == REG_ENERGY:
                s["energy_wh"] = f32(d)
            elif type_ == REG_OUTPUT_ENABLE:
                s["output_on"] = d[0] == 1
            elif type_ == REG_PROTECTION:
                s["protection"] = PROT_STATES[d[0]] if d[0] < 7 else str(d[0])
            elif type_ == REG_CVCC:
                s["mode"] = "CV" if d[0] else "CC"
            elif type_ == REG_MODEL:
                s["model"] = d.decode(errors="replace")
            elif type_ == REG_HW_VER:
                s["hardware"] = d.decode(errors="replace")
            elif type_ == REG_FW_VER:
                s["firmware"] = d.decode(errors="replace")
            elif type_ == REG_ALL and len(d) >= 123:
                s["input_voltage"] = f32(d, 0)
                s["set_voltage"] = f32(d, 4)
                s["set_current"] = f32(d, 8)
                s["output_voltage"] = f32(d, 12)
                s["output_current"] = f32(d, 16)
                s["output_power"] = f32(d, 20)
                s["temperature"] = f32(d, 24)
                s["groups"] = [
                    {"voltage": f32(d, 28 + g * 8), "current": f32(d, 32 + g * 8)}
                    for g in range(6)
                ]
                s["ovp"] = f32(d, 76)
                s["ocp"] = f32(d, 80)
                s["opp"] = f32(d, 84)
                s["otp"] = f32(d, 88)
                s["lvp"] = f32(d, 92)
                s["brightness"] = d[96]
                s["volume"] = d[97]
                s["metering_on"] = d[98] == 0  # open=0 means running
                s["capacity_ah"] = f32(d, 99)
                s["energy_wh"] = f32(d, 103)
                s["output_on"] = d[107] == 1
                p = d[108]
                s["protection"] = PROT_STATES[p] if p < 7 else str(p)
                s["protection_detail"] = PROT_DESC[p] if p < 7 else "unknown"
                s["mode"] = "CV" if d[109] else "CC"
                s["upper_limit_voltage"] = f32(d, 111)
                s["upper_limit_current"] = f32(d, 115)
                s["_have_all"] = True
            else:
                log(f"rx: unhandled type {type_} len {len(d)}: {d.hex(' ')}")
        except (struct.error, IndexError) as e:
            log(f"rx: parse error on type {type_}: {e}")

    # ---- high level --------------------------------------------------
    def session_start(self):
        # announce host connection; device locks its front panel while connected
        self.send(CMD_SESSION, 0, 1)
        self.read_frames(0.2)  # swallow connect burst

    def session_end(self):
        # release front-panel lock
        try:
            self.send(CMD_SESSION, 0, 0)
        except serial.SerialException:
            pass

    def fetch_info(self, timeout=0.8):
        self.send(CMD_GET, REG_MODEL, 0)
        self.send(CMD_GET, REG_HW_VER, 0)
        self.send(CMD_GET, REG_FW_VER, 0)
        self.read_frames(timeout)

    def fetch_all(self, timeout=1.5, retries=1):
        for _ in range(retries + 1):
            self.state.pop("_have_all", None)
            self.send(CMD_GET, REG_ALL, 0)
            self.read_frames(timeout, want_type=REG_ALL)
            if self.state.get("_have_all"):
                return True
        return False

    def set_output(self, on):
        self.send(CMD_SET, REG_OUTPUT_ENABLE, 1 if on else 0)

    def set_voltage(self, v):
        self.send_float(REG_VSET, v)

    def set_current(self, a):
        self.send_float(REG_ISET, a)

    def close(self):
        self.session_end()
        self.ser.close()


# ---------------------------------------------------------------- output
def print_status_human(s):
    prot = s.get("protection", "OK")
    detail = s.get("protection_detail", "")
    in_v = s.get("input_voltage", 0.0)
    print(f"Device        : {s.get('model', 'DPS-150')}  "
          f"(HW {s.get('hardware', '?')}, FW {s.get('firmware', '?')})")
    if s.get("usb_serial"):
        print(f"USB serial    : {s['usb_serial']}   (port {s.get('port', '?')})")
    print("-" * 46)
    warn = "  (below 5V - undervoltage lockout!)" if in_v < 5.0 else ""
    print(f"Input voltage : {in_v:8.2f} V{warn}")
    print(f"Output        : {'ON' if s.get('output_on') else 'OFF'}")
    mode = s.get("mode", "?")
    print(f"Mode          : {mode} ({'constant voltage' if mode == 'CV' else 'constant current'})")
    print(f"Protection    : {prot} - {detail}")
    print("-" * 46)
    print(f"Set voltage   : {s.get('set_voltage', 0):8.2f} V   "
          f"(upper limit {s.get('upper_limit_voltage', 0):.2f} V)")
    print(f"Set current   : {s.get('set_current', 0):8.3f} A   "
          f"(upper limit {s.get('upper_limit_current', 0):.3f} A)")
    print(f"Out voltage   : {s.get('output_voltage', 0):8.2f} V")
    print(f"Out current   : {s.get('output_current', 0):8.3f} A")
    print(f"Out power     : {s.get('output_power', 0):8.2f} W")
    print(f"Temperature   : {s.get('temperature', 0):8.1f} C")
    print("-" * 46)
    print(f"Metering      : {'on' if s.get('metering_on') else 'off'}   "
          f"capacity {s.get('capacity_ah', 0):.3f} Ah   "
          f"energy {s.get('energy_wh', 0):.3f} Wh")
    print(f"Protections   : OVP {s.get('ovp', 0):.2f}V  OCP {s.get('ocp', 0):.3f}A  "
          f"OPP {s.get('opp', 0):.2f}W  OTP {s.get('otp', 0):.0f}C  LVP {s.get('lvp', 0):.2f}V")
    print(f"Brightness    : {s.get('brightness', 0)}    Volume: {s.get('volume', 0)}")
    print("Preset groups :")
    for i, g in enumerate(s.get("groups", []), 1):
        print(f"   M{i}: {g['voltage']:6.2f} V  {g['current']:6.3f} A")


def clean_state(s):
    return {k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in s.items() if not k.startswith("_")}


# ---------------------------------------------------------------- CLI
def verify_setpoints(dev, requested_voltage=None, requested_current=None):
    """Command-and-verify: after a write, read the device state back and
    report anything that prevents the setting from taking effect.
    Returns True if the device state matches the request."""
    if not dev.fetch_all(timeout=1.2, retries=1):
        print("warning: command sent, but device status could not be verified",
              file=sys.stderr)
        return False

    s = dev.state
    ok = True
    input_v = s.get("input_voltage")
    max_v = s.get("upper_limit_voltage")
    max_a = s.get("upper_limit_current")
    actual_v = s.get("set_voltage")
    actual_a = s.get("set_current")
    protection = s.get("protection", "unknown")

    if requested_voltage is not None:
        if actual_v is not None and abs(actual_v - requested_voltage) > 0.05:
            print(f"warning: requested {requested_voltage:.2f} V, "
                  f"but device reports Vset={actual_v:.2f} V", file=sys.stderr)
            ok = False
        if max_v is not None and requested_voltage > max_v + 0.05:
            print("\nwarning: requested voltage exceeds the current device limit",
                  file=sys.stderr)
            print(f"  requested     : {requested_voltage:.2f} V", file=sys.stderr)
            print(f"  device limit  : {max_v:.2f} V", file=sys.stderr)
            if input_v is not None:
                print(f"  input voltage : {input_v:.2f} V", file=sys.stderr)
            print(f"  protection    : {protection}", file=sys.stderr)
            if protection == "LVP" or (input_v is not None and input_v < 5.0):
                print("  cause         : external input power is missing or too low",
                      file=sys.stderr)
                print("  action        : connect sufficient input power "
                      "(input must exceed the desired output)", file=sys.stderr)
            elif input_v is not None and requested_voltage > input_v:
                print("  cause         : buck converter - output cannot exceed input",
                      file=sys.stderr)
                print(f"  action        : supply more than {requested_voltage:.1f} V "
                      "at the input", file=sys.stderr)
            ok = False

    if requested_current is not None:
        if actual_a is not None and abs(actual_a - requested_current) > 0.01:
            print(f"warning: requested {requested_current:.3f} A, "
                  f"but device reports Iset={actual_a:.3f} A", file=sys.stderr)
            ok = False
        if max_a is not None and requested_current > max_a + 0.01:
            print("\nwarning: requested current exceeds the current device limit",
                  file=sys.stderr)
            print(f"  requested     : {requested_current:.3f} A", file=sys.stderr)
            print(f"  device limit  : {max_a:.3f} A", file=sys.stderr)
            print(f"  protection    : {protection}", file=sys.stderr)
            print("  note          : the limit is negotiated from the input "
                  "source (USB-PD wattage)", file=sys.stderr)
            ok = False

    if protection not in ("OK", "unknown") and ok:
        # setpoints accepted, but a protection state is active
        print(f"note: device is in protection state {protection} - "
              f"{s.get('protection_detail', '')}", file=sys.stderr)

    return ok


def frange_check(v, lo, hi, what):
    if not (lo <= v <= hi):
        sys.exit(f"error: {what} {v:.3f} out of range ({lo:.2f}..{hi:.2f})")


def main():
    global VERBOSE
    epilog = """commands:
  list                       list detected DPS-150 devices (port + USB serial)
  status                     show full device status
  on                         enable output
  off                        disable output
  set <volts> <amps>         set output voltage and current limit
  voltage <volts>            set output voltage (0-30)
  current <amps>             set output current limit (0-5)
  monitor [interval_s]       continuously poll and print status lines
  raw [seconds]              dump every decoded frame (debugging, default 10 s)
  info                       model / hardware / firmware versions
  group <1-6> <V> <A>        program a preset group
  ovp|ocp|opp|otp|lvp <val>  set a protection threshold
  brightness <1-14>          set screen brightness
  volume <0-10>              set beeper volume
  metering on|off            start/stop Ah/Wh metering

examples:
  dps150.py list                      # find your device(s)
  dps150.py set 12.0 1.5              # auto-detects a single connected unit
  dps150.py -s 13BD898A2565 on        # address a specific unit by USB serial
  dps150.py -p COM5 status            # force a port explicitly
  dps150.py -j status | jq .output_voltage
"""
    ap = argparse.ArgumentParser(
        description="FNIRSI DPS-150 command-line control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    ap.add_argument("--version", action="version",
                    version=f"dps150.py {__version__}")
    ap.add_argument("-p", "--port",
                    default=os.environ.get("DPS150_PORT"),
                    help="serial port (e.g. COM5, /dev/ttyACM0). Default: "
                         "$DPS150_PORT, else auto-detect by USB VID:PID/serial")
    ap.add_argument("-s", "--serial",
                    default=os.environ.get("DPS150_SERIAL"),
                    help="select device by USB serial number "
                         "(default: $DPS150_SERIAL, else auto-detect)")
    ap.add_argument("-j", "--json", action="store_true", help="JSON output")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="dump raw tx/rx traffic to stderr")
    ap.add_argument("command", help="see command list below")
    ap.add_argument("args", nargs="*", help="command arguments")
    opts = ap.parse_args()
    VERBOSE = opts.verbose
    cmd, args = opts.command, opts.args

    # device discovery command works without opening anything
    if cmd == "list":
        hits = find_devices()
        if not hits:
            print("no DPS-150 devices found (USB VID:PID 2E3C:5740)")
            sys.exit(1)
        for p in hits:
            mark = " <- known unit" if KNOWN_SERIAL and \
                   (p.serial_number or "").lower() == KNOWN_SERIAL.lower() else ""
            print(f"{p.device}  serial={p.serial_number}  "
                  f"vid:pid={p.vid:04X}:{p.pid:04X}{mark}")
        sys.exit(0)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    port, usb_serial = resolve_port(opts.port, opts.serial)
    log(f"using port {port} (usb serial: {usb_serial})")

    try:
        dev = DPS150(port)
    except (serial.SerialException, OSError) as e:
        hint = ("\nhint: add yourself to the serial group: "
                "sudo usermod -aG dialout $USER  (re-login)"
                if "Permission" in str(e) or "denied" in str(e).lower() else "")
        sys.exit(f"error: cannot open {port}: {e}{hint}")
    dev.usb_serial = usb_serial

    rc = 0
    try:
        dev.session_start()

        if cmd == "status":
            dev.fetch_info(0.3)
            if not dev.fetch_all():
                sys.exit(f"error: no response from device "
                         f"(check cable is a DATA cable and port {dev.ser.port})")
            dev.state["usb_serial"] = dev.usb_serial
            dev.state["port"] = dev.ser.port
            if opts.json:
                print(json.dumps(clean_state(dev.state)))
            else:
                print_status_human(dev.state)

        elif cmd in ("on", "off"):
            want = cmd == "on"
            dev.set_output(want)
            if dev.fetch_all():
                s = dev.state
                print(f"output: {'ON' if s['output_on'] else 'OFF'} "
                      f"(measured {s['output_voltage']:.2f} V, {s['output_current']:.3f} A)")
                if s["output_on"] != want:
                    print(f"warning: device did not switch "
                          f"(protection state: {s.get('protection')})", file=sys.stderr)
                    rc = 1
            else:
                print(f"output {'enable' if want else 'disable'} command sent")

        elif cmd == "voltage" and len(args) == 1:
            v = float(args[0])
            frange_check(v, 0, 30.0, "voltage")
            dev.set_voltage(v)
            if verify_setpoints(dev, requested_voltage=v):
                print(f"voltage set and verified: {v:.2f} V")
            else:
                print(f"voltage command sent: {v:.2f} V")
                rc = 1

        elif cmd == "current" and len(args) == 1:
            a = float(args[0])
            frange_check(a, 0, 5.1, "current")
            dev.set_current(a)
            if verify_setpoints(dev, requested_current=a):
                print(f"current set and verified: {a:.3f} A")
            else:
                print(f"current command sent: {a:.3f} A")
                rc = 1

        elif cmd == "set" and len(args) == 2:
            v, a = float(args[0]), float(args[1])
            frange_check(v, 0, 30.0, "voltage")
            frange_check(a, 0, 5.1, "current")
            dev.set_voltage(v)
            dev.set_current(a)
            if verify_setpoints(dev, requested_voltage=v, requested_current=a):
                print(f"set and verified: {v:.2f} V, {a:.3f} A")
            else:
                print(f"set commands sent: {v:.2f} V, {a:.3f} A")
                rc = 1

        elif cmd == "group" and len(args) == 3:
            g = int(args[0])
            v, a = float(args[1]), float(args[2])
            if not 1 <= g <= 6:
                sys.exit("error: group must be 1-6")
            frange_check(v, 0, 30.0, "voltage")
            frange_check(a, 0, 5.1, "current")
            dev.send_float(REG_GROUP_BASE + (g - 1) * 2, v)
            dev.send_float(REG_GROUP_BASE + (g - 1) * 2 + 1, a)
            print(f"group M{g}: {v:.2f} V, {a:.3f} A")

        elif cmd in ("ovp", "ocp", "opp", "otp", "lvp") and len(args) == 1:
            v = float(args[0])
            reg = {"ovp": REG_OVP, "ocp": REG_OCP, "opp": REG_OPP,
                   "otp": REG_OTP, "lvp": REG_LVP}[cmd]
            hi = {"ovp": 30.0, "ocp": 5.1, "opp": 150.0, "otp": 99.0, "lvp": 30.0}[cmd]
            frange_check(v, 0, hi, cmd)
            dev.send_float(reg, v)
            print(f"{cmd} set to {v:.2f}")

        elif cmd == "brightness" and len(args) == 1:
            b = int(args[0])
            if not 1 <= b <= 14:
                sys.exit("error: brightness 1-14")
            dev.send(CMD_SET, REG_BRIGHTNESS, b)

        elif cmd == "volume" and len(args) == 1:
            b = int(args[0])
            if not 0 <= b <= 10:
                sys.exit("error: volume 0-10")
            dev.send(CMD_SET, REG_VOLUME, b)

        elif cmd == "metering" and len(args) == 1:
            if args[0] not in ("on", "off"):
                sys.exit("error: metering must be on or off")
            on = args[0] == "on"
            dev.send(CMD_SET, REG_METERING, 1 if on else 0)
            print(f"metering: {'on' if on else 'off'}")

        elif cmd == "info":
            dev.fetch_info()
            s = dev.state
            print(f"model: {s.get('model', '?')}")
            print(f"hardware: {s.get('hardware', '?')}")
            print(f"firmware: {s.get('firmware', '?')}")

        elif cmd == "monitor":
            interval = max(0.2, float(args[0]) if args else 1.0)
            if not opts.json:
                print(f"{'time':<19} {'in_V':>8} {'out_V':>8} {'out_A':>8} "
                      f"{'out_W':>8} {'temp':>6} {'out':>4} {'mode':>4} prot")
            while not stop["flag"]:
                if dev.fetch_all(timeout=1.2, retries=0):
                    s = dev.state
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    if opts.json:
                        print(json.dumps({
                            "ts": ts,
                            "usb_serial": dev.usb_serial,
                            "input_voltage": round(s["input_voltage"], 3),
                            "output_voltage": round(s["output_voltage"], 3),
                            "output_current": round(s["output_current"], 4),
                            "output_power": round(s["output_power"], 3),
                            "temperature": round(s["temperature"], 1),
                            "output_on": s["output_on"],
                            "mode": s["mode"],
                            "protection": s["protection"],
                        }), flush=True)
                    else:
                        print(f"{ts:<19} {s['input_voltage']:8.2f} "
                              f"{s['output_voltage']:8.2f} {s['output_current']:8.3f} "
                              f"{s['output_power']:8.2f} {s['temperature']:6.1f} "
                              f"{'ON' if s['output_on'] else 'off':>4} {s['mode']:>4} "
                              f"{s['protection']}", flush=True)
                else:
                    print("warning: poll timed out", file=sys.stderr)
                time.sleep(interval)

        elif cmd == "raw":
            secs = int(args[0]) if args else 10
            print(f"dumping decoded frames for {secs} s (Ctrl-C to stop)...",
                  file=sys.stderr)
            dev.send(CMD_GET, REG_ALL, 0)
            dev.read_frames(secs, raw_dump=True)

        else:
            ap.print_help()
            rc = 2

    except (ValueError, IndexError):
        sys.exit(f"error: bad arguments for '{cmd}' (see --help)")
    except serial.SerialException as e:
        sys.exit(f"error: serial I/O failed: {e}")
    finally:
        dev.close()

    sys.exit(rc)


if __name__ == "__main__":
    main()
