#!/usr/bin/env python3

#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Tests for periods.py
#

import os
import sys
import unittest
import xmlrunner
from optparse import OptionParser
from subprocess import Popen
import time
import redis
from dateutil import relativedelta as rd
import shutil

shutil.copyfile("/home/oe2/onearth/src/modules/time_service/utils/oe_redis_utl.py", os.getcwd() + '/oe_redis_utl.py')
shutil.copyfile("/home/oe2/onearth/src/modules/time_service/utils/periods.py", os.getcwd() + '/periods.py')
from periods import get_zadd_dict, get_rd_from_interval, get_duration_from_rd, find_periods_and_breaks, calculate_periods_from_config, calculate_layer_periods
from oe_redis_utl import create_redis_client

def redis_running():
    try:
        r = redis.Redis(host='localhost', port=6379, db=0)
        return r.ping()
    except redis.exceptions.ConnectionError:
        return False

DEBUG = False

class TestPeriods(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        # Start redis
        if not redis_running():
            Popen(['redis-server'])
        time.sleep(2)
        if not redis_running():
            print("WARNING: Can't access Redis server. Tests may fail.")
        self.host = 'localhost'
        self.port = 6379
        self.redis_client = create_redis_client(self.host, self.port, debug=False)
    
    @classmethod
    def tearDown(self):
        # Clean up Redis after each test.
        self.redis_client.flushdb()

    # Test get_zadd_dict
    def test_get_zadd_dict(self):
        periods = ["2023-01-01", "2023-02-01"]
        expected = {"2023-01-01": 0, "2023-02-01": 0}
        self.assertEqual(get_zadd_dict(periods), expected)

    # Test get_rd_from_interval
    def test_get_rd_from_interval(self):
        self.assertEqual(get_rd_from_interval("P1Y"), rd.relativedelta(years=1))
        self.assertEqual(get_rd_from_interval("P2M"), rd.relativedelta(months=2))
        self.assertEqual(get_rd_from_interval("P3D"), rd.relativedelta(days=3))
        self.assertEqual(get_rd_from_interval("PT4H"), rd.relativedelta(hours=4))
        self.assertEqual(get_rd_from_interval("PT5M"), rd.relativedelta(minutes=5))
        self.assertEqual(get_rd_from_interval("PT6S"), rd.relativedelta(seconds=6))
    
    # Test get_duration_from_rd
    def test_get_duration_from_rd(self):
        self.assertEqual(get_duration_from_rd(rd.relativedelta(years=1)), "P1Y")
        self.assertEqual(get_duration_from_rd(rd.relativedelta(months=2)), "P2M")
        self.assertEqual(get_duration_from_rd(rd.relativedelta(days=3)), "P3D")
        self.assertEqual(get_duration_from_rd(rd.relativedelta(hours=4)), "PT4H")
        self.assertEqual(get_duration_from_rd(rd.relativedelta(minutes=5)), "PT5M")
        self.assertEqual(get_duration_from_rd(rd.relativedelta(seconds=6)), "PT6S")
    
    # Test find_periods_and_breaks
    def test_find_periods_and_breaks(self):
        dates = ["2023-01-01", "2023-01-02", "2023-01-04"]
        interval = rd.relativedelta(days=1)
        expected = [
            {'start': '2023-01-01', 'end': '2023-01-02', 'duration': 'P1D'},
            {'start': '2023-01-04', 'end': '2023-01-04', 'duration': 'P1D'}
        ]
        self.assertEqual(find_periods_and_breaks(dates, interval), expected)

    # --- Tests of calculate_periods_from_config ---

    def test_detect_period_from_dates(self):
        # Test period calculation when period is set to DETECT
        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        config = "DETECT"
        expected = ["2024-01-01/2024-01-03/P1D"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_forced_period_with_start_end(self):
        # Test forced start, end, and period values
        dates = ["2023-01-01", "2024-01-05", "2024-01-12"]
        config = "2024-01-01/2024-01-10/P5D"
        expected = ["2024-01-01/2024-01-10/P5D"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_forced_latest_period(self):
        # Test handling of LATEST-based period calculations
        dates = ["2024-01-01", "2024-02-01", "2024-03-01"]
        config = "LATEST-3M"
        expected = ["2023-12-01/2024-03-01/P1M"]  # Expecting 3 months back from last date
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_period_with_subdaily_intervals(self):
        # Test periods with subdaily intervals
        dates = ["2024-01-01T00:00:00", "2024-01-01T06:00:00", "2024-01-01T12:00:00"]
        config = "DETECT"
        expected = ["2024-01-01T00:00:00Z/2024-01-01T12:00:00Z/PT6H"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_empty_dates_list(self):
        # Ensure no periods are returned if no dates are provided
        config = "DETECT"
        self.assertEqual(calculate_periods_from_config([], config, None, None), [])

    def test_single_date_list(self):
        # Ensure a single date returns a single period
        dates = ["2024-01-01"]
        config = "DETECT"
        expected = ["2024-01-01/2024-01-01/P1D"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_irregular_intervals(self):
        # Ensure detection works with irregular intervals
        dates = ["2024-01-01", "2024-01-03", "2024-01-07"]
        config = "DETECT"
        expected = [
            "2024-01-01/2024-01-03/P2D",
            "2024-01-07/2024-01-07/P2D"
        ]
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_forced_start_date_trims_periods(self):
        # Ensure periods are trimmed correctly when a start_date is set
        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        config = "DETECT"
        start_date = "2024-01-02"
        expected = ["2024-01-02/2024-01-03/P1D"]
        self.assertEqual(calculate_periods_from_config(dates, config, start_date, None), expected)

    def test_forced_end_date_trims_periods(self):
        # Ensure periods are trimmed correctly when an end_date is set
        dates = ["2024-01-01", "2024-01-02", "2024-01-03"]
        config = "DETECT"
        end_date = "2024-01-02"
        expected = ["2024-01-01/2024-01-02/P1D"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, end_date), expected)
    
    def test_forced_end_date_take_interval(self):
        # Ensure that when there is just one date that occurs before the end_date,
        # the duration between the date and the end date becomes the period's interval
        dates = ["2024-01-01T01:00:00", "2024-01-03T01:00:00"]
        config = "DETECT"
        end_date = "2024-01-01T01:12:34"
        expected = ["2024-01-01T01:00:00Z/2024-01-01T01:00:00Z/PT12M34S"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, end_date), expected)

    def test_detect_with_minute_intervals(self):
        # Test detection of minute intervals
        dates = ["2024-01-01T00:00:00", "2024-01-01T00:30:00", "2024-01-01T01:00:00"]
        config = "DETECT"
        expected = ["2024-01-01T00:00:00Z/2024-01-01T01:00:00Z/PT30M"]
        self.assertEqual(calculate_periods_from_config(dates, config, None, None), expected)

    def test_find_smallest_interval(self):
        # Test that the smallest interval is detected when find_smallest_interval=True
        dates = [
            "2024-01-01", "2024-01-03", "2024-01-05", "2024-01-06"
        ] 
        config = "DETECT"
        expected_default_interval = ["2024-01-01/2024-01-05/P2D",
                                      "2024-01-06/2024-01-06/P2D"]  # Interval determined based on first two intervals matching
        expected_smallest_interval = ["2024-01-01/2024-01-01/P1D",
                                      "2024-01-03/2024-01-03/P1D",
                                      "2024-01-05/2024-01-06/P1D"]  # Smallest interval is P1D (1 day)
        self.assertEqual(
            calculate_periods_from_config(dates, config, None, None, find_smallest_interval=False),
            expected_default_interval
        )
        self.assertEqual(
            calculate_periods_from_config(dates, config, None, None, find_smallest_interval=True),
            expected_smallest_interval
        )

    def test_irregular_times_PT6M(self):
        # Ensure periods generation works when we have subdaily irregular intervals but a forced period of PT6M
        dates = [
            "2025-01-01T12:54:42",
            "2025-01-01T13:01:22",
            "2025-01-01T13:08:00",
            "2025-01-01T13:28:10",
            "2025-01-01T13:34:50",
            "2025-01-01T13:41:30",
            "2025-01-01T13:48:08",
            "2025-01-01T13:54:45",
            "2025-01-01T14:01:22",
            "2025-01-01T14:08:18",
            "2025-01-01T14:14:58",
            "2025-01-01T14:21:38",
            "2025-01-01T14:28:16",
        ]
        config = "DETECT/DETECT/PT6M"
        expected = [
            "2025-01-01T12:54:42Z/2025-01-01T12:54:42Z/PT6M",
            "2025-01-01T13:01:22Z/2025-01-01T13:01:22Z/PT6M",
            "2025-01-01T13:08:00Z/2025-01-01T13:08:00Z/PT6M",
            "2025-01-01T13:28:10Z/2025-01-01T13:28:10Z/PT6M",
            "2025-01-01T13:34:50Z/2025-01-01T13:34:50Z/PT6M",
            "2025-01-01T13:41:30Z/2025-01-01T13:41:30Z/PT6M",
            "2025-01-01T13:48:08Z/2025-01-01T13:48:08Z/PT6M",
            "2025-01-01T13:54:45Z/2025-01-01T13:54:45Z/PT6M",
            "2025-01-01T14:01:22Z/2025-01-01T14:01:22Z/PT6M",
            "2025-01-01T14:08:18Z/2025-01-01T14:08:18Z/PT6M",
            "2025-01-01T14:14:58Z/2025-01-01T14:14:58Z/PT6M",
            "2025-01-01T14:21:38Z/2025-01-01T14:21:38Z/PT6M",
            "2025-01-01T14:28:16Z/2025-01-01T14:28:16Z/PT6M",
        ]
        periods = calculate_periods_from_config(dates, config, None, None)
        self.assertEqual(periods, expected, "Returned periods {0} does not match expected periods {1}".format(periods, expected))

    # --- Tests for calculate_layer_periods ---

    def test_calculate_layer_periods_multiple_configs(self):
        # Test using multiple time configs.
        layer_key = "test_layer"
        dates = {"2024-03-10T12:00:00": 0,
                 "2024-03-10T13:00:00": 0,
                 "2024-03-10T14:00:00": 0,
                 "2024-03-10T15:00:00": 0,
                 "2024-03-10T19:00:00": 0}
        
        configs = ["DETECT/2024-03-10T14:00:00", "2025-01-01T09:00:00/2025-01-01T10:00:00/PT1H"]

        for config in configs:
            self.redis_client.sadd(layer_key + ":config", config)
        
        expected_periods = [b'2024-03-10T12:00:00Z/2024-03-10T14:00:00Z/PT1H', b'2025-01-01T09:00:00Z/2025-01-01T10:00:00Z/PT1H']
        expected_default = "2025-01-01T10:00:00Z"

        self.redis_client.zadd(layer_key + ":dates", dates)
        calculate_layer_periods(self.redis_client, layer_key)

        periods = self.redis_client.zrange(layer_key + ":periods", 0, -1)
        default = self.redis_client.get(layer_key + ":default").decode('utf-8')
        self.assertEqual(periods, expected_periods)
        self.assertEqual(default, expected_default, "Returned default date {0} does not match expected default date {1}".format(default, expected_default))

    def test_calculate_layer_periods_keep_existing_periods(self):
        # Test basic functionality with simple periods.
        layer_key = "test_layer"
        dates = {"2025-03-10T12:00:00": 0,
                 "2025-03-10T13:00:00": 0,
                 "2025-03-10T14:00:00": 0,
                 "2025-03-10T18:00:00": 0,
                 "2025-03-10T19:00:00": 0}
        
        new_date = "2025-03-10T20:00:00"
        
        expected_periods = [b'2025-03-10T12:00:00Z/2025-03-10T14:00:00Z/PT1H', b'2025-03-10T18:00:00Z/2025-03-10T20:00:00Z/PT1H']

        self.redis_client.zadd(layer_key + ":dates", dates)
        calculate_layer_periods(self.redis_client, layer_key, new_date)

        periods = self.redis_client.zrange(layer_key + ":periods", 0, -1)
        default = self.redis_client.get(layer_key + ":default").decode('utf-8')
        self.assertEqual(periods, expected_periods, "Returned periods {0} does not match expected periods {1}".format(periods, expected_periods))
        self.assertEqual(default, new_date + "Z", "Returned default date {0} does not match expected default date {1}".format(default, new_date + 'Z'))

        # Test calculate_layer_periods with the keep_existing_periods option

        # First, clear out the dates
        self.redis_client.delete(layer_key + ":dates")
        
        # Re-run periods.py with a new date
        new_date = "2025-03-10T23:00:00"
        calculate_layer_periods(self.redis_client, layer_key, new_date, keep_existing_periods=True)
        
        # We expect to keep the previously existing periods despite a change in the underlying dates
        expected_periods.append(b'2025-03-10T23:00:00Z/2025-03-10T23:00:00Z/P1D')

        periods = self.redis_client.zrange(layer_key + ":periods", 0, -1)
        default = self.redis_client.get(layer_key + ":default").decode('utf-8')
        self.assertEqual(periods, expected_periods, "Returned periods {0} does not match expected periods {1}".format(periods, expected_periods))
        self.assertEqual(default, new_date + "Z", "Returned default date {0} does not match expected default date {1}".format(default, new_date + 'Z'))

    def test_replace_same_start_end_in_zset_when_keeping_existing(self):
        # Old period in zset with same start/end but different duration should be removed
        layer_key = "test_layer_replace_zset"

        # Existing periods as zset
        old_period = "2024-01-01T00:00:00Z/2024-01-01T00:00:00Z/P1D"
        self.redis_client.zadd(layer_key + ":periods", {old_period: 0})

        # Provide dates and a config that will generate a period with same start/end but different duration
        dates = {"2024-01-01T00:00:00": 0}
        self.redis_client.zadd(layer_key + ":dates", dates)
        # Config that forces a PT12H period for the same date
        config = "DETECT"
        self.redis_client.sadd(layer_key + ":config", config)

        calculate_layer_periods(self.redis_client, layer_key, end_date="2024-01-01T12:00:00", keep_existing_periods=True)

        periods = self.redis_client.zrange(layer_key + ":periods", 0, -1)
        # Should only have the new period, old one should be removed
        self.assertEqual(len(periods), 1)
        self.assertTrue(periods[0].decode('utf-8').endswith('/PT12H'))

    def test_replace_same_start_end_in_set_when_keeping_existing(self):
        # Old period in set with same start/end but different duration should be removed
        layer_key = "test_layer_replace_set"

        # Existing periods as set
        old_period = "2024-01-01T00:00:00Z/2024-01-01T00:00:00Z/P1D"
        self.redis_client.sadd(layer_key + ":periods", old_period)

        # Provide dates and a config that will generate a period with same start/end but different duration
        dates = {"2024-01-01T00:00:00": 0}
        self.redis_client.zadd(layer_key + ":dates", dates)
        # Config that forces a PT12H period for the same date
        config = "DETECT"
        self.redis_client.sadd(layer_key + ":config", config)

        calculate_layer_periods(self.redis_client, layer_key, end_date="2024-01-01T12:00:00", keep_existing_periods=True)

        # Should remain as a set and only contain the new period
        self.assertEqual(self.redis_client.type(layer_key + ":periods"), b'set')
        members = self.redis_client.smembers(layer_key + ":periods")
        # Should only have one period with PT12H duration
        self.assertEqual(len(members), 1)
        new_period = list(members)[0].decode('utf-8')
        self.assertTrue(new_period.endswith('/PT12H'))
    
    def test_default_to_last(self):
        # Add different dates but make sure latest is set as default key
        layer_key = "test_layer_default"
        datetimes = [
            '2023-09-07T00:41:00',
            '2024-09-07T00:41:00',
            '2026-10-10T00:27:00']

        # Test Subdaily
        # Add oldest date
        self.redis_client.zadd(layer_key + ":dates", {datetimes[0]: 0})

        # Run calculate without start, end, and keep_existing_periods
        calculate_layer_periods(self.redis_client, layer_key, new_datetime=datetimes[-1],debug=True)
        
        # Simulate varable time best key updates:
        # Add new datetime that isn't as late as the latest datetime,
        # simulating what might be copied to this layer's date's key as best key is updated
        self.redis_client.zadd(layer_key + ":dates", {datetimes[1]: 0})
        # Run calculate with start, end, and keep_existing_periods, and without new_datetime
        calculate_layer_periods(self.redis_client, layer_key, new_datetime=None, 
                                start_date=datetimes[0], end_date='2023-09-07T01:32:00',
                                keep_existing_periods=True, debug=True)

        layer_dates = self.redis_client.zrange(layer_key + ":dates", 0, -1)
        layer_default = self.redis_client.get(layer_key + ":default")
        self.assertEqual(len(layer_dates), len(datetimes),
                         f'Returned dates length of {len(layer_dates)} does not match expected length of {len(datetimes)}')
        self.assertTrue(layer_dates[-1].decode('utf-8') == datetimes[-1],
                        f'Returned last date {layer_dates[-1].decode("utf-8")} does not match expected last date {datetimes[-1]}Z')
        self.assertTrue(layer_default.decode('utf-8') == f'{datetimes[-1]}Z',
                        f'Returned default date {layer_default.decode("utf-8")} does not match expected default date {datetimes[-1]}Z')

        #Test Daily
        date1 = '2026-12-01'
        date2 = '2026-12-25'
        self.redis_client.sadd(layer_key + ":config", 'DETECT/DETECT/P1D')
        # add later date first
        calculate_layer_periods(self.redis_client, layer_key, f'{date2}T00:00:00')
        # then add earlier date
        calculate_layer_periods(self.redis_client, layer_key, f'{date1}T00:00:00')
        layer_dates = self.redis_client.zrange(layer_key + ":dates", 0, -1)
        layer_default = self.redis_client.get(layer_key + ":default")
        self.assertEqual(len(layer_dates), 5,
                         f'Returned dates length of {len(layer_dates)} does not match expected length of 5')
        self.assertTrue(layer_dates[-1].decode('utf-8') == f'{date2}T00:00:00',
                        f'Returned last date {layer_dates[-1].decode("utf-8")} does not match expected last date {date2}T00:00:00')
        # verify that the later date is still the default despite the ealier date being added more recently
        self.assertTrue(layer_default.decode('utf-8') == f'{date2}T00:00:00', 
                        f'Returned default date {layer_default.decode("utf-8")} does not match expected default date {date2}T00:00:00')

    @classmethod
    def tearDownClass(self):
        if not DEBUG:
            os.remove(os.getcwd() + '/periods.py')


if __name__ == '__main__':
    # Parse options before running tests
    parser = OptionParser()
    parser.add_option(
        '-o',
        '--output',
        action='store',
        type='string',
        dest='outfile',
        default='test_periods_results.xml',
        help='Specify XML output file (default is test_periods_results.xml'
    )
    parser.add_option(
        '-d',
        '--debug',
        action='store_true',
        dest='debug',
        help='Output verbose debugging messages')
    (options, args) = parser.parse_args()

    DEBUG = options.debug

    # Have to delete the arguments as they confuse unittest
    del sys.argv[1:]

    with open(options.outfile, 'wb') as f:
        print('\nStoring test results in "{0}"'.format(options.outfile))
        unittest.main(testRunner=xmlrunner.XMLTestRunner(output=f))
