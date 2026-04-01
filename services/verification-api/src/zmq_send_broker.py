# SPDX-License-Identifier: Apache-2.0
import zmq
import threading
import queue
import logging
import time


class ZmqSendBroker:
    """
    Single-threaded network sender:
      - owns the ZMQ context and PUSH socket
      - accepts bytes via thread-safe .submit()
      - performs non-blocking sends with drop/backoff
    """

    def __init__(
        self,
        endpoint: str,
        hwm: int = 1000,
        max_queue: int = 5000,
        drop_on_backpressure: bool = True,
        retry_ms: int = 2,
        io_threads: int = 1,
    ):
        self.endpoint = endpoint
        self.hwm = hwm
        self.q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=max_queue)
        self.drop_on_backpressure = drop_on_backpressure
        self.retry_ms = retry_ms
        self.running = False
        self.thread: threading.Thread | None = None
        self.ctx: zmq.Context | None = None
        self.sock: zmq.Socket | None = None
        self.logger = logging.getLogger(__name__)
        self.io_threads = io_threads

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(
            target=self._run, name="zmq-send-broker", daemon=True
        )
        self.thread.start()

    def stop(self, timeout: float = 2.0):
        self.running = False
        # Try to unblock the worker by enqueueing a sentinel
        try:
            self.q.put_nowait(None)
        except queue.Full:
            # If full, drop one item to make room for the sentinel
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(None)
            except Exception:
                pass
        # Join briefly; thread is a daemon so we won’t block process exit
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)

        # Ensure sockets/contexts are torn down even if the thread did not
        # reach its own teardown yet. It's safe to close from this thread.
        try:
            if self.sock is not None:
                try:
                    self.sock.setsockopt(zmq.LINGER, 0)
                except Exception:
                    pass
                self.sock.close(0)
        except Exception:
            pass
        try:
            if self.ctx is not None:
                self.ctx.term()
        except Exception:
            pass

    def submit(self, payload: bytes) -> bool:
        try:
            self.q.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def _run(self):
        # Create context here (owner thread) to avoid fork/thread inheritance issues
        self.ctx = zmq.Context(io_threads=self.io_threads)
        self.sock = self.ctx.socket(zmq.PUSH)
        # Non-blocking behavior / backpressure safety
        self.sock.setsockopt(zmq.SNDHWM, self.hwm)
        self.sock.setsockopt(zmq.LINGER, 0)
        # Do not set IMMEDIATE here: allow local queuing until peer connects
        # Optional TCP keepalive to detect dead peers faster
        try:
            self.sock.setsockopt(zmq.TCP_KEEPALIVE, 1)
            self.sock.setsockopt(zmq.TCP_KEEPALIVE_IDLE, 30)
            self.sock.setsockopt(zmq.TCP_KEEPALIVE_CNT, 3)
            self.sock.setsockopt(zmq.TCP_KEEPALIVE_INTVL, 10)
        except Exception:
            pass

        self.sock.connect(self.endpoint)
        self.logger.info(f"ZmqSendBroker connected to {self.endpoint}")

        while self.running:
            item = self.q.get()
            if item is None:
                break
            try:
                self.sock.send(item, flags=zmq.DONTWAIT)
            except zmq.Again:
                if self.drop_on_backpressure:
                    self.logger.debug("ZmqSendBroker drop: backpressure (EAGAIN)")
                    continue
                deadline = time.time() + (self.retry_ms / 1000.0)
                sent = False
                while time.time() < deadline:
                    try:
                        self.sock.send(item, flags=zmq.DONTWAIT)
                        sent = True
                        break
                    except zmq.Again:
                        time.sleep(self.retry_ms / 1000.0)
                if not sent:
                    # drop after short grace
                    self.logger.debug("ZmqSendBroker drop: retry window elapsed")
            except Exception as e:
                self.logger.error(f"Send error: {e}")

        # teardown
        try:
            if self.sock is not None:
                self.sock.close(0)
        except Exception:
            pass
        try:
            if self.ctx is not None:
                self.ctx.term()
        except Exception:
            pass
        self.logger.info("ZmqSendBroker stopped")
