#!/usr/bin/env python3
"""Simulate a DPS-150 on a PTY and exercise the DPS-150 CLI.

This integration test requires Unix pseudo-terminals and therefore runs only
on Linux and macOS.
"""

import os
import sys
import unittest

if os.name == "nt":
    raise unittest.SkipTest(
        "test_sim requires Unix pseudo-terminals and cannot run on Windows"
    )

import pty
import struct
import subprocess
import threading
import termios
import tty

def frame(type_, data):
    ln = len(data)
    cks = (type_ + ln + sum(data)) % 256
    return bytes([0xF0, 0xA1, type_, ln]) + bytes(data) + bytes([cks])

def f32(v): return struct.pack('<f', v)

# mutable device model (shared with the device thread)
state = {
    "output_on": 0, "vset": 5.0, "iset": 1.0,
    "input_v": 20.0, "prot": 0,        # 0=OK, 5=LVP
    "limit_v": 24.0, "limit_i": 5.0,
}

def all_frame():
    d = b''
    d += f32(state["input_v"])
    d += f32(state["vset"])
    d += f32(state["iset"])
    d += f32(state["vset"] if state["output_on"] else 0.0)      # out v
    d += f32(0.5 if state["output_on"] else 0.0)                # out i
    d += f32(state["vset"]*0.5 if state["output_on"] else 0.0)  # out p
    d += f32(31.5)                                              # temperature
    for g in range(6):                                          # groups
        d += f32(1.0+g) + f32(0.5)
    d += f32(30.0) + f32(5.0) + f32(150.0) + f32(80.0) + f32(4.5)  # ovp..lvp
    d += bytes([10, 3, 0])          # brightness, volume, metering open=0
    d += f32(0.123) + f32(1.456)    # cap Ah, energy Wh
    d += bytes([state["output_on"], state["prot"], 1, 0])  # out_on, prot, cv, d33
    d += f32(state["limit_v"]) + f32(state["limit_i"])     # upper limits
    d += f32(0)*3                   # unknown tail
    return frame(255, d)

def device(fd):
    buf = b''
    while True:
        try:
            b = os.read(fd, 256)
        except OSError:
            return
        if not b: return
        buf += b
        while len(buf) >= 5:
            if buf[0] != 0xF1:
                buf = buf[1:]; continue
            cmd, typ, ln = buf[1], buf[2], buf[3]
            if len(buf) < 5+ln: break
            payload = buf[4:4+ln]
            buf = buf[5+ln:]
            if cmd == 0xA1:  # GET
                if typ == 255: os.write(fd, all_frame())
                elif typ == 222: os.write(fd, frame(222, b'DPS-150'))
                elif typ == 223: os.write(fd, frame(223, b'V1.0'))
                elif typ == 224: os.write(fd, frame(224, b'V1.2'))
            elif cmd == 0xB1:  # SET
                if typ == 219: state["output_on"] = payload[0]
                elif typ == 193: state["vset"] = struct.unpack('<f', payload)[0]
                elif typ == 194: state["iset"] = struct.unpack('<f', payload)[0]

master, slave = pty.openpty()
tty.setraw(slave, when=termios.TCSANOW)
sname = os.ttyname(slave)
threading.Thread(target=device, args=(master,), daemon=True).start()

failures = 0

def run(*args, expect=0, expect_out=None, expect_err=None):
    global failures
    print(f"\n$ dps150 {' '.join(args)}")
    r = subprocess.run(['python3', './dps150.py', '-p', sname, *args],
                       capture_output=True, text=True, timeout=15)
    print(r.stdout, end='')
    if r.stderr: print("stderr:", r.stderr, end='')
    ok = r.returncode == expect
    if expect_out and expect_out not in r.stdout: ok = False
    if expect_err and expect_err not in r.stderr: ok = False
    if not ok:
        failures += 1
        print(f"  ^^ FAIL (rc={r.returncode}, expected {expect})")
    return r

# --- scenario 1: healthy device, 20 V input --------------------------------
run('info', expect_out='DPS-150')
run('set', '12.5', '2.0', expect_out='set and verified')
run('voltage', '5.0', expect_out='voltage set and verified')
run('current', '0.75', expect_out='current set and verified')
run('on', expect_out='output: ON')
run('status', expect_out='Input voltage')
run('-j', 'status', expect_out='"protection": "OK"')
run('off', expect_out='output: OFF')

# --- scenario 2: USB-only power, input undervoltage (LVP) ------------------
state.update(input_v=3.73, prot=5, limit_v=3.53, limit_i=1.0)
print("\n--- switching simulator to LVP scenario (3.73 V input) ---")
run('voltage', '12', expect=1,
    expect_out='voltage command sent',
    expect_err='exceeds the current device limit')
run('voltage', '3.0', expect_out='voltage set and verified')  # within limit
run('current', '2.0', expect=1,
    expect_err='exceeds the current device limit')
run('set', '12', '2', expect=1, expect_err='protection    : LVP')

print("\nEXIT:", "PASS" if failures == 0 else f"FAIL ({failures})")
sys.exit(1 if failures else 0)
