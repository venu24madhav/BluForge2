#!/usr/bin/env python3
"""
hfp_ag.py - Hands-Free Profile (HFP 1.7) *Audio Gateway* side, protocol only.

This host plays the phone; the DUT (headset / car kit) is the Hands-Free unit.
The class below is pure protocol over any object with send(bytes)/recv(n) and
a settimeout() (an RFCOMM socket in production, a socketpair in the self-test),
so the AT state machine is testable without Bluetooth hardware.

Implements: SLC establishment (AT+BRSF, AT+BAC, AT+CIND=?, AT+CIND?, AT+CMER,
AT+CHLD=?), volume sync (AT+VGS/AT+VGM), codec connection (AT+BCS), caller-id
(AT+CLIP), call control (ATA, AT+CHUP, ATD, AT+CLCC), HSP button (AT+CKPD),
and AG-initiated incoming-call / call-active / call-end indicators.

Not implemented (reported as ERROR to the HF, as the spec allows): three-way
calling (+CHLD actions), voice recognition, enhanced call control.
"""
import re
import socket
import time

# AG feature bits (HFP 1.7 sec 4.34.2)
AG_THREEWAY, AG_ECNR, AG_VOICE_RECOG, AG_INBAND_RING, AG_VOICE_TAG, AG_REJECT_CALL, \
    AG_ENH_CALL_STATUS, AG_ENH_CALL_CTRL, AG_EXT_ERR, AG_CODEC_NEG = [1 << i for i in range(10)]
# HF feature bits
HF_ECNR, HF_THREEWAY, HF_CLIP, HF_VOICE_RECOG, HF_VOL, HF_ENH_STATUS, HF_ENH_CTRL, HF_CODEC_NEG = \
    [1 << i for i in range(8)]

INDICATORS = [("service", 0, 1), ("call", 0, 1), ("callsetup", 0, 3),
              ("callheld", 0, 2), ("signal", 0, 5), ("roam", 0, 1), ("battchg", 0, 5)]
IDX = {n: i + 1 for i, (n, _, _) in enumerate(INDICATORS)}


class HfpError(Exception):
    pass


class AudioGateway:
    def __init__(self, link, features=AG_REJECT_CALL | AG_EXT_ERR, log=None):
        self.link = link
        self.features = features
        self.log = log or (lambda *a: None)
        self.buf = b""
        self.hf_features = 0
        self.hf_codecs = [1]
        self.cmer_on = False
        self.slc = False
        self.cind = {"service": 1, "call": 0, "callsetup": 0, "callheld": 0, "signal": 5, "roam": 0, "battchg": 5}
        self.number = "+10000000000"
        self.speaker = None
        self.mic = None
        self.codec = 1                      # 1 CVSD, 2 mSBC
        self.events = []                    # (time, what) protocol timeline
        self.answered = False
        self.hung_up = False
        self.t_start = time.time()

    # ---- transport
    def _mark(self, what):
        self.events.append((round(time.time() - self.t_start, 3), what))

    def send(self, line):
        self.log("AG> " + line)
        self.link.send(("\r\n%s\r\n" % line).encode())

    def ok(self):
        self.send("OK")

    def err(self):
        self.send("ERROR")

    def read_commands(self, timeout):
        """Return complete AT commands received within `timeout` seconds."""
        self.link.settimeout(timeout)
        try:
            data = self.link.recv(512)
        except (socket.timeout, BlockingIOError):
            return []                      # nothing arrived within `timeout`: normal
        except OSError as e:               # a real socket failure used to be hidden here as "no data"
            raise HfpError("RFCOMM link error: %s" % e)
        if not data:
            raise HfpError("RFCOMM link closed by the DUT")
        self.buf += data
        cmds = []
        while b"\r" in self.buf:
            line, self.buf = self.buf.split(b"\r", 1)
            line = line.strip(b"\n ").decode("ascii", "replace")
            if line:
                cmds.append(line)
        return cmds

    # ---- commands from the HF
    def handle(self, cmd):
        """Process one AT command. A malformed one (for example 'AT+VGS=' or 'AT+BRSF=x') answers ERROR and is
        noted in the timeline; it must never raise out of here and take the whole test down."""
        try:
            self._handle(cmd)
        except (ValueError, IndexError):
            self.err()
            self._mark("malformed:" + cmd)

    def _handle(self, cmd):
        self.log("HF> " + cmd)
        u = cmd.upper()
        if u.startswith("AT+BRSF="):
            self.hf_features = int(u.split("=")[1])
            self.send("+BRSF: %d" % self.features)
            self.ok()
        elif u.startswith("AT+BAC="):
            self.hf_codecs = [int(x) for x in u.split("=")[1].split(",") if x.strip().isdigit()]
            self.ok()
        elif u == "AT+CIND=?":
            self.send("+CIND: " + ",".join('("%s",(%d%s%d))' % (n, lo, "-" if hi - lo > 1 else ",", hi)
                                           for n, lo, hi in INDICATORS))
            self.ok()
        elif u == "AT+CIND?":
            self.send("+CIND: " + ",".join(str(self.cind[n]) for n, _, _ in INDICATORS))
            self.ok()
        elif u.startswith("AT+CMER="):
            self.cmer_on = u.split("=")[1].split(",")[:4] == ["3", "0", "0", "1"]
            self.ok()
            self._after_cmer()
        elif u == "AT+CHLD=?":
            self.send("+CHLD: (0,1)")
            self.ok()
        elif u.startswith("AT+CHLD="):
            self.err()
        elif u.startswith("AT+VGS="):
            self.speaker = int(u.split("=")[1]); self.ok(); self._mark("vgs=%d" % self.speaker)
        elif u.startswith("AT+VGM="):
            self.mic = int(u.split("=")[1]); self.ok(); self._mark("vgm=%d" % self.mic)
        elif u.startswith("AT+CLIP=") or u.startswith("AT+CCWA=") or u.startswith("AT+NREC=") \
                or u.startswith("AT+BIA=") or u.startswith("AT+CMEE=") or u.startswith("AT+XAPL=") or u.startswith("AT+IPHONEACCEV="):
            self.ok()
        elif u.startswith("AT+BCS="):
            self.codec = int(u.split("=")[1]); self.ok(); self._mark("codec=%d" % self.codec)
        elif u == "ATA":
            self.ok(); self.answered = True; self._mark("answered")
            self.set_ind("call", 1); self.set_ind("callsetup", 0)
        elif u == "AT+CHUP":
            self.ok(); self.hung_up = True; self._mark("hf_hangup")
            self.set_ind("call", 0); self.set_ind("callsetup", 0)
        elif u.startswith("ATD"):
            self.ok(); self._mark("hf_dial")
            self.set_ind("callsetup", 2); self.set_ind("callsetup", 3)
            self.set_ind("call", 1); self.set_ind("callsetup", 0)
        elif u == "AT+CLCC":
            if self.cind["call"]:
                self.send('+CLCC: 1,0,0,0,0,"%s",145' % self.number)
            self.ok()
        elif u.startswith("AT+CKPD="):
            self.ok(); self._mark("hsp_button")
        else:
            self.err()
            self._mark("unsupported:" + cmd)
        if not self.slc and self.hf_features is not None and self._slc_complete():
            self.slc = True
            self._mark("slc_up")

    def _slc_complete(self):
        # CMER enable is the final mandatory step; with codec negotiation the HF
        # also sends AT+BAC first, which we have already handled by then.
        return self.cmer_on

    def _after_cmer(self):
        pass

    def pump(self, timeout=0.2):
        for c in self.read_commands(timeout):
            self.handle(c)

    # ---- AG-initiated
    def set_ind(self, name, value):
        self.cind[name] = value
        if self.cmer_on:
            self.send("+CIEV: %d,%d" % (IDX[name], value))
        self._mark("ciev %s=%d" % (name, value))

    def establish_slc(self, timeout=10.0):
        """Run until the HF has completed SLC setup (or timeout)."""
        end = time.time() + timeout
        while time.time() < end and not self.slc:
            self.pump(0.3)
        return self.slc

    def ring(self, number=None):
        if number:
            self.number = number
        self.set_ind("callsetup", 1)
        self.send("RING")
        if self.hf_features & HF_CLIP:
            self.send('+CLIP: "%s",145' % self.number)
        self._mark("ring")

    def activate_call(self):
        self.set_ind("call", 1)
        self.set_ind("callsetup", 0)

    def end_call(self):
        self.set_ind("call", 0)
        self.set_ind("callsetup", 0)
