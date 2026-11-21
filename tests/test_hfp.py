import random
import socket
import unittest
import _path  # noqa: F401
import hfp_ag as h


class Fake:
    """Minimal link: collects what the gateway sends."""
    def __init__(self, chunks=(), exc=None):
        self.sent, self.chunks, self.exc = [], list(chunks), exc
    def settimeout(self, t): pass
    def send(self, b): self.sent.append(b)
    def recv(self, n):
        if self.exc:
            raise self.exc
        if self.chunks:
            return self.chunks.pop(0)
        raise socket.timeout()


class HfpTests(unittest.TestCase):
    def test_normal_command_exchange(self):
        ag = h.AudioGateway(Fake([b"AT+BRSF=21\r"]))
        ag.pump(0.1)
        self.assertEqual(ag.hf_features, 21)
        self.assertIn(b"+BRSF", b"".join(ag.link.sent))
        self.assertIn(b"OK", b"".join(ag.link.sent))

    def test_malformed_commands_answer_error_and_never_raise(self):
        ag = h.AudioGateway(Fake())
        for cmd in ("AT+VGS=", "AT+VGM=x", "AT+BRSF=", "AT+BRSF=abc", "AT+BCS=", "AT+CMER=", "AT+BAC=,,", "AT+CKPD=", "", "AT", "ATD"):
            ag.handle(cmd)
        self.assertTrue(any("malformed" in e[1] for e in ag.events))

    def test_timeout_is_not_an_error(self):
        self.assertEqual(h.AudioGateway(Fake()).read_commands(0.05), [])

    def test_real_socket_error_is_reported(self):
        with self.assertRaises(h.HfpError):
            h.AudioGateway(Fake(exc=ConnectionResetError("reset"))).read_commands(0.05)

    def test_link_closed_by_peer_is_reported(self):
        with self.assertRaises(h.HfpError):
            h.AudioGateway(Fake([b""])).read_commands(0.05)

    def test_partial_command_is_buffered_until_complete(self):
        ag = h.AudioGateway(Fake([b"AT+CI", b"ND?\r"]))
        self.assertEqual(ag.read_commands(0.05), [])
        self.assertEqual(ag.read_commands(0.05), ["AT+CIND?"])

    def test_at_fuzz_never_raises(self):
        rng = random.Random(9)
        alphabet = "ATD+=?,;0123456789CIMERBSGHLVNPX \"x-"
        for _ in range(20000):
            cmd = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
            h.AudioGateway(Fake()).handle(cmd)

    def test_call_flow_state(self):
        ag = h.AudioGateway(Fake())
        ag.handle("AT+BRSF=%d" % h.HF_CLIP)
        ag.handle("AT+CMER=3,0,0,1")
        self.assertTrue(ag.slc)
        ag.handle("ATA")
        self.assertTrue(ag.answered)
        ag.handle("AT+CHUP")
        self.assertTrue(ag.hung_up)


if __name__ == "__main__":
    unittest.main()
