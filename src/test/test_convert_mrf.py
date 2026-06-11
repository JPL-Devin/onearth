#!/usr/bin/env python3

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
# Tests for convert_mrf.py
#

import os
import sys
import shutil
import unittest
import xmlrunner
import hashlib
from optparse import OptionParser
from oe_test_utils import run_command

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), '../mrfgen/convert_mrf.py')

# Expected checksums for verified correct conversions
EXPECTED_CHECKSUMS = {
    'zen_pjg': '62ebd3d84f2fcbe8c45786ff8fb2a844',
    'zen_idx': 'a0dbd898e56a331e158944bab4c38a07',
    'zenbrunsli_pjg': 'af5416ab0c6c49236966566d6ce18a0a',
    'zenbrunsli_idx': 'c54fc61f18939317185d3e34bce60034'
}

class TestConvertMRF(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.testdata_path = os.path.join(os.getcwd(), 'convert_mrf_files/')
        cls.source_mrf_dir = os.path.join(cls.testdata_path, 'source_png_mrf/')
        cls.source_mrf = os.path.join(cls.source_mrf_dir, 'mrf_convert_src.mrf')
        cls.results_dir = os.path.join(os.getcwd(), 'results/')
        
        # Flag to control cleanup (set by command line option)
        cls.keep_results = False
        
        # Create results directory if it doesn't exist
        if not os.path.exists(cls.results_dir):
            os.makedirs(cls.results_dir)
        
        # Output directories for different conversions
        cls.output_zen = os.path.join(cls.results_dir, 'test_zen/')
        cls.output_zenbrunsli = os.path.join(cls.results_dir, 'test_zenbrunsli/')
        cls.output_zen2zenbrunsli = os.path.join(cls.results_dir, 'test_zen2zenbrunsli/')
        cls.output_zenbrunsli2zen = os.path.join(cls.results_dir, 'test_zenbrunsli2zen/')
        
        # Clean up any existing output directories
        for output_dir in [cls.output_zen, cls.output_zenbrunsli, 
                          cls.output_zen2zenbrunsli, cls.output_zenbrunsli2zen]:
            if os.path.exists(output_dir):
                shutil.rmtree(output_dir)
            os.makedirs(output_dir)

    def get_mrf_files(self, output_dir):
        """Get the paths to MRF, data, and index files in output directory."""
        basename = 'mrf_convert_src'
        return {
            'mrf': os.path.join(output_dir, f'{basename}.mrf'),
            'data': os.path.join(output_dir, f'{basename}.pjg'),
            'idx': os.path.join(output_dir, f'{basename}.idx')
        }
    
    def get_tile_count_from_index(self, idx_path):
        """Get the number of tiles from index file size."""
        if not os.path.exists(idx_path):
            return 0
        idx_size = os.path.getsize(idx_path)
        return idx_size // 16  # Each index entry is 16 bytes
    
    def get_file_md5(self, file_path):
        """Calculate MD5 checksum of a file."""
        md5_hash = hashlib.md5()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b''):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    
    def verify_mrf_metadata(self, mrf_path, expected_compression, has_jfif):
        """Verify MRF metadata has correct compression settings."""
        with open(mrf_path, 'r') as f:
            content = f.read()
            
        # Check for JPEG compression
        self.assertIn('<Compression>JPEG</Compression>', content,
                     f"MRF should have JPEG compression: {mrf_path}")
        
        # Check JFIF option
        if has_jfif:
            self.assertIn('<Options>JFIF:on</Options>', content,
                         f"Standard JPEG MRF should have JFIF:on: {mrf_path}")
        else:
            self.assertNotIn('<Options>JFIF:on</Options>', content,
                            f"Brunsli JPEG MRF should not have JFIF:on: {mrf_path}")
        
        # Check for Rsets (overviews)
        self.assertIn('<Rsets model="uniform" scale="2" />', content,
                     f"MRF should have Rsets tag: {mrf_path}")
        
        # Check for GeoTags
        self.assertIn('<GeoTags>', content,
                     f"MRF should have GeoTags: {mrf_path}")

    def test_01_png_to_zenjpeg(self):
        """Test PNG MRF to standard ZenJPEG conversion."""
        print("\n=== Test 1: PNG → ZenJPEG (standard JPEG) ===")
        
        cmd = f"python3 {SCRIPT_PATH} {self.source_mrf} {self.output_zen}"
        run_command(cmd)
        
        files = self.get_mrf_files(self.output_zen)
        
        # Verify all files exist
        self.assertTrue(os.path.exists(files['mrf']), "Output MRF file should exist")
        self.assertTrue(os.path.exists(files['data']), "Output .pjg file should exist")
        self.assertTrue(os.path.exists(files['idx']), "Output .idx file should exist")
        
        # Verify tile count (should have base tiles + overviews = 1074 tiles)
        tile_count = self.get_tile_count_from_index(files['idx'])
        self.assertEqual(tile_count, 1074, 
                        f"Should have 1074 tiles (800 base + 274 overview), got {tile_count}")
        
        # Verify MRF metadata
        self.verify_mrf_metadata(files['mrf'], 'JPEG', has_jfif=True)
        
        # Verify data file is not empty
        data_size = os.path.getsize(files['data'])
        self.assertGreater(data_size, 0, "Data file should not be empty")
        
        # Verify checksums match expected values
        pjg_md5 = self.get_file_md5(files['data'])
        idx_md5 = self.get_file_md5(files['idx'])
        self.assertEqual(pjg_md5, EXPECTED_CHECKSUMS['zen_pjg'],
                        f"Data file checksum mismatch: {pjg_md5} != {EXPECTED_CHECKSUMS['zen_pjg']}")
        self.assertEqual(idx_md5, EXPECTED_CHECKSUMS['zen_idx'],
                        f"Index file checksum mismatch: {idx_md5} != {EXPECTED_CHECKSUMS['zen_idx']}")
        
        print(f"✓ PNG → ZenJPEG conversion successful")
        print(f"  Data size: {data_size:,} bytes")
        print(f"  Tile count: {tile_count}")
        print(f"  Data MD5: {pjg_md5}")
        print(f"  Index MD5: {idx_md5}")

    def test_02_png_to_zenbrunsli(self):
        """Test PNG MRF to brunsli ZenJPEG conversion."""
        print("\n=== Test 2: PNG → Brunsli ZenJPEG ===")
        
        cmd = f"python3 {SCRIPT_PATH} {self.source_mrf} {self.output_zenbrunsli} --brunsli"
        run_command(cmd)
        
        files = self.get_mrf_files(self.output_zenbrunsli)
        
        # Verify all files exist
        self.assertTrue(os.path.exists(files['mrf']), "Output MRF file should exist")
        self.assertTrue(os.path.exists(files['data']), "Output .pjg file should exist")
        self.assertTrue(os.path.exists(files['idx']), "Output .idx file should exist")
        
        # Verify tile count
        tile_count = self.get_tile_count_from_index(files['idx'])
        self.assertEqual(tile_count, 1074, 
                        f"Should have 1074 tiles (800 base + 274 overview), got {tile_count}")
        
        # Verify MRF metadata (brunsli = no JFIF)
        self.verify_mrf_metadata(files['mrf'], 'JPEG', has_jfif=False)
        
        # Verify data file is not empty and smaller than standard JPEG
        data_size = os.path.getsize(files['data'])
        self.assertGreater(data_size, 0, "Data file should not be empty")
        
        # Brunsli should be smaller than standard JPEG (roughly 20-25% smaller)
        zen_data_size = os.path.getsize(self.get_mrf_files(self.output_zen)['data'])
        compression_ratio = data_size / zen_data_size
        self.assertLess(compression_ratio, 0.85, 
                       f"Brunsli should be smaller than standard JPEG (ratio: {compression_ratio:.2%})")
        
        # Verify checksums match expected values
        pjg_md5 = self.get_file_md5(files['data'])
        idx_md5 = self.get_file_md5(files['idx'])
        self.assertEqual(pjg_md5, EXPECTED_CHECKSUMS['zenbrunsli_pjg'],
                        f"Data file checksum mismatch: {pjg_md5} != {EXPECTED_CHECKSUMS['zenbrunsli_pjg']}")
        self.assertEqual(idx_md5, EXPECTED_CHECKSUMS['zenbrunsli_idx'],
                        f"Index file checksum mismatch: {idx_md5} != {EXPECTED_CHECKSUMS['zenbrunsli_idx']}")
        
        print(f"✓ PNG → Brunsli ZenJPEG conversion successful")
        print(f"  Data size: {data_size:,} bytes")
        print(f"  Compression ratio vs standard: {compression_ratio:.2%}")
        print(f"  Tile count: {tile_count}")
        print(f"  Data MD5: {pjg_md5}")
        print(f"  Index MD5: {idx_md5}")

    def test_03_zenjpeg_to_zenbrunsli(self):
        """Test ZenJPEG to brunsli ZenJPEG conversion (lossless)."""
        print("\n=== Test 3: ZenJPEG → Brunsli ZenJPEG ===")
        
        zen_mrf = self.get_mrf_files(self.output_zen)['mrf']
        cmd = f"python3 {SCRIPT_PATH} {zen_mrf} {self.output_zen2zenbrunsli} --brunsli"
        run_command(cmd)
        
        files = self.get_mrf_files(self.output_zen2zenbrunsli)
        
        # Verify all files exist
        self.assertTrue(os.path.exists(files['mrf']), "Output MRF file should exist")
        self.assertTrue(os.path.exists(files['data']), "Output .pjg file should exist")
        self.assertTrue(os.path.exists(files['idx']), "Output .idx file should exist")
        
        # Verify tile count
        tile_count = self.get_tile_count_from_index(files['idx'])
        self.assertEqual(tile_count, 1074, f"Should have 1074 tiles, got {tile_count}")
        
        # Verify MRF metadata
        self.verify_mrf_metadata(files['mrf'], 'JPEG', has_jfif=False)
        
        # Verify data file size matches direct PNG→Brunsli conversion
        data_size = os.path.getsize(files['data'])
        direct_brunsli_size = os.path.getsize(self.get_mrf_files(self.output_zenbrunsli)['data'])
        self.assertEqual(data_size, direct_brunsli_size,
                        "ZenJPEG→Brunsli should produce same size as PNG→Brunsli")
        
        # Verify checksums match expected values (should match zen2brunsli)
        pjg_md5 = self.get_file_md5(files['data'])
        idx_md5 = self.get_file_md5(files['idx'])
        self.assertEqual(pjg_md5, EXPECTED_CHECKSUMS['zenbrunsli_pjg'],
                        f"Data file checksum mismatch: {pjg_md5} != {EXPECTED_CHECKSUMS['zenbrunsli_pjg']}")
        self.assertEqual(idx_md5, EXPECTED_CHECKSUMS['zenbrunsli_idx'],
                        f"Index file checksum mismatch: {idx_md5} != {EXPECTED_CHECKSUMS['zenbrunsli_idx']}")
        
        print(f"✓ ZenJPEG → Brunsli ZenJPEG conversion successful")
        print(f"  Data size: {data_size:,} bytes")
        print(f"  Matches direct PNG→Brunsli: {data_size == direct_brunsli_size}")
        print(f"  Data MD5: {pjg_md5}")
        print(f"  Index MD5: {idx_md5}")

    def test_04_zenbrunsli_to_zenjpeg(self):
        """Test brunsli ZenJPEG to standard ZenJPEG conversion (lossless round-trip)."""
        print("\n=== Test 4: Brunsli ZenJPEG → ZenJPEG (round-trip) ===")
        
        zenbrunsli_mrf = self.get_mrf_files(self.output_zen2zenbrunsli)['mrf']
        cmd = f"python3 {SCRIPT_PATH} {zenbrunsli_mrf} {self.output_zenbrunsli2zen}"
        run_command(cmd)
        
        files = self.get_mrf_files(self.output_zenbrunsli2zen)
        
        # Verify all files exist
        self.assertTrue(os.path.exists(files['mrf']), "Output MRF file should exist")
        self.assertTrue(os.path.exists(files['data']), "Output .pjg file should exist")
        self.assertTrue(os.path.exists(files['idx']), "Output .idx file should exist")
        
        # Verify tile count
        tile_count = self.get_tile_count_from_index(files['idx'])
        self.assertEqual(tile_count, 1074, f"Should have 1074 tiles, got {tile_count}")
        
        # Verify MRF metadata
        self.verify_mrf_metadata(files['mrf'], 'JPEG', has_jfif=True)
        
        # Verify round-trip: should match original ZenJPEG size
        data_size = os.path.getsize(files['data'])
        original_zen_size = os.path.getsize(self.get_mrf_files(self.output_zen)['data'])
        self.assertEqual(data_size, original_zen_size,
                        "Round-trip conversion should restore original size (lossless)")
        
        # Verify checksums match expected values (should match original zen)
        pjg_md5 = self.get_file_md5(files['data'])
        idx_md5 = self.get_file_md5(files['idx'])
        self.assertEqual(pjg_md5, EXPECTED_CHECKSUMS['zen_pjg'],
                        f"Data file checksum mismatch: {pjg_md5} != {EXPECTED_CHECKSUMS['zen_pjg']}")
        self.assertEqual(idx_md5, EXPECTED_CHECKSUMS['zen_idx'],
                        f"Index file checksum mismatch: {idx_md5} != {EXPECTED_CHECKSUMS['zen_idx']}")
        
        print(f"✓ Brunsli ZenJPEG → ZenJPEG round-trip successful")
        print(f"  Data size: {data_size:,} bytes")
        print(f"  Matches original ZenJPEG: {data_size == original_zen_size}")
        print(f"  Data MD5: {pjg_md5}")
        print(f"  Index MD5: {idx_md5}")

    def test_05_mrf_metadata_consistency(self):
        """Test that MRF metadata is consistent across conversion paths."""
        print("\n=== Test 5: MRF Metadata Consistency ===")
        
        # Get all standard JPEG MRFs
        zen_mrf = self.get_mrf_files(self.output_zen)['mrf']
        zen_roundtrip_mrf = self.get_mrf_files(self.output_zenbrunsli2zen)['mrf']
        
        # Get all brunsli MRFs
        zenbrunsli_mrf = self.get_mrf_files(self.output_zenbrunsli)['mrf']
        zen2zenbrunsli_mrf = self.get_mrf_files(self.output_zen2zenbrunsli)['mrf']
        
        # Compare standard JPEG MRF sizes
        zen_size = os.path.getsize(zen_mrf)
        zen_roundtrip_size = os.path.getsize(zen_roundtrip_mrf)
        self.assertEqual(zen_size, zen_roundtrip_size,
                        "Standard JPEG MRFs should have identical metadata size")
        
        # Compare brunsli MRF sizes
        zenbrunsli_size = os.path.getsize(zenbrunsli_mrf)
        zen2zenbrunsli_size = os.path.getsize(zen2zenbrunsli_mrf)
        self.assertEqual(zenbrunsli_size, zen2zenbrunsli_size,
                        "Brunsli MRFs should have identical metadata size")
        
        print(f"✓ MRF metadata consistency verified")
        print(f"  Standard JPEG MRF size: {zen_size} bytes")
        print(f"  Brunsli MRF size: {zenbrunsli_size} bytes")

    @classmethod
    def tearDownClass(cls):
        """Clean up test outputs unless keep_results flag is set."""
        if cls.keep_results:
            print(f"\n✓ Test results preserved in: {cls.results_dir}")
        else:
            # Clean up all test output directories
            for output_dir in [cls.output_zen, cls.output_zenbrunsli, 
                              cls.output_zen2zenbrunsli, cls.output_zenbrunsli2zen]:
                if os.path.exists(output_dir):
                    shutil.rmtree(output_dir)
            
            # Remove results directory if empty
            if os.path.exists(cls.results_dir) and not os.listdir(cls.results_dir):
                os.rmdir(cls.results_dir)
            
            print(f"\n✓ Test outputs cleaned up")

if __name__ == '__main__':
    # Parse options before running tests
    parser = OptionParser()
    parser.add_option(
        '-o',
        '--output',
        action='store',
        type='string',
        dest='outfile',
        default='test_convert_mrf_results.xml',
        help='Specify XML output file (default is test_convert_mrf_results.xml')
    parser.add_option(
        '-s',
        '--start_server',
        action='store_true',
        dest='start_server',
        help='Load test configuration and quit (for debugging)')
    parser.add_option(
        '-k',
        '--keep-results',
        action='store_true',
        dest='keep_results',
        help='Keep test output files for inspection (default is to clean up)')
    (options, args) = parser.parse_args()

    # Set the keep_results flag before running tests
    TestConvertMRF.keep_results = options.keep_results

    # --start_server option runs the test setup, then quits.
    if options.start_server:
        TestConvertMRF.setUpClass()
        sys.exit('Test setup complete. No tests run.')

    # Have to delete the arguments as they confuse unittest
    del sys.argv[1:]

    with open(options.outfile, 'wb') as f:
        print('\nStoring test results in "{0}"'.format(options.outfile))
        unittest.main(testRunner=xmlrunner.XMLTestRunner(output=f))
