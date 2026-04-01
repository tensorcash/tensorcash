class StreamingProver:
    def __init__(self, challenge: bytes, disc_size: int, checkpoint_size: int, *_):
        self._iterations = 0
        self._challenge = challenge
        self._running = False

    def set_verbose(self, flag: bool):
        pass

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def reset(self, challenge: bytes):
        self._challenge = challenge
        self._iterations = 0

    def get_last_available_proof(self):
        # Simulate increasing iterations and a dummy proof blob
        self._iterations += 1
        return b"mock_proof", self._iterations

