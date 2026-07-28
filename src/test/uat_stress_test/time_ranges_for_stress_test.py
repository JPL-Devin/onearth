#!/usr/bin/env python3
"""
Extract time ranges for specific layers in the S3 bucket gitc-uat-mrf-archive/epsg4326
"""

import boto3
import re
import json
from collections import defaultdict
from datetime import datetime
from botocore.config import Config


def extract_time_from_path(key):
    """
    Extract time information from S3 key path.
    Expected format: LAYERNAME-YYYYDDDHHMMSS.extension
    Example: TEMPO_L2_Ozone_Cloud_Fraction_Granule_v3_STD-2025144002301.ppg
    Where YYYY=year (2025), DDD=day of year (144), HHMMSS=time (00:23:01)

    Args:
        key: S3 object key

    Returns:
        Tuple of (year, day_of_year, hour, minute, second) or None if pattern doesn't match
    """
    # Pattern: YYYYDDDHHMMSS (e.g., 2025144002301)
    # Year: 4 digits, DOY: 3 digits, Time: 6 digits (HHMMSS)
    pattern = r'-(\d{4})(\d{3})(\d{6})\.'
    match = re.search(pattern, key)

    if match:
        year = match.group(1)
        doy = match.group(2)
        time_str = match.group(3)

        # Parse HHMMSS into hour, minute, second
        hour = time_str[0:2]
        minute = time_str[2:4]
        second = time_str[4:6]

        return (year, doy, hour, minute, second)

    return None


def get_layer_time_range(bucket_name, layer_prefix):
    """
    Collect time range information for a single layer.

    Args:
        bucket_name: Name of the S3 bucket
        layer_prefix: Full prefix to the layer (e.g., 'epsg4326/TEMPO_L2_...')

    Returns:
        Dictionary with times and sample keys
    """
    # Configure boto3 with higher max_pool_connections for better performance
    config = Config(max_pool_connections=50)
    s3_client = boto3.client('s3', config=config)

    times = set()
    sample_keys = []
    continuation_token = None
    total_objects = 0

    print(f"Scanning: {bucket_name}/{layer_prefix}")

    # Normalize prefix to ensure it ends with /
    if layer_prefix and not layer_prefix.endswith('/'):
        layer_prefix = layer_prefix + '/'

    while True:
        # List objects with pagination
        list_kwargs = {
            'Bucket': bucket_name,
            'Prefix': layer_prefix,
            'MaxKeys': 1000
        }

        if continuation_token:
            list_kwargs['ContinuationToken'] = continuation_token

        try:
            response = s3_client.list_objects_v2(**list_kwargs)
        except Exception as e:
            print(f"  Error accessing S3: {e}")
            return None

        # Process objects in this batch
        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                total_objects += 1

                # Extract time information
                time_info = extract_time_from_path(key)
                if time_info:
                    year, doy, hour, minute, second = time_info
                    times.add((year, doy, hour, minute, second))
                    # Keep a few sample keys
                    if len(sample_keys) < 10:
                        sample_keys.append(key)

        # Check if there are more objects to fetch
        if response.get('IsTruncated'):
            continuation_token = response.get('NextContinuationToken')
        else:
            break

    # Convert to sorted list for JSON serialization
    sorted_times = sorted(list(times))

    print(f"  Total objects: {total_objects:,}, Unique times: {len(sorted_times)}\n")

    return {
        'times': sorted_times,
        'sample_keys': sample_keys,
        'total_files': total_objects
    }


def main():
    bucket_name = 'gitc-uat-mrf-archive'
    base_prefix = 'epsg4326'

    # Specific layers to extract time ranges for
    target_layers = [
        'TEMPO_L2_Ozone_Cloud_Fraction_Granule_v3_STD',
        'SMAP_L4_Analyzed_Root_Zone_Soil_Moisture_v7_STD',
        'MODIS_Combined_Value_Added_AOD'
    ]

    print("=" * 80)
    print("EXTRACTING TIME RANGES FROM S3")
    print("=" * 80)
    print()

    time_ranges = {}

    # Get time ranges for each layer separately
    for layer_name in target_layers:
        layer_prefix = f"{base_prefix}/{layer_name}"
        result = get_layer_time_range(bucket_name, layer_prefix)

        if result:
            time_ranges[layer_name] = result

    if not time_ranges:
        print(f"\nNo time data found for target layers")
        return

    # Save complete list of times to JSON file for use by stress test
    # Convert day-of-year to month/day for easier use
    time_ranges_output = {}
    for layer_name in target_layers:
        if layer_name in time_ranges:
            # Store all times as a list - each entry is an actual time from S3
            # Convert day of year to month and day
            all_times = []
            for t in time_ranges[layer_name]['times']:
                year = t[0]
                doy = int(t[1])
                hour = t[2]
                minute = t[3]
                second = t[4]

                # Convert day of year to month and day
                date_obj = datetime.strptime(f"{year}-{doy}", "%Y-%j")
                month = date_obj.strftime("%m")
                day = date_obj.strftime("%d")

                all_times.append({
                    'year': year,
                    'month': month,
                    'day': day,
                    'hour': hour,
                    'minute': minute,
                    'second': second
                })

            time_ranges_output[layer_name] = {
                'times': all_times,  # Complete list of all unique times found
                'count': len(all_times),
                'total_files': time_ranges[layer_name]['total_files']
            }

    if time_ranges_output:
        output_json = 'layer_time_ranges.json'
        with open(output_json, 'w') as f:
            json.dump(time_ranges_output, f, indent=2)
        print(f"\nComplete list of {sum(len(v['times']) for v in time_ranges_output.values())} unique times saved to: {output_json}")


if __name__ == '__main__':
    main()
