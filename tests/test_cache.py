import threading
import time

from app.services.cache import TTLCache


def test_cache_expires_values():
    cache = TTLCache(default_ttl=0.02)
    cache.set("key", "value")
    assert cache.get("key") == "value"
    time.sleep(0.03)
    assert cache.get("key") is None


def test_cache_concurrent_reads_and_writes_are_isolated():
    cache = TTLCache(default_ttl=2, max_size=1000)
    errors = []
    def worker(index):
        try:
            for n in range(100):
                key=(index,n)
                cache.set(key,index)
                assert cache.get(key)==index
        except Exception as exc:
            errors.append(exc)
    threads=[threading.Thread(target=worker,args=(i,)) for i in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    assert cache.get((0,50)) == 0
    assert cache.get((1,50)) == 1
