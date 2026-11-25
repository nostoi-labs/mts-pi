#!/usr/bin/env python3
"""Writing a log line must never hold up the caller, even when the card takes seconds to flush.

Bench of 24 Nov 2026: with the logs kept on the card every line was flushed (fsync) by the program's main loop, and a
microSD now and then takes most of a second: the picture on the dashboard stood still for that long.
Here the flush is made slow on purpose (0.4 s each). A log call must return at once, the lines must reach the file
in order and complete, drain() must wait for them, and the log kept in memory (no flush) must work as before."""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mtspi import session  # noqa: E402


def main():
    real_fsync, flushes = os.fsync, []

    def slow_fsync(fd):
        time.sleep(0.4)
        flushes.append(time.monotonic())
        real_fsync(fd)

    with tempfile.TemporaryDirectory() as d:
        # 1. flush to the card: slow, and not the caller's business
        os.environ["MTSPI_LOG_SYNC"] = "1"; os.environ["MTSPI_LOG_MAX_MB"] = "0"
        path = os.path.join(d, "bridge.jsonl")
        log = session.JsonLog(path, quiet=True)
        os.fsync = slow_fsync
        try:
            worst, t0 = 0.0, time.monotonic()
            for n in range(60):
                t = time.monotonic()
                log("tick", n=n)
                worst = max(worst, time.monotonic() - t)
                time.sleep(0.02)
            took = time.monotonic() - t0
            assert log.drain(5.0), "the lines did not reach the file within 5 s"
        finally:
            os.fsync = real_fsync
        got = [json.loads(line)["n"] for line in open(path)]
        print(f"60 lines in {took:.2f} s, slowest call {worst * 1000:.1f} ms, flushes {len(flushes)}")
        assert worst < 0.05, f"a log call took {worst * 1000:.0f} ms: the caller waited for the card"
        assert got == list(range(60)), f"lines lost or out of order: {got}"
        assert 1 <= len(flushes) < 60, "lines that arrive during a flush must go to the card together"
        # 2. drain() with nothing pending answers at once; lines written after it still arrive
        t = time.monotonic(); assert log.drain(1.0) and time.monotonic() - t < 0.2
        log("after"); assert log.drain(2.0)
        assert json.loads(open(path).read().splitlines()[-1])["event"] == "after"
        # 3. log kept in memory: written by the caller as before, no thread, the size limit still turns the file over
        os.environ["MTSPI_LOG_SYNC"] = "0"; os.environ["MTSPI_LOG_MAX_MB"] = "0.001"
        mem = session.JsonLog(os.path.join(d, "mem.jsonl"), quiet=True)
        for n in range(40):
            mem("tick", n=n, pad="x" * 40)
        assert mem._writer is None and mem.drain(0.1)
        assert os.path.exists(os.path.join(d, "mem.jsonl.1")), "the size limit did not turn the file over"
        # 4. a file that cannot be written never stops the program
        os.environ["MTSPI_LOG_SYNC"] = "1"
        bad = session.JsonLog(os.path.join(d, "bridge2.jsonl"), quiet=True)
        bad("one"); assert bad.drain(2.0)
        bad.f.close()                                  # the card goes away under the writer
        bad("two"); bad("three"); assert bad.drain(2.0)
        assert bad.f is None
        bad("four")                                    # given up: only the journal from here on
    del os.environ["MTSPI_LOG_SYNC"], os.environ["MTSPI_LOG_MAX_MB"]
    print("ok")


if __name__ == "__main__":
    main()
