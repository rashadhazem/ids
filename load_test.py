"""
load_test.py – Concurrency & Stress Testing for BUA Student ID Portal
Simulates high concurrent student traffic to verify speed, stability, and zero-drop behavior.
"""
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_URL = "http://127.0.0.1:5000"

ENDPOINTS = [
    ("/", "Home Page"),
    ("/student/login", "Student Login Page"),
    ("/auth/login", "Staff/Admin Login Page"),
]

def test_endpoint(url, session):
    start = time.time()
    try:
        res = session.get(url, timeout=10)
        elapsed = (time.time() - start) * 1000 # ms
        return res.status_code, elapsed
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return 0, elapsed

def run_stress_test(concurrency=50, total_requests=250):
    print("=" * 65)
    print(f"⚡ RUNNING STRESS & CONCURRENCY TEST FOR BUA PORTAL")
    print(f"• Target Server:      {BASE_URL}")
    print(f"• Concurrency Level:  {concurrency} simultaneous students/connections")
    print(f"• Total Requests:     {total_requests} requests")
    print("=" * 65)

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=concurrency, pool_maxsize=concurrency)
    session.mount("http://", adapter)

    urls = [f"{BASE_URL}{ENDPOINTS[i % len(ENDPOINTS)][0]}" for i in range(total_requests)]

    start_total = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(test_endpoint, url, session) for url in urls]
        for f in as_completed(futures):
            results.append(f.result())

    total_duration = time.time() - start_total

    status_codes = [r[0] for r in results]
    latencies = [r[1] for r in results]

    success_count = sum(1 for c in status_codes if c in (200, 302))
    error_count = total_requests - success_count
    rps = total_requests / total_duration if total_duration > 0 else 0

    avg_lat = statistics.mean(latencies)
    median_lat = statistics.median(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    p95_lat = statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20 else max_lat

    print("\n📊 TEST RESULTS SUMMARY:")
    print("-" * 65)
    print(f"  • Total Time Taken:         {total_duration:.2f} seconds")
    print(f"  • Requests Per Second (RPS): {rps:.1f} req/sec")
    print(f"  • Successful Requests:      {success_count} / {total_requests} ({(success_count/total_requests)*100:.1f}%)")
    print(f"  • Failed / Dropped Requests:{error_count}")
    print("-" * 65)
    print(f"⏱️ LATENCY METRICS (Responsiveness):")
    print(f"  • Average Response Time:    {avg_lat:.1f} ms")
    print(f"  • Median Response Time:     {median_lat:.1f} ms")
    print(f"  • Minimum Response Time:    {min_lat:.1f} ms")
    print(f"  • Maximum Response Time:    {max_lat:.1f} ms")
    print(f"  • 95% of Requests Under:    {p95_lat:.1f} ms")
    print("=" * 65)

    if error_count == 0:
        print("✅ STRESS TEST PASSED: 100% Success rate under heavy concurrent load!")
    else:
        print(f"⚠️ WARNING: {error_count} requests failed.")

if __name__ == "__main__":
    c = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 250
    run_stress_test(c, n)
