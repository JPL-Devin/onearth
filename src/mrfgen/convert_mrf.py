#!/usr/bin/env python3
"""
MRF Conversion Tool

This script converts MRFs between different compression formats:
  - PNG → JPEG (standard or brunsli-compressed)
  - JPEG → Brunsli (lossless compression, ~22% size reduction)
  - Brunsli → JPEG (lossless decompression, restores exact original)

Conversion methods:
  1. Standard method: Fast bulk conversion using GDAL tools (PNG → standard JPEG only)
  2. Tile-by-tile method: Extracts and converts tiles individually
     - Required for: brunsli output, brunsli input, or JPEG input
     - Uses cbrunsli/dbrunsli for lossless JPEG ↔ Brunsli conversions

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

DEFAULT_JPEG_QUALITY = 80
DEFAULT_BLOCK_SIZE = 512


def run_command(command, quiet=False, fail_on_error=True):
    """Run a shell command and return output."""
    if not quiet:
        print(f"Executing: {' '.join(command)}")
    
    result = subprocess.run(command, capture_output=True, text=True, check=fail_on_error)
    
    if result.returncode != 0:
        if fail_on_error:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return None
    
    return result.stdout


def format_gdal_path(path):
    """Format path for GDAL (handle spaces and special characters)."""
    return path


def get_mrf_info(mrf_path):
    """Get MRF metadata using gdalinfo."""
    gdal_mrf_path = format_gdal_path(mrf_path)
    gdalinfo_command = ['gdalinfo', '--config', 'GDAL_TMPDIR', '/tmp', '-json', gdal_mrf_path]
    
    json_output = run_command(gdalinfo_command)
    return json.loads(json_output)


def detect_compression(mrf_path):
    """Detect the compression type of an MRF."""
    mrf_path_obj = Path(mrf_path)
    
    # Check data file extension
    pjg_file = mrf_path_obj.with_suffix('.pjg')
    ppg_file = mrf_path_obj.with_suffix('.ppg')
    
    if pjg_file.exists():
        # Check if it's brunsli by looking at MRF metadata
        try:
            with open(mrf_path, 'r') as f:
                mrf_content = f.read()
                # Brunsli MRFs don't have JFIF:on in options
                if '<Options>JFIF:on</Options>' in mrf_content:
                    return 'JPEG'
                elif '<Compression>JPEG</Compression>' in mrf_content:
                    return 'JPEG_BRUNSLI'
        except Exception as e:
            raise ValueError(f"Failed to read MRF metadata: {e}")
        return 'JPEG'
    elif ppg_file.exists():
        return 'PNG'
    
    raise ValueError(f"Unknown MRF format: no .pjg or .ppg file found")


def convert_png_to_jpeg_standard(input_mrf, output_dir, quality=DEFAULT_JPEG_QUALITY):
    """
    Convert PNG MRF to standard JPEG using GDAL tools (fast bulk conversion).
    This is the fastest method but only works for PNG → standard JPEG.
    Uses the same approach as convert_png_to_zenjpeg_mrf.py.
    """
    print("Converting PNG MRF to standard JPEG MRF (Fast Method)")
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
    
    # Read blocksize from source MRF (quality comes from parameter for PNG input)
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
    
    output_basename = input_mrf_path.name
    output_mrf = os.path.join(output_dir, output_basename)
    
    # Create VRT from input MRF
    print("\nStep 2/4: Creating VRT from source PNG MRF...")
    vrt_path = output_mrf.replace('.mrf', '_source.vrt')
    
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
    
    # Clean up VRT
    try:
        os.remove(vrt_path)
    except:
        pass
    
    print("\n" + "=" * 60)
    print(f"SUCCESS: Conversion complete!")
    print(f"Output MRF: {output_mrf}")
    print("=" * 60)
    
    return True


def convert_tile_by_tile(input_mrf_path, output_dir, temp_dir_base, no_cleanup, input_compression, output_compression, quality=DEFAULT_JPEG_QUALITY):
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
        
        gdal_input_mrf = format_gdal_path(input_mrf_path)
        gdal_config = ['--config', 'GDAL_TMPDIR', '/tmp']
        
        info = get_mrf_info(input_mrf_path)
        size_x = info['size'][0]
        size_y = info['size'][1]
        
        # Get overview info from bands
        overview_factors = []
        bands = info.get('bands', [])
        if bands and 'overviews' in bands[0]:
            for overview in bands[0]['overviews']:
                factor = round(size_x / overview['size'][0])
                if factor > 1:
                    overview_factors.append(factor)
        
        overview_count = len(overview_factors)
        if overview_factors:
            print(f"Detected: {size_x}x{size_y}, overviews: {overview_factors}")
        else:
            print(f"Detected: {size_x}x{size_y}, no overviews")
        
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
        output_basename = os.path.basename(input_mrf_path)
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
        
        # Always create clean, consistent MRF metadata
        # This ensures all conversion paths produce identical .mrf files
        mrf_output_content = f"""<MRF_META>
  <Raster>
    <Size x="{size_x}" y="{size_y}" c="3" />
    <PageSize x="{blocksize}" y="{blocksize}" c="3" />
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
                        gdal_input_mrf,
                        tile_png
                    ]
                    run_command(extract_cmd, quiet=True)
                    
                    # Convert to JPEG MRF
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
        description="Unified MRF conversion tool",
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

Note: JPEG ↔ Brunsli conversions use cbrunsli/dbrunsli for truly lossless conversions.
        """
    )
    
    parser.add_argument("input_mrf", help="Path to input MRF file")
    parser.add_argument("output_dir", help="Output directory for converted MRF")
    parser.add_argument("--quality", type=int, default=DEFAULT_JPEG_QUALITY,
                       help=f"JPEG quality for PNG→JPEG conversion (default: {DEFAULT_JPEG_QUALITY}, ignored for JPEG input)")
    # Blocksize is automatically read from input MRF
    parser.add_argument("--brunsli", action="store_true",
                       help="Output brunsli-compressed JPEG (omit for standard JPEG)")
    parser.add_argument("--temp-dir", default=os.environ.get('TMPDIR', '/tmp'),
                       help="Temporary directory for intermediate files")
    parser.add_argument("--no-cleanup", action="store_true",
                       help="Don't delete temporary files after conversion")
    
    args = parser.parse_args()
    
    # Validate input
    if not os.path.exists(args.input_mrf):
        print(f"Error: Input MRF not found: {args.input_mrf}")
        sys.exit(1)
    
    if args.quality < 1 or args.quality > 100:
        print("Error: Quality must be between 1 and 100")
        sys.exit(1)
    
    # Detect input compression
    print("Detecting input MRF compression...")
    try:
        input_compression = detect_compression(args.input_mrf)
        print(f"Input compression: {input_compression}")
    except Exception as e:
        print(f"Error: {e}")
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
    
    # Choose conversion method
    try:
        if input_compression == 'PNG' and output_compression == 'JPEG':
            # Use fast VRT-based method for PNG → standard JPEG
            success = convert_png_to_jpeg_standard(
                args.input_mrf,
                args.output_dir,
                args.quality
            )
        else:
            # Use tile-by-tile method for brunsli conversions and JPEG input
            success = convert_tile_by_tile(
                args.input_mrf,
                args.output_dir,
                args.temp_dir,
                args.no_cleanup,
                input_compression,
                output_compression,
                args.quality
            )
        
        sys.exit(0 if success else 1)
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
