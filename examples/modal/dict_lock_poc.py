import random
import time
import uuid
from contextlib import contextmanager

import modal

stub = modal.Stub("modal-lock-test-randomized")
lock_dict = modal.Dict.from_name(
    "lock-test", create_if_missing=True
)  # Shared lock storage for locks


class ModalLockProvider:
    """A provider that manages distributed locks using modal.Dict with optimistic concurrency."""

    def __init__(
        self, lock_dict, wait_interval=0.1, acquire_timeout=10, worker_id=None
    ):
        self.lock_dict = lock_dict
        self.wait_interval = wait_interval
        self.acquire_timeout = acquire_timeout
        self.worker_id = worker_id or str(uuid.uuid4())  # Unique ID per worker

    @contextmanager
    def get(self, key: str):
        """Acquire a distributed lock for the given key, ensuring safe modifications."""
        start_time = time.time()

        while time.time() - start_time < self.acquire_timeout:
            existing_lock = self.lock_dict.get(key)

            if existing_lock:  # If key exists, wait and retry
                print(f"Lock '{key}' is already held by {existing_lock}")
                time.sleep(self.wait_interval)
                continue

            print(f"Lock '{key}' is available, attempting to acquire...")
            # Set the lock with this worker's unique ID
            self.lock_dict[key] = {
                "worker_id": self.worker_id,
                "timestamp": time.time(),
            }

            # Re-check that we still hold the lock
            latest_lock = self.lock_dict.get(key)
            if latest_lock and latest_lock["worker_id"] == self.worker_id:
                print(f"Lock '{key}' successfully acquired")
                try:
                    yield  # Lock acquired
                finally:
                    # Release lock only if still owned by this instance
                    print(f"Releasing lock '{key}'...")
                    if self.lock_dict.get(key, {}).get("worker_id") == self.worker_id:
                        del self.lock_dict[key]
                return  # Exit successfully

            # If another worker overwrote our lock, retry
            print(f"Lock '{key}' was overwritten, retrying...")
            time.sleep(self.wait_interval)

        raise TimeoutError(
            f"Failed to acquire lock '{key}' within {self.acquire_timeout} seconds"
        )


# Predefined keys and values
KEYS = ["key1", "key2", "key3"]
VALUES = list(range(10))  # 10 predefined values


@stub.function()
def worker(worker_id: int):
    """Each worker appends 100 values across 3 predefined keys in random order."""
    lock_provider = ModalLockProvider(lock_dict, worker_id=worker_id)

    remaining_values = {key: VALUES[:] for key in KEYS}

    while any(remaining_values.values()):  # While there are values left to add
        key = random.choice(
            [k for k, v in remaining_values.items() if v]
        )  # Pick a non-empty key
        value = remaining_values[key].pop(0)  # Remove from worker's list

        with lock_provider.get(key + ".lock"):  # Ensure safe modification
            existing_list = lock_dict.get(key, [])
            existing_list.append((worker_id, value))  # Track which worker added what
            lock_dict[key] = existing_list  # Save back


@stub.function()
def verify():
    """Verify that all values were added exactly as expected."""
    total_expected = len(VALUES) * 10  # 100 values per worker, 10 workers
    all_correct = True

    for key in KEYS:
        stored_values = lock_dict.get(key, [])
        unique_values = set(v for _, v in stored_values)

        print(f"Key: {key}, Expected: {total_expected}, Found: {len(stored_values)}")
        if len(stored_values) != total_expected or len(unique_values) != len(VALUES):
            all_correct = False

    print("✅ Verification passed!" if all_correct else "❌ Verification failed!")


@stub.local_entrypoint()
def main():
    """Runs 10 workers in parallel and verifies results."""
    # Clear dicts before starting
    lock_dict.clear()
    print({k: v for k, v in lock_dict.items()})
    for key in KEYS:
        lock_dict[key] = []

    # Run 10 parallel workers
    list(worker.map(range(10)))

    # Run verification
    verify.remote()

    print({k: v for k, v in lock_dict.items()})
