"""Bounded classification microbenchmark, separate from browser/host time.

PYTHONPATH=tests uv run python tools/benchmarks/browser_effects.py
"""
import json
import platform
from statistics import median
from time import perf_counter_ns

from _local_package import load_local_package
load_local_package()
from omh.workflows.browser_effect_attempts_contract import classify


def main():
    samples = []
    count = 10000
    for _ in range(count):
        start = perf_counter_ns()
        assert classify('read') == 'read_only'
        samples.append(perf_counter_ns() - start)
    ordered = sorted(samples)
    p95 = ordered[(95 * count + 99) // 100 - 1]
    assert p95 < 100000, p95
    print(json.dumps({'schema_version':'browser_effects_benchmark/v1', 'samples':count,
        'procedure':'individual inert-read classification, perf_counter_ns, nearest-rank p95, assertion included',
        'p50_ns':median(samples), 'p95_ns':p95, 'threshold_ns':100000,
        'host_browser_ns':None, 'host_browser_time_source':'separate real QA callback timings',
        'python':platform.python_version(), 'host':platform.platform(),
        'attempt_store_operations':0, 'browser_roundtrips':0, 'provider_calls':0,
        'remaining_resources':[]}))


if __name__ == '__main__':
    main()
