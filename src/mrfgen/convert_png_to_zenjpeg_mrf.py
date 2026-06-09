#!/usr/bin/env python3
"""
Convert PNG MRF to ZenJPEG MRF

This script converts an existing PNG-compressed MRF to a ZenJPEG (JPEG with optional Brunsli)-compressed MRF.
It supports two conversion methods:
  1. Standard method: Uses GDAL tools to perform bulk conversion
  2. Brunsli method: Extracts and converts tiles individually, manually constructing the MRF structure
     (required for brunsli-compressed ZenJPEG)

Usage:
    python3 convert_png_to_zenjpeg_mrf.py <input_png_mrf> <output_dir> [options]

Examples:
    # Standard conversion
    python3 convert_png_to_zenjpeg_mrf.py input.mrf output_dir/
    
    # Brunsli-compressed ZenJPEG
    python3 convert_png_to_zenjpeg_mrf.py input.mrf output_dir/ --brunsli
    
    # Custom quality
    python3 convert_png_to_zenjpeg_mrf.py input.mrf output_dir/ --quality 90
"""

import argparse
import os
import subprocess
import sys
import tempfile
import shutil
import struct
import json
from pathlib import Path

DEFAULT_JPEG_QUALITY = 80
DEFAULT_BLOCK_SIZE = 512


def run_command(command, fail_on_error=True, quiet=False):
    """Execute shell command."""
    if not quiet:
        print(f"Executing: {' '.join(command)}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=fail_on_error)
        if result.stderr and result.stderr.strip() and not quiet:
            print(f"STDERR: {result.stderr.strip()}")
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if not quiet:
            print(f"ERROR: Command failed: {e.stderr}")
        if fail_on_error:
            raise
        return None


def format_gdal_path(input_path):
    """Format path for GDAL (handle S3 paths)."""
    if input_path.lower().startswith('s3://'):
        return '/vsis3/' + input_path[5:]
    return input_path


def get_mrf_info(mrf_path):
    """Get information about the source MRF using local gdalinfo."""
    gdal_mrf_path = format_gdal_path(mrf_path)
    gdalinfo_command = ['gdalinfo', '--config', 'GDAL_TMPDIR', '/tmp', '-json', gdal_mrf_path]
    
    json_output = run_command(gdalinfo_command)
    return json.loads(json_output)


def convert_standard_method(input_mrf, output_dir, quality, blocksize):
    """
    Convert using standard GDAL tools (VRT + gdal_translate).
    This method works for standard ZenJPEG but not brunsli-compressed.
    """
    input_mrf = Path(input_mrf).resolve()
    output_dir = Path(output_dir).resolve()
    
    if not input_mrf.exists():
        print(f"Error: Input MRF does not exist: {input_mrf}")
        sys.exit(1)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Converting PNG MRF to ZenJPEG MRF (Standard Method)")
    print(f"Input:  {input_mrf}")
    print(f"Output: {output_dir}")
    print("-" * 60)
    
    print("Step 1/4: Reading source MRF metadata...")
    mrf_info = get_mrf_info(str(input_mrf))
    
    input_mrf_path = str(input_mrf)
    output_dir_path = str(output_dir)
    
    size_x = mrf_info['size'][0]
    size_y = mrf_info['size'][1]
    bands = mrf_info['bands']
    
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
    
    output_basename = input_mrf.stem
    output_mrf_path = f"{output_dir_path}/{output_basename}.mrf"
    
    print("\nStep 2/4: Creating VRT from source PNG MRF...")
    vrt_path = output_dir / f"{output_basename}_source.vrt"
    vrt_path_str = str(vrt_path)
    
    run_command(["gdalbuildvrt", vrt_path_str, input_mrf_path])
    
    print(f"  VRT created: {vrt_path}")
    
    print("\nStep 3/4: Converting to ZenJPEG MRF...")
    
    gdal_cmd = [
        "gdal_translate",
        "-of", "MRF",
        "-co", "COMPRESS=JPEG",
        "-co", f"BLOCKSIZE={blocksize}",
        "-co", f"QUALITY={quality}",
        "-co", "OPTIONS=JFIF:on"
    ]
    
    for i in range(1, min(4, len(bands) + 1)):
        gdal_cmd.extend(["-b", str(i)])
    
    gdal_cmd.extend([
        vrt_path_str,
        output_mrf_path
    ])
    
    result = subprocess.run(gdal_cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error during conversion: {result.stderr}")
        sys.exit(1)
    
    print(f"  ZenJPEG MRF created")
    
    print("\nStep 4/4: Building overviews...")
    
    if source_overviews:
        overview_levels = [str(level) for level in source_overviews]
        print(f"  Using source MRF overview levels: {' '.join(overview_levels)}")
        
        gdaladdo_cmd = [
            "gdaladdo",
            "-r", "nearest",
            output_mrf_path
        ] + overview_levels
        
        run_command(gdaladdo_cmd)
        
        print(f"  Overviews created: {' '.join(overview_levels)}")
    else:
        print("  Source MRF has no overviews - skipping overview creation")
    
    if vrt_path.exists():
        vrt_path.unlink()
    
    print("\n" + "=" * 60)
    print("SUCCESS: Conversion complete!")
    print(f"Output MRF: {output_dir / output_basename}.mrf")
    print("=" * 60)
    
    print("\nVerifying output...")
    verify_cmd = ["gdalinfo", output_mrf_path]
    
    result = subprocess.run(verify_cmd, capture_output=True, text=True)
    
    if result.returncode == 0:
        print("\n✓ Output MRF is valid and readable")
    else:
        print("\n✗ Warning: Output MRF verification failed")
        return False
    
    return True


def create_empty_mrf_xml(mrf_path, size_x, size_y, block_size, quality):
    """Create MRF XML metadata file."""
    data_file = os.path.basename(mrf_path).replace('.mrf', '.pjg')
    idx_file = os.path.basename(mrf_path).replace('.mrf', '.idx')
    
    tiles_x = (size_x + block_size - 1) // block_size
    tiles_y = (size_y + block_size - 1) // block_size
    
    xml_content = f"""<MRF_META>
  <Raster>
    <Size x="{size_x}" y="{size_y}" c="3" />
    <PageSize x="{block_size}" y="{block_size}" c="3" />
    <Compression>JPEG</Compression>
    <Quality>{quality}</Quality>
    <Options>JFIF:on</Options>
  </Raster>
  <Rsets model="uniform" scale="2" />
  <GeoTags>
    <BoundingBox minx="-180.0" miny="-90.0" maxx="180.0" maxy="90.0" />
  </GeoTags>
  <DataFile>{data_file}</DataFile>
  <IndexFile>{idx_file}</IndexFile>
</MRF_META>
"""
    
    with open(mrf_path, 'w') as f:
        f.write(xml_content)
    
    return tiles_x, tiles_y


def convert_brunsli_method(input_mrf_path, output_dir, quality, blocksize, temp_dir_base, no_cleanup):
    """
    Convert by extracting tiles individually and manually constructing the MRF.
    This method is required for brunsli-compressed ZenJPEG.
    """
    temp_dir = None
    
    try:
        print("Converting PNG MRF to ZenJPEG MRF (Brunsli Method)")
        print(f"Input:  {input_mrf_path}")
        print(f"Output: {output_dir}")
        print("-" * 60)
        
        print("\n1/6: Extracting metadata from input MRF...")
        
        gdal_mrf_path = format_gdal_path(input_mrf_path)
        gdalinfo_command = ['gdalinfo', '--config', 'GDAL_TMPDIR', '/tmp', '-json', gdal_mrf_path]
        
        json_output = run_command(gdalinfo_command)
        info = json.loads(json_output)
        
        size_x = info.get('size', [0, 0])[0]
        size_y = info.get('size', [0, 0])[1]
        
        if size_x == 0 or size_y == 0:
            raise ValueError(f"Could not determine valid size from {input_mrf_path}")
        
        overviews_bands = info.get('bands', [])
        overview_list = overviews_bands[0].get('overviews', []) if overviews_bands else []
        
        overview_factors = []
        current_factor = 1
        if overview_list:
            for overview in overview_list:
                factor = round(size_x / overview['size'][0])
                if factor > current_factor:
                    overview_factors.append(factor)
                    current_factor = factor
            print(f"Detected: {size_x}x{size_y}, overviews: {overview_factors}")
        else:
            print(f"Detected: {size_x}x{size_y}, no overviews")
        
        base_name = os.path.splitext(os.path.basename(input_mrf_path))[0]
        if base_name.lower().endswith('.mrf'):
            base_name = base_name[:-4]
        
        output_dir = output_dir.rstrip('/')
        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        
        final_mrf_path = f"{output_dir}/{base_name}.mrf"
        final_idx_path = f"{output_dir}/{base_name}.idx"
        final_data_path = f"{output_dir}/{base_name}.pjg"
        
        gdal_config = ['--config', 'GDAL_TMPDIR', '/tmp']
        gdal_input_mrf = format_gdal_path(input_mrf_path)
        
        temp_dir = tempfile.mkdtemp(dir=temp_dir_base)
        print(f"Using temp directory: {temp_dir}")
        
        print("\n2/6: Creating MRF metadata...")
        tiles_x, tiles_y = create_empty_mrf_xml(final_mrf_path, size_x, size_y, blocksize, quality)
        total_tiles = tiles_x * tiles_y
        
        print(f"\n3/6: Converting {total_tiles} tiles ({tiles_x}x{tiles_y})...")
        
        total_index_entries = total_tiles
        if overview_factors:
            for factor in overview_factors:
                ov_tiles_x = (tiles_x + factor - 1) // factor
                ov_tiles_y = (tiles_y + factor - 1) // factor
                total_index_entries += ov_tiles_x * ov_tiles_y
        
        print(f"   Pre-allocating index for {total_index_entries} total tiles (including overviews)")
        
        data_file = open(final_data_path, 'wb')
        idx_file = open(final_idx_path, 'wb')
        
        idx_file.write(b'\x00' * (16 * total_index_entries))
        idx_file.seek(0)
        
        current_offset = 0
        processed = 0
        
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                x_off = tx * blocksize
                y_off = ty * blocksize
                x_size = min(blocksize, size_x - x_off)
                y_size = min(blocksize, size_y - y_off)
                
                tile_png = os.path.join(temp_dir, f"tile.png")
                extract_cmd = [
                    'gdal_translate', '-q'
                ] + gdal_config + [
                    '-of', 'PNG',
                    '-srcwin', str(x_off), str(y_off), str(x_size), str(y_size),
                    gdal_input_mrf,
                    tile_png
                ]
                run_command(extract_cmd, quiet=True)
                
                tile_mrf = os.path.join(temp_dir, f"tile.mrf")
                convert_cmd = [
                    'gdal_translate', '-q'
                ] + gdal_config + [
                    '-b', '1', '-b', '2', '-b', '3',
                    '-of', 'MRF',
                    '-co', 'COMPRESS=JPEG',
                    '-co', f'BLOCKSIZE={x_size}',
                    '-co', f'QUALITY={quality}',
                    tile_png,
                    tile_mrf
                ]
                run_command(convert_cmd, quiet=True)
                
                tile_pjg = tile_mrf.replace('.mrf', '.pjg')
                with open(tile_pjg, 'rb') as f:
                    jpeg_data = f.read()
                
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
        
        if overview_factors:
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
            print("\n5/6: No overviews to generate")
        
        print("\n6/6: Conversion complete")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    finally:
        if temp_dir:
            if no_cleanup:
                print(f"Temp directory preserved: {temp_dir}")
            else:
                try:
                    shutil.rmtree(temp_dir)
                except Exception as e:
                    print(f"WARNING: Cleanup failed: {e}")
    
    print("\n" + "=" * 60)
    print(f"SUCCESS: ZenJPEG MRF created at: {final_mrf_path}")
    print("=" * 60)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Convert PNG MRF to ZenJPEG MRF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Standard conversion (faster, but doesn't support brunsli)
  python3 convert_png_to_zenjpeg_mrf.py input.mrf output_dir/
  
  # Brunsli-compressed ZenJPEG
  python3 convert_png_to_zenjpeg_mrf.py input.mrf output_dir/ --brunsli
  
  # Custom quality with brunsli
  python3 convert_png_to_zenjpeg_mrf.py input.mrf output_dir/ --brunsli --quality 90
        """
    )
    
    parser.add_argument("input_mrf", help="Path to input PNG MRF file")
    parser.add_argument("output_dir", help="Output directory for ZenJPEG MRF")
    parser.add_argument("--quality", type=int, default=DEFAULT_JPEG_QUALITY, 
                       help=f"JPEG quality (1-100, default: {DEFAULT_JPEG_QUALITY})")
    parser.add_argument("--blocksize", type=int, default=DEFAULT_BLOCK_SIZE,
                       help=f"Tile block size (default: {DEFAULT_BLOCK_SIZE})")
    parser.add_argument("--brunsli", action="store_true",
                       help="Use brunsli-compressed ZenJPEG (tile-by-tile conversion method)")
    parser.add_argument("--temp-dir", default=os.environ.get('TMPDIR', '/tmp'),
                       help="Temp directory (only for brunsli method)")
    parser.add_argument("--no-cleanup", action="store_true",
                       help="Keep temp files (only for brunsli method)")
    
    args = parser.parse_args()
    
    if not 1 <= args.quality <= 100:
        print("Error: Quality must be between 1 and 100")
        sys.exit(1)
    
    if args.brunsli:
        success = convert_brunsli_method(
            args.input_mrf,
            args.output_dir,
            args.quality,
            args.blocksize,
            args.temp_dir,
            args.no_cleanup
        )
    else:
        if args.no_cleanup or args.temp_dir != os.environ.get('TMPDIR', '/tmp'):
            print("Warning: --no-cleanup and --temp-dir are only used with --brunsli method")
        
        success = convert_standard_method(
            args.input_mrf,
            args.output_dir,
            args.quality,
            args.blocksize
        )
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
