#!/usr/bin/env python3
"""Self-test for hfp_ag.py: a scripted simulated Hands-Free unit talks to the
AudioGateway over a socketpair. Needs no Bluetooth hardware. Exit 0 = pass."""
import socket, sys, threading, time
import hfp_ag as h

def hf_script(sock, out):
    def tx(s): sock.send((s + "\r").encode())
    seen = []
    def rx(t=2.0):
        sock.settimeout(t); b = b""
        try:
            while b"OK" not in b and b"ERROR" not in b:
                b += sock.recv(512)
        except Exception: pass
        seen.append(b.decode())
        return b.decode()
    tx("AT+BRSF=%d" % (h.HF_CLIP | h.HF_VOL)); out["brsf"] = rx()
    tx("AT+CIND=?"); out["cind_t"] = rx()
    tx("AT+CIND?"); out["cind"] = rx()
    tx("AT+CMER=3,0,0,1"); out["cmer"] = rx()
    tx("AT+CHLD=?"); out["chld"] = rx()
    tx("AT+VGS=9"); rx(); tx("AT+VGM=5"); rx()
    sock.settimeout(3.0)
    while "RING" not in "".join(seen):
        seen.append(sock.recv(512).decode())
    out["ring"] = "".join(seen)
    tx("ATA"); out["ata"] = rx()
    tx("AT+CLCC"); out["clcc"] = rx()
    tx("AT+FOO"); out["unknown"] = rx()
    tx("AT+CHUP"); out["chup"] = rx()

a_sock, b_sock = socket.socketpair()
out = {}
t = threading.Thread(target=hf_script, args=(b_sock, out), daemon=True); t.start()
ag = h.AudioGateway(a_sock)
assert ag.establish_slc(5), "SLC did not complete"
ag.pump(0.8)
ag.ring("+15551234567")
end = time.time() + 3
while time.time() < end and not ag.answered: ag.pump(0.2)
end = time.time() + 4
while time.time() < end and not ag.hung_up: ag.pump(0.2)
t.join(5)
checks = {
  "BRSF reply": "+BRSF: %d" % ag.features in out["brsf"] and "OK" in out["brsf"],
  "CIND test lists callsetup": '"callsetup",(0-3)' in out["cind_t"],
  "CIND status": "+CIND: 1,0,0,0,5,0,5" in out["cind"],
  "CMER accepted": "OK" in out["cmer"] and ag.cmer_on,
  "volume captured": (ag.speaker, ag.mic) == (9, 5),
  "RING + CLIP": "RING" in out["ring"] and "+CLIP" in out["ring"] and "+15551234567" in out["ring"],
  "ATA answers + call=1": ag.answered and "ciev call=1" in [e[1] for e in ag.events] and "OK" in out["ata"],
  "CLCC lists active call": "+CLCC: 1,0,0,0,0" in out["clcc"],
  "unknown AT -> ERROR": "ERROR" in out["unknown"],
  "CHUP ends call": ag.hung_up and ag.cind["call"] == 0,
}
bad = [k for k, v in checks.items() if not v]
for k, v in checks.items(): print(("PASS " if v else "FAIL ") + k)
print("timeline:", ag.events)
sys.exit(1 if bad else 0)
