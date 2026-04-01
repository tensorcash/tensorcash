# SPDX-License-Identifier: Apache-2.0
import time

import pytest


def test_broker_sends_and_stops(monkeypatch):
    import zmq
    from services.verification_api.src import zmq_send_broker as broker_mod

    # Fake socket that always succeeds
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.closed = False
            self.opts = {}
            self.endpoint = None

        def setsockopt(self, opt, val):
            self.opts[opt] = val

        def connect(self, endpoint):
            self.endpoint = endpoint

        def send(self, item, flags=0):
            self.sent.append(item)

        def close(self, *_):
            self.closed = True

    class FakeContext:
        def __init__(self, *_, **__):
            self.termed = False
        def socket(self, *_):
            return FakeSocket()
        def term(self):
            self.termed = True

    # Patch Context factory
    monkeypatch.setattr(broker_mod, "zmq", zmq)
    monkeypatch.setattr(broker_mod.zmq, "Context", lambda *a, **k: FakeContext())

    b = broker_mod.ZmqSendBroker(endpoint="tcp://127.0.0.1:9999", hwm=5, max_queue=10)
    b.start()
    assert b.running

    payload = b"hello"
    assert b.submit(payload) is True

    # Wait for worker to send
    deadline = time.time() + 1.0
    while time.time() < deadline and not getattr(b.sock, "sent", []):
        time.sleep(0.01)

    assert getattr(b.sock, "sent", []) == [payload]

    b.stop()
    assert getattr(b.sock, "closed", False) is True
    assert getattr(b.ctx, "termed", False) is True


def test_broker_backpressure_drop_and_retry_window(monkeypatch):
    import zmq
    from services.verification_api.src import zmq_send_broker as broker_mod

    class FakeSocket:
        def __init__(self, again_sequence):
            self._again_sequence = list(again_sequence)
            self.sent = []
            self.attempts = 0
            self.closed = False
        def setsockopt(self, *_):
            pass
        def connect(self, *_):
            pass
        def send(self, item, flags=0):
            self.attempts += 1
            if self._again_sequence and self._again_sequence.pop(0):
                # Simulate EAGAIN
                raise zmq.Again()
            self.sent.append(item)
        def close(self, *_):
            self.closed = True

    class FakeContext:
        def __init__(self, again_sequence):
            self.sock = FakeSocket(again_sequence)
        def socket(self, *_):
            return self.sock
        def term(self):
            pass

    # Case 1: drop_on_backpressure=True → item dropped
    ctx1 = FakeContext([True])  # first send raises Again
    monkeypatch.setattr(broker_mod, "zmq", zmq)
    monkeypatch.setattr(broker_mod.zmq, "Context", lambda *a, **k: ctx1)
    b1 = broker_mod.ZmqSendBroker(endpoint="tcp://x:1", drop_on_backpressure=True, retry_ms=5)
    b1.start()
    b1.submit(b"x")
    time.sleep(0.05)
    # Attempted at least once, and dropped (no successful send)
    assert ctx1.sock.attempts >= 1
    assert ctx1.sock.sent == []
    b1.stop()

    # Case 2: drop_on_backpressure=False → short retry window then drop
    # Simulate Again on first attempt inside main send and also inside retry loop
    ctx2 = FakeContext([True, True])
    monkeypatch.setattr(broker_mod.zmq, "Context", lambda *a, **k: ctx2)
    b2 = broker_mod.ZmqSendBroker(endpoint="tcp://x:2", drop_on_backpressure=False, retry_ms=5)
    b2.start()
    b2.submit(b"y")
    time.sleep(0.05)
    # It should have attempted >= 1 times in total and still not send
    assert ctx2.sock.attempts >= 1
    assert ctx2.sock.sent == []
    b2.stop()

