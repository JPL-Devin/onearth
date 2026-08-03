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
import random
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Optional
import aiohttp

# UAT environment base URL
UAT_BASE_URL = "https://uat.gitc.earthdata.nasa.gov"

# Layer time ranges - will be loaded from layer_time_ranges.json if available
LAYER_TIME_RANGES = {}

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
        self.load_time_ranges()

    def load_time_ranges(self):
        """Load layer time lists from JSON file created by time_ranges_for_stress_test.py"""
        try:
            with open('layer_time_ranges.json', 'r') as f:
                global LAYER_TIME_RANGES
                LAYER_TIME_RANGES = json.load(f)
                total_times = sum(len(layer['times']) for layer in LAYER_TIME_RANGES.values())
                print(f"Loaded {len(LAYER_TIME_RANGES)} layers with {total_times} total unique times")
        except FileNotFoundError:
            print("Warning: layer_time_ranges.json not found. Run time_ranges_for_stress_test.py first.")
        except Exception as e:
            print(f"Warning: Could not load layer_time_ranges.json: {e}")

    def get_layer_times(self, layer_name: str, count: int = None) -> List[str]:
        """
        Get actual time strings for a layer from the complete list in layer_time_ranges.json.
        These are actual times extracted from S3 file names - no inference or ranges.

        Args:
            layer_name: Name of the layer
            count: Number of times to randomly sample (if None, returns all times)

        Returns:
            List of time strings in YYYY-MM-DDTHH:MM:SS format for OnEarth URLs
        """
        if layer_name not in LAYER_TIME_RANGES:
            return []

        all_times = LAYER_TIME_RANGES[layer_name]['times']
        if not all_times:
            return []

        # If count is specified, randomly sample that many times from the complete list
        if count and len(all_times) > count:
            sampled_times = random.sample(all_times, count)
        else:
            sampled_times = all_times

        # Convert each actual time to OnEarth URL format
        time_strings = []
        for t in sampled_times:
            year = t['year']
            month = t['month']
            day = t['day']
            hour = t['hour']
            minute = t['minute']
            second = t['second']

            # Build full timestamp: YYYY-MM-DDTHH:MM:SSZ
            time_strings.append(f"{year}-{month}-{day}T{hour}:{minute}:{second}Z")

        return time_strings

    async def fetch_url(self, session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> tuple:
        """Fetch a URL and return response time, status, error, and url"""
        async with semaphore:
            start_time = time.time()
            try:
                async with session.get(url, ssl=False) as response:
                    await response.read()
                    response_time = (time.time() - start_time) * 1000  # Convert to ms
                    return response_time, response.status, None, url
            except asyncio.TimeoutError:
                response_time = (time.time() - start_time) * 1000
                return response_time, 0, "timeout", url
            except Exception as e:
                response_time = (time.time() - start_time) * 1000
                return response_time, 0, str(type(e).__name__), url

    async def run_test(self, test_name: str, url = None, urls: List[str] = None,
                       num_requests: int = None, timeout: int = None,
                       header_info: str = None) -> TestResult:
        """
        Run a stress test on URL(s).

        Args:
            test_name: Name of the test
            url: Single URL to test (requires num_requests)
            urls: List of URLs to test (mutually exclusive with url)
            num_requests: Number of requests (only used with url parameter)
            timeout: Timeout in seconds
            header_info: Additional info to print in test header
        """
        if timeout is None:
            timeout = self.config.timeout

        # Generate URL list
        if urls is not None:
            url_list = urls
            num_requests = len(urls)
        elif url is not None:
            if num_requests is None:
                num_requests = self.config.num_requests
            url_list = [url] * num_requests
        else:
            raise ValueError("Must provide either 'url' or 'urls' parameter")

        print(f"\n{'='*60}")
        print(f"Running test: {test_name}")
        if header_info:
            print(header_info)
        elif url is not None:
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
            tasks = [self.fetch_url(session, url, semaphore) for url in url_list]

            # Progress indicator
            completed = 0
            for coro in asyncio.as_completed(tasks):
                response_time, status, error, returned_url = await coro
                response_times.append(response_time)
                statuses.append(status)
                if error:
                    errors[error] += 1
                    print(f"  Error: {error} (status: {status}) - URL: {returned_url}")
                elif status >= 400:
                    print(f"  HTTP Error: {status} - URL: {returned_url}")

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

    async def run_test_with_varying_times(self, test_name: str, base_url: str, layer_name: str,
                                            tile_matrix_set: str, num_requests: int = None, timeout: int = None) -> TestResult:
        """
        Run a stress test using actual times from S3 for this layer.
        Each request will use an actual timestamp that exists in the S3 data.

        Args:
            test_name: Name of the test
            base_url: Base portion of URL before the date
            layer_name: Layer name to get times for
            tile_matrix_set: Tile Matrix Set (e.g., '1km', '2km')
            num_requests: Number of requests to make
            timeout: Timeout in seconds
        """
        if num_requests is None:
            num_requests = self.config.num_requests

        # Get actual times from S3 for this layer (randomly sampled if more requests than times)
        time_strings = self.get_layer_times(layer_name, count=num_requests)

        if not time_strings:
            print(f"Warning: No time data available for {layer_name}. Skipping test.")
            return None

        # Create URLs with different times
        urls = []
        for i in range(num_requests):
            # Cycle through available times
            time_str = time_strings[i % len(time_strings)]
            url = f"{base_url}/{time_str}/{tile_matrix_set}/0/0/0.png"
            urls.append(url)

        header_info = (f"Layer: {layer_name}\n"
                      f"Using {len(time_strings)} actual times from S3\n"
                      f"Time range: {min(time_strings)} to {max(time_strings)}")

        return await self.run_test(test_name, urls=urls, timeout=timeout, header_info=header_info)

    async def run_test_with_varying_tiles(self, test_name: str, base_url: str, layer_name: str,
                                           tile_matrix_set: str, zoom_level: int = 0, num_requests: int = None,
                                           timeout: int = None) -> TestResult:
        """
        Run a stress test using actual times from S3 with varying tile coordinates.
        Tests different tiles at the specified zoom level.

        Args:
            test_name: Name of the test
            base_url: Base portion of URL before the date
            layer_name: Layer name to get times for
            tile_matrix_set: Tile Matrix Set (e.g., '1km', '2km')
            zoom_level: Zoom level to test (default: 0)
            num_requests: Number of requests to make
            timeout: Timeout in seconds
        """
        if num_requests is None:
            num_requests = self.config.num_requests

        # Get actual times from S3 for this layer
        time_strings = self.get_layer_times(layer_name, count=num_requests)

        if not time_strings:
            print(f"Warning: No time data available for {layer_name}. Skipping test.")
            return None

        # Define valid tile ranges for the zoom level (conservative for global layers)
        # Format: (zoom, row, col) which maps to (z, y, x) in the URL
        if zoom_level == 0:
            tile_coords = [(0, 0, 0), (0, 0, 1)]  # Two tiles at zoom 0 for EPSG:4326
        elif zoom_level == 1:
            tile_coords = [(1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1)]  # Four tiles at zoom 1
        elif zoom_level == 2:
            tile_coords = [(2, 0, 0), (2, 0, 1), (2, 0, 2), (2, 0, 3),
                          (2, 1, 0), (2, 1, 1), (2, 1, 2), (2, 1, 3)]
        else:
            tile_coords = [(zoom_level, 0, 0)]  # Default to single tile for higher zooms

        # Create URLs with different times and tile coordinates
        # URL format: {base_url}/{time}/{tile_matrix_set}/{zoom}/{row}/{col}.png
        urls = []
        for i in range(num_requests):
            # Cycle through available times and tiles
            time_str = time_strings[i % len(time_strings)]
            z, row, col = tile_coords[i % len(tile_coords)]
            url = f"{base_url}/{time_str}/{tile_matrix_set}/{z}/{row}/{col}.png"
            urls.append(url)

        header_info = (f"Layer: {layer_name}\n"
                      f"Zoom level: {zoom_level}, Testing {len(tile_coords)} different tiles\n"
                      f"Using {len(time_strings)} actual times from S3\n"
                      f"Time range: {min(time_strings)} to {max(time_strings)}")

        return await self.run_test(test_name, urls=urls, timeout=timeout, header_info=header_info)

    async def run_test_with_varying_describe_domains(self, test_name: str, layer_name: str,
                                                       tilematrixset: str = "1km", projection: str = "epsg4326",
                                                       quality: str = "std", num_requests: int = None,
                                                       timeout: int = None) -> TestResult:
        """
        Run a DescribeDomains stress test using actual times from S3 for this layer.
        Each request will use different start and end times from the available data.

        Args:
            test_name: Name of the test
            layer_name: Layer name to get times for
            tilematrixset: Tile matrix set (default: "1km")
            projection: Projection (default: "epsg4326")
            quality: Quality level (default: "std")
            num_requests: Number of requests to make
            timeout: Timeout in seconds
        """
        if num_requests is None:
            num_requests = self.config.num_requests

        # Get actual times from S3 for this layer
        time_strings = self.get_layer_times(layer_name)

        if not time_strings or len(time_strings) < 2:
            print(f"Warning: Not enough time data available for {layer_name}. Skipping test.")
            return None

        # Create DescribeDomains URLs with different time ranges
        # Format: /wmts/{projection}/{quality}/1.0.0/{layer}/default/{tilematrixset}/all/{start}--{end}.xml
        urls = []
        sorted_times = sorted(time_strings)

        for i in range(num_requests):
            # Pick two random times from the list, ensuring start < end
            indices = random.sample(range(len(sorted_times)), 2)
            start_idx, end_idx = sorted(indices)
            start_time_str = sorted_times[start_idx]
            end_time_str = sorted_times[end_idx]

            url = f"{UAT_BASE_URL}/wmts/{projection}/{quality}/1.0.0/{layer_name}/default/{tilematrixset}/all/{start_time_str}--{end_time_str}.xml"
            urls.append(url)

        header_info = (f"Layer: {layer_name}\n"
                      f"Using {len(time_strings)} actual times from S3 to generate time ranges\n"
                      f"Time extent: {min(time_strings)} to {max(time_strings)}")

        return await self.run_test(test_name, urls=urls, timeout=timeout, header_info=header_info)

    async def run_test_with_varying_wms(self, test_name: str, base_wms_url: str, layer_name: str,
                                         bbox: str = "-180,-90,180,90", width: int = 512, height: int = 512,
                                         num_requests: int = None, timeout: int = None) -> TestResult:
        """
        Run a WMS GetMap stress test using actual times from S3 for this layer.
        Each request will use an actual timestamp that exists in the S3 data.

        Args:
            test_name: Name of the test
            base_wms_url: Base WMS URL (e.g., .../wms.cgi)
            layer_name: Layer name to get times for
            bbox: Bounding box (default: "-180,-90,180,90")
            width: Image width (default: 512)
            height: Image height (default: 512)
            num_requests: Number of requests to make
            timeout: Timeout in seconds
        """
        if num_requests is None:
            num_requests = self.config.num_requests

        # Get actual times from S3 for this layer
        time_strings = self.get_layer_times(layer_name, count=num_requests)

        if not time_strings:
            print(f"Warning: No time data available for {layer_name}. Skipping test.")
            return None

        # Create WMS URLs with different times
        # WMS format: {base_url}?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS={layer}&STYLES=&FORMAT=image/png&TRANSPARENT=true&HEIGHT={height}&WIDTH={width}&CRS=EPSG:4326&BBOX={bbox}&TIME={time}
        urls = []
        for i in range(num_requests):
            # Cycle through available times
            time_str = time_strings[i % len(time_strings)]
            url = f"{base_wms_url}?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS={layer_name}&STYLES=&FORMAT=image/png&TRANSPARENT=true&HEIGHT={height}&WIDTH={width}&CRS=EPSG:4326&BBOX={bbox}&TIME={time_str}"
            urls.append(url)

        header_info = (f"Layer: {layer_name}\n"
                      f"Using {len(time_strings)} actual times from S3\n"
                      f"Time range: {min(time_strings)} to {max(time_strings)}")

        return await self.run_test(test_name, urls=urls, timeout=timeout, header_info=header_info)

    async def run_all_tests(self):
        """Run all stress tests"""
        tile_requests = self.config.num_requests * 3  # More requests for tile tests

        # ===== GetCapabilities Tests =====
        print("\n" + "="*60)
        print("GETCAPABILITIES TESTS")
        print("="*60)

        # GetCapabilities responses are large and can take time, especially under load
        await self.run_test(
            "WMTS GetCapabilities (EPSG:4326 Best)",
            f"{UAT_BASE_URL}/wmts/epsg4326/best/wmts.cgi?SERVICE=WMTS&request=GetCapabilities",
            timeout=60  # Increase timeout for large GetCapabilities response
        )

        await self.run_test(
            "WMTS GetCapabilities (EPSG:3857 All)",
            f"{UAT_BASE_URL}/wmts/epsg3857/all/wmts.cgi?SERVICE=WMTS&request=GetCapabilities",
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

        # DescribeDomains with varying time ranges
        await self.run_test_with_varying_describe_domains(
            "DescribeDomains - TEMPO_L2 Ozone (varying time ranges)",
            "TEMPO_L2_Ozone_Cloud_Fraction_Granule_v3_STD",
            tilematrixset="1km",
            projection="epsg4326",
            quality="std"
        )

        await self.run_test_with_varying_describe_domains(
            "DescribeDomains - MODIS AOD (varying time ranges)",
            "MODIS_Combined_Value_Added_AOD",
            tilematrixset="2km",
            projection="epsg4326",
            quality="best"
        )

        # DescribeDomains for all periods 
        await self.run_test(
            "DescribeDomains - Orbit Tracks Aqua Ascending all periods",
            f"{UAT_BASE_URL}/wmts/epsg4326/all/1.0.0/OrbitTracks_Aqua_Ascending/default/2km/all/all.xml"
        )

        # ===== Direct Tile Tests with Varying Times =====
        print("\n" + "="*60)
        print("DIRECT TILE TESTS WITH VARYING TIMES")
        print("="*60)

        await self.run_test_with_varying_times(
            "Direct Tile - TEMPO_L2_Ozone (varying times)",
            f"{UAT_BASE_URL}/wmts/epsg4326/std/TEMPO_L2_Ozone_Cloud_Fraction_Granule_v3_STD/default",
            "TEMPO_L2_Ozone_Cloud_Fraction_Granule_v3_STD",
            "1km",
            num_requests=tile_requests
        )

        await self.run_test_with_varying_times(
            "Direct Tile - SMAP (varying times)",
            f"{UAT_BASE_URL}/wmts/epsg4326/std/SMAP_L4_Analyzed_Root_Zone_Soil_Moisture_v7_STD/default",
            "SMAP_L4_Analyzed_Root_Zone_Soil_Moisture_v7_STD",
            "2km",
            num_requests=tile_requests
        )

        await self.run_test_with_varying_times(
            "Direct Tile - MODIS AOD (varying times)",
            f"{UAT_BASE_URL}/wmts/epsg4326/best/MODIS_Combined_Value_Added_AOD/default",
            "MODIS_Combined_Value_Added_AOD",
            "2km",
            num_requests=tile_requests
        )

        # ===== Direct Tile Tests with Varying Tiles =====
        print("\n" + "="*60)
        print("DIRECT TILE TESTS WITH VARYING TILES AND TIMES")
        print("="*60)

        await self.run_test_with_varying_tiles(
            "Direct Tile - MODIS AOD (varying tiles, zoom 0)",
            f"{UAT_BASE_URL}/wmts/epsg4326/best/MODIS_Combined_Value_Added_AOD/default",
            "MODIS_Combined_Value_Added_AOD",
            "2km",
            zoom_level=0,
            num_requests=tile_requests
        )

        await self.run_test_with_varying_tiles(
            "Direct Tile - MODIS AOD (varying tiles, zoom 1)",
            f"{UAT_BASE_URL}/wmts/epsg4326/best/MODIS_Combined_Value_Added_AOD/default",
            "MODIS_Combined_Value_Added_AOD",
            "2km",
            zoom_level=1,
            num_requests=tile_requests
        )

        # ===== EPSG:3857 (Web Mercator) Tests =====
        print("\n" + "="*60)
        print("EPSG:3857 (WEB MERCATOR) TESTS")
        print("="*60)

        await self.run_test_with_varying_times(
            "Direct Tile - MODIS AOD EPSG:3857 (varying times)",
            f"{UAT_BASE_URL}/wmts/epsg3857/best/MODIS_Combined_Value_Added_AOD/default",
            "MODIS_Combined_Value_Added_AOD",
            "GoogleMapsCompatible_Level6",
            num_requests=tile_requests
        )

        await self.run_test_with_varying_tiles(
            "Direct Tile - MODIS AOD EPSG:3857 (varying tiles, zoom 1)",
            f"{UAT_BASE_URL}/wmts/epsg3857/best/MODIS_Combined_Value_Added_AOD/default",
            "MODIS_Combined_Value_Added_AOD",
            "GoogleMapsCompatible_Level6",
            zoom_level=1,
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

        await self.run_test_with_varying_wms(
            "WMS GetMap - MODIS AOD (varying times)",
            f"{UAT_BASE_URL}/wms/epsg4326/best/wms.cgi",
            "MODIS_Combined_Value_Added_AOD",
            bbox="-180,-90,180,90",
            width=512,
            height=512,
            num_requests=tile_requests
        )

        # ===== Vector Layer Tests =====
        print("\n" + "="*60)
        print("VECTOR LAYER TESTS")
        print("="*60)

        await self.run_test_with_varying_wms(
            "Vector Layer - Thermal Anomalies (varying times)",
            f"{UAT_BASE_URL}/wms/epsg4326/nrt/wms.cgi",
            "VIIRS_NOAA20_Thermal_Anomalies_375m_All_v2_NRT",
            bbox="-180,-90,180,90",
            width=512,
            height=512,
            num_requests=tile_requests
        )

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
        default=1000,
        help="Number of requests per test (default: 1000)"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Number of concurrent connections (default: 50)"
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
