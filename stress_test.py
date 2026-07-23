#!/usr/bin/env python3
"""
OnEarth Stress Test Script

Tests major OnEarth capabilities under load and provides performance metrics
for comparison between runs.
"""

import argparse
import asyncio
import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Optional
import aiohttp

# UAT environment base URL
UAT_BASE_URL = "https://uat.gitc.earthdata.nasa.gov"

@dataclass
class TestResult:
    """Results from a single test"""
    name: str
    total_requests: int
    successful: int
    failed: int
    duration_seconds: float
    requests_per_second: float
    avg_response_time_ms: float
    min_response_time_ms: float
    max_response_time_ms: float
    p50_response_time_ms: float
    p95_response_time_ms: float
    p99_response_time_ms: float
    error_types: Dict[str, int]


@dataclass
class StressTestConfig:
    """Configuration for stress tests"""
    base_url: str
    num_requests: int
    concurrency: int
    timeout: int


class OnEarthStressTester:
    """Stress tester for OnEarth services"""

    def __init__(self, config: StressTestConfig):
        self.config = config
        self.results: List[TestResult] = []

    async def fetch_url(self, session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> tuple:
        """Fetch a URL and return response time and status"""
        async with semaphore:
            start_time = time.time()
            try:
                async with session.get(url, ssl=False) as response:
                    await response.read()
                    response_time = (time.time() - start_time) * 1000  # Convert to ms
                    return response_time, response.status, None
            except asyncio.TimeoutError:
                response_time = (time.time() - start_time) * 1000
                return response_time, 0, "timeout"
            except Exception as e:
                response_time = (time.time() - start_time) * 1000
                return response_time, 0, str(type(e).__name__)

    async def run_test(self, test_name: str, url: str, num_requests: int = None, timeout: int = None) -> TestResult:
        """Run a stress test on a specific URL"""
        if num_requests is None:
            num_requests = self.config.num_requests
        if timeout is None:
            timeout = self.config.timeout

        print(f"\n{'='*60}")
        print(f"Running test: {test_name}")
        print(f"URL: {url}")
        print(f"Requests: {num_requests}, Concurrency: {self.config.concurrency}, Timeout: {timeout}s")
        print(f"{'='*60}")

        semaphore = asyncio.Semaphore(self.config.concurrency)
        response_times = []
        statuses = []
        errors = defaultdict(int)

        start_time = time.time()

        # Create a new session with the specified timeout
        connector = aiohttp.TCPConnector(limit=self.config.concurrency * 2)
        client_timeout = aiohttp.ClientTimeout(total=timeout)

        async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as session:
            tasks = [self.fetch_url(session, url, semaphore) for _ in range(num_requests)]

            # Progress indicator
            completed = 0
            for coro in asyncio.as_completed(tasks):
                response_time, status, error = await coro
                response_times.append(response_time)
                statuses.append(status)
                if error:
                    errors[error] += 1

                completed += 1
                if completed % max(1, num_requests // 10) == 0:
                    print(f"Progress: {completed}/{num_requests} ({completed*100//num_requests}%)")

        duration = time.time() - start_time

        # Calculate statistics
        successful = sum(1 for s in statuses if 200 <= s < 300)
        failed = num_requests - successful

        result = TestResult(
            name=test_name,
            total_requests=num_requests,
            successful=successful,
            failed=failed,
            duration_seconds=duration,
            requests_per_second=num_requests / duration if duration > 0 else 0,
            avg_response_time_ms=statistics.mean(response_times),
            min_response_time_ms=min(response_times),
            max_response_time_ms=max(response_times),
            p50_response_time_ms=statistics.median(response_times),
            p95_response_time_ms=self._percentile(response_times, 95),
            p99_response_time_ms=self._percentile(response_times, 99),
            error_types=dict(errors)
        )

        self.results.append(result)
        self._print_result(result)
        return result

    @staticmethod
    def _percentile(data: List[float], percentile: float) -> float:
        """Calculate percentile of data"""
        sorted_data = sorted(data)
        index = int((percentile / 100) * len(sorted_data))
        return sorted_data[min(index, len(sorted_data) - 1)]

    def _print_result(self, result: TestResult):
        """Print test result"""
        print(f"\n{'-'*60}")
        print(f"Test Results: {result.name}")
        print(f"{'-'*60}")
        print(f"Total Requests:    {result.total_requests}")
        print(f"Successful:        {result.successful} ({result.successful*100/result.total_requests:.1f}%)")
        print(f"Failed:            {result.failed} ({result.failed*100/result.total_requests:.1f}%)")
        print(f"Duration:          {result.duration_seconds:.2f}s")
        print(f"Requests/sec:      {result.requests_per_second:.2f}")
        print(f"\nResponse Times (ms):")
        print(f"  Average:         {result.avg_response_time_ms:.2f}")
        print(f"  Min:             {result.min_response_time_ms:.2f}")
        print(f"  Max:             {result.max_response_time_ms:.2f}")
        print(f"  P50 (median):    {result.p50_response_time_ms:.2f}")
        print(f"  P95:             {result.p95_response_time_ms:.2f}")
        print(f"  P99:             {result.p99_response_time_ms:.2f}")

        if result.error_types:
            print(f"\nErrors:")
            for error_type, count in result.error_types.items():
                print(f"  {error_type}: {count}")

    async def run_all_tests(self):
        """Run all stress tests"""
        tile_requests = self.config.num_requests * 3  # More requests for tile tests

        # ===== GetCapabilities Tests =====
        print("\n" + "="*60)
        print("GETCAPABILITIES TESTS")
        print("="*60)

        # GetCapabilities responses are large and can take time, especially under load
        await self.run_test(
            "WMTS GetCapabilities (EPSG:4326 All)",
            f"{UAT_BASE_URL}/wmts/epsg4326/best/wmts.cgi?SERVICE=WMTS&request=GetCapabilities",
            timeout=60  # Increase timeout for large GetCapabilities response
        )

        await self.run_test(
            "WMS GetCapabilities (EPSG:4326 Best)",
            f"{UAT_BASE_URL}/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0",
            timeout=60
        )

        await self.run_test(
            "WMS GetCapabilities (EPSG:4326 STD)",
            f"{UAT_BASE_URL}/wms/epsg4326/std/wms.cgi?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0",
            timeout=60
        )

        # ===== DescribeDomains Tests (Time/Spatial Queries) =====
        print("\n" + "="*60)
        print("DESCRIBEDOMAINS TESTS")
        print("="*60)

        # DescribeDomains with time range
        await self.run_test(
            "DescribeDomains - TEMPO with time range",
            f"{UAT_BASE_URL}/wmts/epsg4326/std/1.0.0/TEMPO_L2_Ozone_Cloud_Fraction_Granule_v3_STD/default/1km/all/2024-01-01T00:00:00Z--2024-12-03T00:00:00Z.xml"
        )

        # DescribeDomains for all periods
        await self.run_test(
            "DescribeDomains - TEMPO_L3 all periods",
            f"{UAT_BASE_URL}/wmts/epsg4326/best/1.0.0/TEMPO_L3_NO2_Vertical_Column_Stratosphere/default/1km/7/all.xml"
        )

        # DescribeDomains for another layer
        await self.run_test(
            "DescribeDomains - MODIS Aqua AOD all periods",
            f"{UAT_BASE_URL}/wmts/epsg4326/all/1.0.0/MODIS_Aqua_Aerosol_Optical_Depth_3k/default/2km/all/all.xml"
        )

        # ===== Direct Tile Tests  =====
        print("\n" + "="*60)
        print("DIRECT TILE TESTS (OE-STATUS)")
        print("="*60)

        await self.run_test(
            "Direct Tile - Raster_Status JPEG (Level 0)",
            f"{UAT_BASE_URL}/oe-status/Raster_Status/default/2004-08-01/16km/0/0/0.jpeg",
            num_requests=tile_requests
        )

        await self.run_test(
            "Direct Tile - Raster_Status JPEG (Level 1)",
            f"{UAT_BASE_URL}/oe-status/Raster_Status/default/2004-08-01/16km/1/0/0.jpeg",
            num_requests=tile_requests
        )

        # ===== WMS GetMap Tests =====
        print("\n" + "="*60)
        print("WMS GETMAP TESTS")
        print("="*60)

        await self.run_test(
            "WMS GetMap - BlueMarble PNG",
            f"{UAT_BASE_URL}/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=BlueMarble_NextGeneration&STYLES=&FORMAT=image/png&TRANSPARENT=true&HEIGHT=256&WIDTH=256&CRS=EPSG:4326&BBOX=-22.5,0,0,22.5",
            num_requests=tile_requests
        )

        await self.run_test(
            "WMS GetMap - BlueMarble PNG (different bbox)",
            f"{UAT_BASE_URL}/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=BlueMarble_NextGeneration&STYLES=&FORMAT=image/png&TRANSPARENT=true&HEIGHT=256&WIDTH=256&CRS=EPSG:4326&BBOX=-45,-45,45,45",
            num_requests=tile_requests
        )

        # ===== Multi-Projection Tests =====
        # TODO: Enable when we identify correct layers and tilematrixsets for each projection
        print("\n" + "="*60)
        print("MULTI-PROJECTION TESTS - TODO")
        print("Need to verify available layers per projection")
        print("="*60)

    def print_summary(self):
        """Print summary of all tests"""
        print(f"\n{'='*60}")
        print("STRESS TEST SUMMARY")
        print(f"{'='*60}")
        print(f"Test Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Base URL: {self.config.base_url}")
        print(f"Total Tests: {len(self.results)}")
        print(f"\n{'Test Name':<35} {'RPS':>8} {'Avg(ms)':>10} {'P95(ms)':>10} {'Success%':>10}")
        print(f"{'-'*75}")

        for result in self.results:
            success_rate = result.successful * 100 / result.total_requests
            print(f"{result.name:<35} {result.requests_per_second:>8.2f} {result.avg_response_time_ms:>10.2f} {result.p95_response_time_ms:>10.2f} {success_rate:>9.1f}%")

    def save_results(self, filename: Optional[str] = None):
        """Save results to JSON file"""
        if filename is None:
            filename = f"stress_test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        output = {
            "timestamp": datetime.now().isoformat(),
            "config": {
                "base_url": self.config.base_url,
                "num_requests": self.config.num_requests,
                "concurrency": self.config.concurrency,
                "timeout": self.config.timeout
            },
            "results": [asdict(r) for r in self.results]
        }

        with open(filename, 'w') as f:
            json.dump(output, f, indent=2)

        print(f"\nResults saved to: {filename}")


async def main():
    parser = argparse.ArgumentParser(
        description="OnEarth Stress Test - Test major capabilities under load"
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=100,
        help="Number of requests per test (default: 100)"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Number of concurrent connections (default: 10)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Request timeout in seconds (default: 30)"
    )
    parser.add_argument(
        "--output",
        help="Output filename for JSON results (default: auto-generated)"
    )

    args = parser.parse_args()

    config = StressTestConfig(
        base_url=UAT_BASE_URL,
        num_requests=args.requests,
        concurrency=args.concurrency,
        timeout=args.timeout
    )

    print(f"\n{'='*60}")
    print("OnEarth Stress Test")
    print(f"{'='*60}")
    print(f"Base URL:      {config.base_url}")
    print(f"Requests:      {config.num_requests} per test")
    print(f"Concurrency:   {config.concurrency}")
    print(f"Timeout:       {config.timeout}s")
    print(f"{'='*60}")

    tester = OnEarthStressTester(config)

    try:
        await tester.run_all_tests()
        tester.print_summary()
        tester.save_results(args.output)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        if tester.results:
            tester.print_summary()
            tester.save_results(args.output)


if __name__ == "__main__":
    asyncio.run(main())
