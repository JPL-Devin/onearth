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

"""
MRF Conversion Tool

This script converts MRFs between different compression formats:
  - PNG → JPEG (standard, ZenJPEG, or brunsli-compressed)
  - JPEG → Brunsli (lossless compression, ~22% size reduction)
  - Brunsli → JPEG (lossless decompression, restores exact original)

Conversion methods:
  1. Standard method: Fast bulk conversion using GDAL tools (PNG → standard JPEG only)
  2. Tile-by-tile method: Extracts and converts tiles individually
     - Required for: brunsli output, brunsli input, or JPEG input
     - Uses cbrunsli/dbrunsli for lossless JPEG ↔ Brunsli conversions

S3 Support:
  - Input and output paths can be S3 URIs (s3://bucket/key)
  - Automatically downloads from S3 and uploads results
  - Requires AWS credentials configured (environment, credentials file, or IAM role)

Usage:
    python3 convert_mrf.py <input_mrf> <output_dir> [options]

Examples:
    # PNG to standard JPEG (fast)
    python3 convert_mrf.py png_input.mrf output_dir/
    
    # PNG to brunsli JPEG
    python3 convert_mrf.py png_input.mrf output_dir/ --brunsli
    
    # JPEG to brunsli (auto-detected, lossless)
    python3 convert_mrf.py jpeg_input.mrf output_dir/ --brunsli
    
    # Brunsli to JPEG (auto-detected, lossless)
    python3 convert_mrf.py brunsli_input.mrf output_dir/
    
    # Custom quality
    python3 convert_mrf.py input.mrf output_dir/ --quality 90
    
    # Custom output filename
    python3 convert_mrf.py input.mrf output_dir/ --output-name custom_name.mrf
    
    # S3 to S3 conversion
    python3 convert_mrf.py s3://bucket/input.mrf s3://bucket/output/
    
    # S3 to local
    python3 convert_mrf.py s3://bucket/input.mrf /local/output/
    
    # Local to S3
    python3 convert_mrf.py /local/input.mrf s3://bucket/output/
    
    # With sigevent monitoring (for production pipelines)
    python3 convert_mrf.py input.mrf output_dir/ --sigevent-url http://monitor/sigevent
"""

import argparse
import os
import subprocess
import sys
import tempfile
import shutil
import struct
import json
import re
from pathlib import Path
import boto3
import botocore

DEFAULT_JPEG_QUALITY = 80
DEFAULT_BLOCK_SIZE = 512

# Global sigevent URL for monitoring (set via command line or None for standalone use)
SIGEVENT_URL = None


def is_s3_path(path):
    """Check if a path is an S3 URI."""
    return path.startswith('s3://')


def parse_s3_path(s3_path):
    """Parse S3 path into bucket and key."""
    if not s3_path.startswith('s3://'):
        raise ValueError(f"Invalid S3 path: {s3_path}")
    
    path_parts = s3_path[5:].split('/', 1)
    bucket = path_parts[0]
    key = path_parts[1] if len(path_parts) > 1 else ''
    
    return bucket, key


def download_from_s3(s3_path, local_path, s3_client=None):
    """Download a file from S3 to local path."""
    if s3_client is None:
        s3_client = boto3.client('s3')
    
    bucket, key = parse_s3_path(s3_path)
    
    print(f"Downloading from S3: {s3_path} -> {local_path}")
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    
    try:
        s3_client.download_file(bucket, key, local_path)
        return True
    except botocore.exceptions.ClientError as e:
        print(f"Error downloading from S3: {e}")
        return False


def upload_to_s3(local_path, s3_path, s3_client=None):
    """Upload a file from local path to S3."""
    if s3_client is None:
        s3_client = boto3.client('s3')
    
    bucket, key = parse_s3_path(s3_path)
    
    print(f"Uploading to S3: {local_path} -> {s3_path}")
    
    try:
        s3_client.upload_file(local_path, bucket, key)
        return True
    except botocore.exceptions.ClientError as e:
        print(f"Error uploading to S3: {e}")
        return False


def download_mrf_from_s3(s3_mrf_path, local_dir, s3_client=None):
    """Download MRF and its associated files (.idx, .ppg/.pjg) from S3."""
    if s3_client is None:
        s3_client = boto3.client('s3')
    
    bucket, key = parse_s3_path(s3_mrf_path)
    base_key = key.rsplit('.', 1)[0] if '.' in key else key
    base_name = os.path.basename(base_key)
    
    # Download .mrf file
    local_mrf = os.path.join(local_dir, base_name + '.mrf')
    if not download_from_s3(s3_mrf_path, local_mrf, s3_client):
        raise ValueError(f"Failed to download MRF file: {s3_mrf_path}")
    
    # Download .idx file
    s3_idx = f"s3://{bucket}/{base_key}.idx"
    local_idx = os.path.join(local_dir, base_name + '.idx')
    if not download_from_s3(s3_idx, local_idx, s3_client):
        raise ValueError(f"Failed to download IDX file: {s3_idx}")
    
    # Try to download .ppg or .pjg file
    for ext in ['.ppg', '.pjg']:
        s3_data = f"s3://{bucket}/{base_key}{ext}"
        local_data = os.path.join(local_dir, base_name + ext)
        if download_from_s3(s3_data, local_data, s3_client):
            break
    else:
        raise ValueError(f"Failed to download data file (.ppg or .pjg): {base_key}")
    
    return local_mrf


def upload_mrf_to_s3(local_mrf, s3_output_path, s3_client=None):
    """Upload MRF and its associated files (.idx, .pjg) to S3."""
    if s3_client is None:
        s3_client = boto3.client('s3')
    
    local_mrf_path = Path(local_mrf)
    base_name = local_mrf_path.stem
    local_dir = local_mrf_path.parent
    
    # Parse S3 output path
    bucket, key_prefix = parse_s3_path(s3_output_path)
    
    # If key_prefix ends with .mrf, use it as the base
    if key_prefix.endswith('.mrf'):
        s3_base = key_prefix.rsplit('.', 1)[0]
    else:
        # Otherwise, append the base name to the prefix
        s3_base = os.path.join(key_prefix, base_name).replace('\\', '/')
    
    # Upload .mrf file
    local_mrf_file = str(local_mrf_path)
    s3_mrf = f"s3://{bucket}/{s3_base}.mrf"
    if not upload_to_s3(local_mrf_file, s3_mrf, s3_client):
        raise ValueError(f"Failed to upload MRF file: {s3_mrf}")
    
    # Upload .idx file
    local_idx = os.path.join(local_dir, base_name + '.idx')
    s3_idx = f"s3://{bucket}/{s3_base}.idx"
    if not upload_to_s3(local_idx, s3_idx, s3_client):
        raise ValueError(f"Failed to upload IDX file: {s3_idx}")
    
    # Upload .pjg file
    local_pjg = os.path.join(local_dir, base_name + '.pjg')
    s3_pjg = f"s3://{bucket}/{s3_base}.pjg"
    if not upload_to_s3(local_pjg, s3_pjg, s3_client):
        raise ValueError(f"Failed to upload data file: {s3_pjg}")
    
    print(f"\nMRF successfully uploaded to S3: {s3_mrf}")
    return s3_mrf


def run_command(command, quiet=False):
    """
    Run a shell command and return output.
    If quiet=False and SIGEVENT_URL is set, logs to sigevent.
    """
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    
    if not quiet and SIGEVENT_URL:
        # Log to sigevent if configured and not in quiet mode
        from oe_utils import log_info_mssg
        log_info_mssg(' '.join(command))
    
    return result.stdout


def get_mrf_info(mrf_path):
    """Get MRF metadata using gdalinfo."""
    gdalinfo_command = ['gdalinfo', '--config', 'GDAL_TMPDIR', '/tmp', '-json', mrf_path]
    
    json_output = run_command(gdalinfo_command)
    return json.loads(json_output)


def detect_compression(mrf_path):
    """
    Detect the compression type of an MRF by checking file signatures.
    
    JPEG files always start with FF D8 FF (JPEG SOI marker, ITU T.81 standard).
    Brunsli files use a different format and don't start with FF D8 FF.
    """
    mrf_path_obj = Path(mrf_path)
    
    # Check data file extension
    pjg_file = mrf_path_obj.with_suffix('.pjg')
    ppg_file = mrf_path_obj.with_suffix('.ppg')
    
    if pjg_file.exists():
        # Read first few bytes to detect format by file signature
        try:
            with open(pjg_file, 'rb') as f:
                header = f.read(3)
                
            # Check for JPEG signature (FF D8 FF - JPEG SOI marker)
            if header == b'\xff\xd8\xff':
                return 'JPEG'
            # If it's a .pjg file but not JPEG, it must be Brunsli
            else:
                return 'JPEG_BRUNSLI'
        except Exception as e:
            raise ValueError(f"Failed to read data file: {e}")
    elif ppg_file.exists():
        return 'PNG'
    
    raise ValueError(f"Unknown MRF format: no .pjg or .ppg file found")


def apply_alpha_mask_to_grayscale(input_path, output_path):
    """
    Apply alpha mask to grayscale+alpha image.
    
    Handles the Zen mask limitation where value=0 means transparent:
    - If alpha=0 (transparent): set to 0 (for Zen mask)
    - If alpha>0 and gray=0 (black opaque): set to 1 (visually black but not transparent)
    - Otherwise: keep original grayscale value
    
    Args:
        input_path: Path to input image with 2 bands (grayscale + alpha)
        output_path: Path to output single-band grayscale GTiff
    """
    import numpy as np
    from osgeo import gdal
    
    src_ds = gdal.Open(input_path)
    gray_band = src_ds.GetRasterBand(1).ReadAsArray()
    alpha_band = src_ds.GetRasterBand(2).ReadAsArray()
    
    # Apply alpha mask with black opaque pixel handling
    result = np.where(alpha_band == 0, 0, 
                     np.where(gray_band == 0, 1, gray_band))
    
    # Write result to output file
    driver = gdal.GetDriverByName('GTiff')
    x_size = src_ds.RasterXSize
    y_size = src_ds.RasterYSize
    out_ds = driver.Create(output_path, x_size, y_size, 1, gdal.GDT_Byte)
    
    if src_ds.GetGeoTransform():
        out_ds.SetGeoTransform(src_ds.GetGeoTransform())
    if src_ds.GetProjection():
        out_ds.SetProjection(src_ds.GetProjection())
    
    out_ds.GetRasterBand(1).WriteArray(result)
    out_ds.FlushCache()
    
    # Close the files
    out_ds = None
    src_ds = None


def convert_png_to_jpeg_standard(input_mrf, output_dir, quality=DEFAULT_JPEG_QUALITY, output_name=None):
    """
    Convert PNG MRF to standard JPEG/ZenJPEG using GDAL tools (fast bulk conversion).
    This is the fastest method but only works for PNG → standard JPEG/ZenJPEG.
    """
    print("Converting PNG MRF to standard JPEG/ZenJPEG MRF (Fast Method)")
    print(f"Input:  {input_mrf}")
    print(f"Output: {output_dir}")
    print("-" * 60)
    
    input_mrf_path = Path(input_mrf)
    os.makedirs(output_dir, exist_ok=True)
    
    # Get MRF info
    print("\nStep 1/4: Reading source MRF metadata...")
    info = get_mrf_info(input_mrf)
    
    size_x = info['size'][0]
    size_y = info['size'][1]
    bands = info.get('bands', [])
    
    # Read blocksize from source MRF
    input_mrf_path = Path(input_mrf)
    with open(input_mrf_path, 'r') as f:
        mrf_content = f.read()
        blocksize_match = re.search(r'<PageSize x="(\d+)"', mrf_content)
        blocksize = int(blocksize_match.group(1)) if blocksize_match else DEFAULT_BLOCK_SIZE
    
    # Get overview levels from source
    source_overviews = []
    if bands and 'overviews' in bands[0]:
        for overview in bands[0]['overviews']:
            ov_size_x = overview['size'][0]
            level = size_x // ov_size_x
            if level > 1:
                source_overviews.append(level)
    
    print(f"  Size: {size_x} x {size_y}")
    print(f"  Bands: {len(bands)}")
    if source_overviews:
        print(f"  Source overviews: {source_overviews}")
    
    # Check if source is paletted (1 band with colorInterpretation = Palette)
    is_paletted = (len(bands) == 1 and 
                   bands[0].get('colorInterpretation') == 'Palette')
    
    # Check if source is grayscale with alpha (2 bands: Gray + Alpha)
    is_grayscale_alpha = (len(bands) == 2 and 
                          bands[0].get('colorInterpretation') == 'Gray' and
                          bands[1].get('colorInterpretation') == 'Alpha')
    
    output_basename = output_name if output_name else input_mrf_path.name
    if not output_basename.endswith('.mrf'):
        output_basename += '.mrf'
    output_mrf = os.path.join(output_dir, output_basename)
    
    # Create VRT from input MRF
    print("\nStep 2/4: Creating VRT from source PNG MRF...")
    vrt_path = output_mrf.replace('.mrf', '_source.vrt')
    
    if is_grayscale_alpha:
        # For grayscale+alpha, apply alpha mask preprocessing
        print(f"  Applying alpha mask (transparent→0, black opaque→1)...")
        temp_tif = output_mrf.replace('.mrf', '_temp.tif')
        apply_alpha_mask_to_grayscale(input_mrf, temp_tif)
        
        # Create VRT from the temp file
        vrt_cmd = ['gdalbuildvrt', vrt_path, temp_tif]
        run_command(vrt_cmd)
        print(f"  VRT created: {vrt_path}")
    else:
        vrt_cmd = ['gdalbuildvrt', vrt_path, input_mrf]
        run_command(vrt_cmd)
        print(f"  VRT created: {vrt_path}")
    
    # Convert VRT to JPEG MRF
    print("\nStep 3/4: Converting to ZenJPEG MRF...")
    gdal_cmd = [
        'gdal_translate',
        '-of', 'MRF',
        '-co', 'COMPRESS=JPEG',
        '-co', f'BLOCKSIZE={blocksize}',
        '-co', f'QUALITY={quality}',
        '-co', 'OPTIONS=JFIF:on'
    ]
    
    if is_paletted:
        # For paletted images, use -expand rgb to convert to 3-band RGB
        gdal_cmd.extend(['-expand', 'rgb'])
        print(f"  Expanding paletted image to RGB")
    elif is_grayscale_alpha:
        # For grayscale+alpha, we've already applied alpha to create single-band grayscale
        # with transparent pixels set to black (0) - just use band 1
        gdal_cmd.extend(['-b', '1'])
        print(f"  Converting to 1-band JPEG (Zen mask for zero/transparent pixels)")
    else:
        # For non-paletted, select bands (up to 3 for RGB JPEG)
        for i in range(1, min(4, len(bands) + 1)):
            gdal_cmd.extend(['-b', str(i)])
    
    gdal_cmd.extend([vrt_path, output_mrf])
    run_command(gdal_cmd)
    print(f"  ZenJPEG MRF created")
    
    # Generate overviews
    print("\nStep 4/4: Building overviews...")
    if source_overviews:
        overview_levels = [str(level) for level in source_overviews]
        print(f"  Using source MRF overview levels: {' '.join(overview_levels)}")
        
        gdaladdo_cmd = [
            'gdaladdo',
            '-r', 'nearest',
            output_mrf
        ] + overview_levels
        run_command(gdaladdo_cmd)
        print(f"  Overviews created: {' '.join(overview_levels)}")
    else:
        print("  Source MRF has no overviews - skipping overview creation")
    
    # Clean up VRT and temp files
    try:
        os.remove(vrt_path)
        if is_grayscale_alpha:
            temp_tif = output_mrf.replace('.mrf', '_temp.tif')
            if os.path.exists(temp_tif):
                os.remove(temp_tif)
    except:
        pass
    
    print("\n" + "=" * 60)
    print(f"SUCCESS: Conversion complete!")
    print(f"Output MRF: {output_mrf}")
    print("=" * 60)
    
    return True


def convert_tile_by_tile(input_mrf_path, output_dir, temp_dir_base, no_cleanup, input_compression, output_compression, quality=DEFAULT_JPEG_QUALITY, output_name=None):
    """
    Convert MRF by processing tiles individually.
    Required for: PNG→Brunsli, JPEG→Brunsli, or Brunsli→JPEG conversions.
    Uses cbrunsli/dbrunsli for lossless JPEG ↔ Brunsli conversions.
    """
    conversion_name = f"{input_compression} → {output_compression}"
    print(f"Converting {conversion_name} (Tile-by-Tile Method)")
    print(f"Input:  {input_mrf_path}")
    print(f"Output: {output_dir}")
    print("-" * 60)
    
    temp_dir = None
    
    try:
        # Get input MRF info
        print("\n1/6: Extracting metadata from input MRF...")
        
        gdal_config = ['--config', 'GDAL_TMPDIR', '/tmp']
        
        info = get_mrf_info(input_mrf_path)
        size_x = info['size'][0]
        size_y = info['size'][1]
        
        # Get overview info from bands
        overview_factors = []
        bands = info.get('bands', [])
        num_bands = len(bands)
        if bands and 'overviews' in bands[0]:
            for overview in bands[0]['overviews']:
                factor = round(size_x / overview['size'][0])
                if factor > 1:
                    overview_factors.append(factor)
        
        if overview_factors:
            print(f"Detected: {size_x}x{size_y}, {num_bands} band(s), overviews: {overview_factors}")
        else:
            print(f"Detected: {size_x}x{size_y}, {num_bands} band(s), no overviews")
        
        # Read source MRF metadata
        with open(input_mrf_path, 'r') as f:
            mrf_content = f.read()
            # For JPEG input, read quality from source; for PNG input, use provided quality
            if input_compression != 'PNG':
                quality_match = re.search(r'<Quality>(\d+)</Quality>', mrf_content)
                if quality_match:
                    quality = int(quality_match.group(1))
            blocksize_match = re.search(r'<PageSize x="(\d+)"', mrf_content)
            blocksize = int(blocksize_match.group(1)) if blocksize_match else DEFAULT_BLOCK_SIZE
        
        # Setup paths
        input_mrf_path_obj = Path(input_mrf_path)
        source_data_path = input_mrf_path_obj.with_suffix('.pjg' if input_compression != 'PNG' else '.ppg')
        source_idx_path = input_mrf_path_obj.with_suffix('.idx')
        
        os.makedirs(output_dir, exist_ok=True)
        output_basename = output_name if output_name else os.path.basename(input_mrf_path)
        if not output_basename.endswith('.mrf'):
            output_basename += '.mrf'
        final_mrf_path = os.path.join(output_dir, output_basename)
        final_data_path = final_mrf_path.replace('.mrf', '.pjg')
        final_idx_path = final_mrf_path.replace('.mrf', '.idx')
        
        # Create temp directory
        if temp_dir_base and temp_dir_base != os.environ.get('TMPDIR', '/tmp'):
            os.makedirs(temp_dir_base, exist_ok=True)
            temp_dir = tempfile.mkdtemp(dir=temp_dir_base)
        else:
            temp_dir = tempfile.mkdtemp()
        
        print(f"Using temp directory: {temp_dir}")
        
        # Create/modify output MRF metadata
        print("\n2/6: Creating MRF metadata...")
        
        # Check if source is grayscale with alpha (2 bands: Gray + Alpha)
        is_grayscale_alpha = (num_bands == 2 and 
                              bands[0].get('colorInterpretation') == 'Gray' and
                              bands[1].get('colorInterpretation') == 'Alpha')
        
        # Set output channels: 1 for grayscale+alpha (Zen mask), 3 for RGB
        output_channels = 1 if is_grayscale_alpha else 3
        
        mrf_output_content = f"""<MRF_META>
  <Raster>
    <Size x="{size_x}" y="{size_y}" c="{output_channels}" />
    <PageSize x="{blocksize}" y="{blocksize}" c="{output_channels}" />
    <Compression>JPEG</Compression>
    <Quality>{quality}</Quality>
  </Raster>"""
        
        # Add JFIF option for standard JPEG, omit for brunsli
        if output_compression == 'JPEG':
            mrf_output_content += "\n  <Options>JFIF:on</Options>"
        
        # Add Rsets if source has overviews
        if overview_factors:
            mrf_output_content += "\n  <Rsets model=\"uniform\" scale=\"2\" />"
        
        # Copy GeoTags from source if present
        if '<GeoTags>' in mrf_content:
            geotags_match = re.search(r'(<GeoTags>.*?</GeoTags>)', mrf_content, re.DOTALL)
            if geotags_match:
                mrf_output_content += "\n  " + geotags_match.group(1).replace('\n', '\n  ')
        
        mrf_output_content += "\n</MRF_META>\n"
        
        # Write output MRF
        with open(final_mrf_path, 'w') as f:
            f.write(mrf_output_content)
        
        tiles_x = (size_x + blocksize - 1) // blocksize
        tiles_y = (size_y + blocksize - 1) // blocksize
        total_tiles = tiles_x * tiles_y
        
        # Get actual number of tiles from source index
        source_idx_size = os.path.getsize(source_idx_path)
        total_index_entries = source_idx_size // 16
        
        print(f"\n3/6: Converting {total_tiles} base tiles ({tiles_x}x{tiles_y})...")
        print(f"   Total tiles in source (including overviews): {total_index_entries}")
        print(f"   Pre-allocating index for {total_index_entries} tiles")
        
        data_file = open(final_data_path, 'wb')
        idx_file = open(final_idx_path, 'wb')
        
        idx_file.write(b'\x00' * (16 * total_index_entries))
        idx_file.seek(0)
        
        current_offset = 0
        processed = 0
        
        # Process based on conversion type
        if input_compression in ['JPEG', 'JPEG_BRUNSLI'] and output_compression in ['JPEG', 'JPEG_BRUNSLI']:
            # JPEG ↔ Brunsli: Use cbrunsli/dbrunsli for lossless conversion
            with open(source_data_path, 'rb') as source_data:
                with open(source_idx_path, 'rb') as source_idx:
                    for tile_idx in range(total_index_entries):
                        idx_entry = source_idx.read(16)
                        if len(idx_entry) < 16:
                            break
                        
                        src_offset, src_size = struct.unpack('>QQ', idx_entry)
                        
                        if src_size == 0:
                            # Empty tile
                            idx_file.write(struct.pack('>QQ', 0, 0))
                            continue
                        
                        # Read source data
                        source_data.seek(src_offset)
                        source_tile_data = source_data.read(src_size)
                        
                        if len(source_tile_data) != src_size:
                            raise ValueError(f"Failed to read complete tile data at tile {tile_idx}")
                        
                        # Convert using cbrunsli/dbrunsli
                        if input_compression == 'JPEG' and output_compression == 'JPEG_BRUNSLI':
                            # JPEG → Brunsli
                            jpeg_file = os.path.join(temp_dir, f"tile_{tile_idx}.jpg")
                            brunsli_file = os.path.join(temp_dir, f"tile_{tile_idx}.brn")
                            
                            with open(jpeg_file, 'wb') as f:
                                f.write(source_tile_data)
                            
                            cbrunsli_cmd = ['cbrunsli', jpeg_file, brunsli_file]
                            run_command(cbrunsli_cmd, quiet=True)
                            
                            with open(brunsli_file, 'rb') as f:
                                output_tile_data = f.read()
                            
                            try:
                                os.remove(jpeg_file)
                                os.remove(brunsli_file)
                            except:
                                pass
                        
                        elif input_compression == 'JPEG_BRUNSLI' and output_compression == 'JPEG':
                            # Brunsli → JPEG
                            brunsli_file = os.path.join(temp_dir, f"tile_{tile_idx}.brn")
                            jpeg_file = os.path.join(temp_dir, f"tile_{tile_idx}.jpg")
                            
                            with open(brunsli_file, 'wb') as f:
                                f.write(source_tile_data)
                            
                            dbrunsli_cmd = ['dbrunsli', brunsli_file, jpeg_file]
                            run_command(dbrunsli_cmd, quiet=True)
                            
                            with open(jpeg_file, 'rb') as f:
                                output_tile_data = f.read()
                            
                            try:
                                os.remove(brunsli_file)
                                os.remove(jpeg_file)
                            except:
                                pass
                        
                        tile_size = len(output_tile_data)
                        data_file.write(output_tile_data)
                        idx_file.write(struct.pack('>QQ', current_offset, tile_size))
                        
                        current_offset += tile_size
                        processed += 1
                        
                        if processed % 100 == 0 or tile_idx == total_index_entries - 1:
                            print(f"  Processed {processed}/{total_index_entries} tiles...")
        
        else:
            # PNG → JPEG/Brunsli: Extract and encode tiles
            for ty in range(tiles_y):
                for tx in range(tiles_x):
                    x_off = tx * blocksize
                    y_off = ty * blocksize
                    x_size = min(blocksize, size_x - x_off)
                    y_size = min(blocksize, size_y - y_off)
                    
                    # Extract tile as PNG
                    tile_png = os.path.join(temp_dir, f"tile.png")
                    extract_cmd = [
                        'gdal_translate', '-q'
                    ] + gdal_config + [
                        '-of', 'PNG',
                        '-srcwin', str(x_off), str(y_off), str(x_size), str(y_size),
                        input_mrf_path,
                        tile_png
                    ]
                    run_command(extract_cmd, quiet=True)
                    
                    # Check if source is paletted
                    is_paletted = (num_bands == 1 and 
                                   bands[0].get('colorInterpretation') == 'Palette')
                    
                    # Check if source is grayscale with alpha
                    is_grayscale_alpha_tile = (num_bands == 2 and 
                                               bands[0].get('colorInterpretation') == 'Gray' and
                                               bands[1].get('colorInterpretation') == 'Alpha')
                    
                    # For grayscale+alpha, apply alpha mask preprocessing
                    if is_grayscale_alpha_tile:
                        tile_processed = os.path.join(temp_dir, f"tile_processed.tif")
                        apply_alpha_mask_to_grayscale(tile_png, tile_processed)
                        # Use processed tile for conversion
                        tile_png = tile_processed
                    
                    # Convert to JPEG MRF
                    tile_mrf = os.path.join(temp_dir, f"tile.mrf")
                    convert_cmd = [
                        'gdal_translate', '-q'
                    ] + gdal_config
                    
                    if is_paletted:
                        # For paletted images, use -expand rgb to convert to 3-band RGB
                        convert_cmd.extend(['-expand', 'rgb'])
                    elif is_grayscale_alpha_tile:
                        # For grayscale+alpha, we've already applied alpha - just use band 1
                        convert_cmd.extend(['-b', '1'])
                    else:
                        # For non-paletted, add band selection (up to 3 bands for RGB JPEG)
                        for i in range(1, min(4, num_bands + 1)):
                            convert_cmd.extend(['-b', str(i)])
                    
                    convert_cmd.extend([
                        '-of', 'MRF',
                        '-co', 'COMPRESS=JPEG',
                        '-co', f'BLOCKSIZE={x_size}',
                        '-co', f'QUALITY={quality}',
                        tile_png,
                        tile_mrf
                    ])
                    run_command(convert_cmd, quiet=True)
                    
                    # Read JPEG data
                    tile_pjg = tile_mrf.replace('.mrf', '.pjg')
                    with open(tile_pjg, 'rb') as f:
                        jpeg_data = f.read()
                    
                    # For PNG input, we already have JPEG data (with or without JFIF based on MRF metadata)
                    # No need to use cbrunsli - the JFIF option in MRF metadata controls the format
                    tile_size = len(jpeg_data)
                    data_file.write(jpeg_data)
                    idx_file.write(struct.pack('>QQ', current_offset, tile_size))
                    
                    current_offset += tile_size
                    
                    try:
                        os.remove(tile_png)
                        os.remove(tile_mrf)
                        os.remove(tile_pjg)
                        os.remove(tile_mrf.replace('.mrf', '.idx'))
                    except:
                        pass
                    
                    processed += 1
                    if processed % 100 == 0 or processed == total_tiles:
                        print(f"  Processed {processed}/{total_tiles} tiles...")
        
        data_file.close()
        idx_file.close()
        
        print(f"\n4/6: All tiles converted, data file size: {current_offset} bytes")
        
        # Generate overviews if needed (only for PNG input)
        if input_compression == 'PNG' and overview_factors:
            print(f"\n5/6: Generating overviews...")
            factors_str = [str(f) for f in overview_factors]
            
            gdaladdo_cmd = [
                'gdaladdo',
                '-r', 'nearest'
            ] + gdal_config + [
                final_mrf_path
            ] + factors_str
            run_command(gdaladdo_cmd)
        else:
            print(f"\n5/6: No overviews to generate")
        
        print(f"\n6/6: Conversion complete")
        
        print("\n" + "=" * 60)
        print(f"SUCCESS: MRF created at: {final_mrf_path}")
        print("=" * 60)
        
        return True
        
    finally:
        # Clean up temp directory
        if temp_dir and not no_cleanup:
            try:
                shutil.rmtree(temp_dir)
            except:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="MRF conversion tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # PNG to standard JPEG (fast)
  python3 convert_mrf.py png_input.mrf output_dir/
  
  # PNG to brunsli JPEG
  python3 convert_mrf.py png_input.mrf output_dir/ --brunsli
  
  # JPEG to brunsli (auto-detected, lossless)
  python3 convert_mrf.py jpeg_input.mrf output_dir/ --brunsli
  
  # Brunsli to JPEG (auto-detected, lossless)
  python3 convert_mrf.py brunsli_input.mrf output_dir/
  
  # Custom quality
  python3 convert_mrf.py input.mrf output_dir/ --quality 90
  
  # S3 input and output
  python3 convert_mrf.py s3://bucket/path/input.mrf s3://bucket/path/output/
  
  # S3 input, local output
  python3 convert_mrf.py s3://bucket/path/input.mrf /local/output/
  
  # Local input, S3 output
  python3 convert_mrf.py /local/input.mrf s3://bucket/path/output/

Note: JPEG ↔ Brunsli conversions use cbrunsli/dbrunsli for lossless conversions.
S3 paths must be in the format: s3://bucket-name/key/path
        """
    )
    
    parser.add_argument("input_mrf", help="Path to input MRF file (local path or s3://bucket/key)")
    parser.add_argument("output_dir", help="Output directory for converted MRF (local path or s3://bucket/key)")
    parser.add_argument("-q", "--quality", type=int, default=DEFAULT_JPEG_QUALITY,
                       help=f"JPEG quality for PNG→JPEG conversion (default: {DEFAULT_JPEG_QUALITY}, ignored for JPEG input)")
    parser.add_argument("-b", "--brunsli", action="store_true",
                       help="Output brunsli-compressed JPEG (omit for standard JPEG)")
    parser.add_argument("-t", "--temp-dir", default=os.environ.get('TMPDIR', '/tmp'),
                       help="Temporary directory for intermediate files")
    parser.add_argument("-n", "--no-cleanup", action="store_true",
                       help="Don't delete temporary files after conversion")
    parser.add_argument("-s", "--sigevent-url", dest="sigevent_url",
                       help="URL for sigevent monitoring (optional)")
    parser.add_argument("-o", "--output-name", dest="output_name",
                       help="Custom output MRF filename (optional, defaults to input filename)")
    
    args = parser.parse_args()
    
    # Set global sigevent URL if provided
    global SIGEVENT_URL
    SIGEVENT_URL = args.sigevent_url
    
    # Initialize S3 client if needed
    s3_client = None
    if is_s3_path(args.input_mrf) or is_s3_path(args.output_dir):
        try:
            s3_client = boto3.client('s3')
        except Exception as e:
            print(f"Error: Failed to initialize S3 client: {e}")
            sys.exit(1)
    
    # Handle S3 input
    temp_input_dir = None
    input_mrf_path = args.input_mrf
    
    if is_s3_path(args.input_mrf):
        print("Input is S3 path, downloading MRF files...")
        temp_input_dir = tempfile.mkdtemp(prefix='convert_mrf_input_')
        try:
            input_mrf_path = download_mrf_from_s3(args.input_mrf, temp_input_dir, s3_client)
        except Exception as e:
            print(f"Error downloading from S3: {e}")
            if temp_input_dir:
                shutil.rmtree(temp_input_dir, ignore_errors=True)
            sys.exit(1)
    else:
        # Validate local input
        if not os.path.exists(args.input_mrf):
            print(f"Error: Input MRF not found: {args.input_mrf}")
            sys.exit(1)
    
    if args.quality < 1 or args.quality > 100:
        print("Error: Quality must be between 1 and 100")
        sys.exit(1)
    
    # Detect input compression
    print("Detecting input MRF compression...")
    try:
        input_compression = detect_compression(input_mrf_path)
        print(f"Input compression: {input_compression}")
    except Exception as e:
        print(f"Error: {e}")
        if temp_input_dir:
            shutil.rmtree(temp_input_dir, ignore_errors=True)
        sys.exit(1)
    
    # Determine output compression
    if args.brunsli:
        output_compression = 'JPEG_BRUNSLI'
    else:
        output_compression = 'JPEG'
    
    # Validate conversion path
    if input_compression == 'JPEG_BRUNSLI' and args.brunsli:
        print("Note: Input is already brunsli-compressed, ignoring --brunsli flag")
        output_compression = 'JPEG'
    
    if input_compression == 'JPEG' and not args.brunsli:
        print("Error: JPEG to JPEG conversion is not meaningful.")
        print("Use --brunsli flag to convert to brunsli-compressed JPEG.")
        sys.exit(1)
    
    # Handle S3 output - create temp directory for output
    temp_output_dir = None
    output_dir = args.output_dir
    
    if is_s3_path(args.output_dir):
        print("Output is S3 path, using temporary local directory...")
        temp_output_dir = tempfile.mkdtemp(prefix='convert_mrf_output_')
        output_dir = temp_output_dir
    
    # Choose conversion method
    try:
        if input_compression == 'PNG' and output_compression == 'JPEG':
            # Use fast VRT-based method for PNG → standard JPEG
            success = convert_png_to_jpeg_standard(
                input_mrf_path,
                output_dir,
                args.quality,
                args.output_name
            )
        else:
            # Use tile-by-tile method for brunsli conversions and JPEG input
            success = convert_tile_by_tile(
                input_mrf_path,
                output_dir,
                args.temp_dir,
                args.no_cleanup,
                input_compression,
                output_compression,
                args.quality,
                args.output_name
            )
        
        # Upload to S3 if output is S3 path
        if success and is_s3_path(args.output_dir):
            print("\nUploading converted MRF to S3...")
            # Find the output MRF file
            output_basename = args.output_name if args.output_name else os.path.basename(input_mrf_path)
            if not output_basename.endswith('.mrf'):
                output_basename += '.mrf'
            local_output_mrf = os.path.join(output_dir, output_basename)
            
            try:
                upload_mrf_to_s3(local_output_mrf, args.output_dir, s3_client)
            except Exception as e:
                print(f"Error uploading to S3: {e}")
                success = False
        
        # Clean up temp directories
        if temp_input_dir:
            shutil.rmtree(temp_input_dir, ignore_errors=True)
        if temp_output_dir:
            shutil.rmtree(temp_output_dir, ignore_errors=True)
        
        sys.exit(0 if success else 1)
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        
        # Clean up temp directories
        if temp_input_dir:
            shutil.rmtree(temp_input_dir, ignore_errors=True)
        if temp_output_dir:
            shutil.rmtree(temp_output_dir, ignore_errors=True)
        
        sys.exit(1)


if __name__ == "__main__":
    main()
