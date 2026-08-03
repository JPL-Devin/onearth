`uat_stress_test.py` has stress tests specifially for UAT. You can change the environment by editing the `UAT_BASE_URL` variable 

Before running these tests, you should generate the time ranges for the layers in the test by running `time_ranges_for_stress_test.py`. The same layers that need time information are hard coded into both files so you don't need to change anything 
`time_ranges_for_stress_test.py` checks the gitc-uat-mrf-archive bucket in s3 to see the date ranges to make correct tile requests 