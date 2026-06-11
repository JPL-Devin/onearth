#!/usr/bin/env python3
"""
Convert MRF between JPEG and Brunsli compression formats

This script performs lossless conversion between standard JPEG and brunsli-compressed
JPEG formats using the cbrunsli/dbrunsli tools for true lossless compression/decompression.

Supported conversions:
  - JPEG MRF → Brunsli MRF (lossless compression, ~22% size reduction)
  - Brunsli MRF → JPEG MRF (lossless decompression, restores exact original)

Usage:
    python3 convert_mrf_compression.py <input_mrf> <output_dir> [options]

Examples:
    # JPEG to Brunsli (auto-detected)
    python3 convert_mrf_compression.py jpeg_input.mrf output_dir/
    
    # Brunsli to JPEG (auto-detected)
    python3 convert_mrf_compression.py brunsli_input.mrf output_dir/
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

DEFAULT_BLOCK_SIZE = 512


def run_command(command, quiet=False, fail_on_error=True):
    """Run a shell command and return output."""
    if not quiet:
        print(f"Executing: {' '.join(command)}")
    
    result = subprocess.run(command, capture_output=True, text=True, check=fail_on_error)
    
    if result.returncode != 0 and not fail_on_error:
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
    
    if not pjg_file.exists():
        raise ValueError(f"Expected JPEG data file not found: {pjg_file}")
    
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


def create_mrf_xml(mrf_path, size_x, size_y, block_size, quality, use_brunsli=False):
    """Create MRF XML metadata file."""
    data_file = os.path.basename(mrf_path).replace('.mrf', '.pjg')
    idx_file = os.path.basename(mrf_path).replace('.mrf', '.idx')
    
    tiles_x = (size_x + block_size - 1) // block_size
    tiles_y = (size_y + block_size - 1) // block_size
    
    # Build options line - omit for brunsli, include JFIF:on for standard
    options_line = "" if use_brunsli else "    <Options>JFIF:on</Options>\n"
    
    xml_content = f"""<MRF_META>
  <Raster>
    <Size x="{size_x}" y="{size_y}" c="3" />
    <PageSize x="{block_size}" y="{block_size}" c="3" />
    <Compression>JPEG</Compression>
    <Quality>{quality}</Quality>
{options_line}  </Raster>
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


def jpeg_to_brunsli(input_mrf_path, output_dir):
    """Convert JPEG MRF to Brunsli MRF (lossless compression)."""
    print("Converting JPEG MRF to Brunsli MRF (Lossless Compression)")
    print(f"Input:  {input_mrf_path}")
    print(f"Output: {output_dir}")
    print("-" * 60)
    
    # Get input MRF info
    print("\n1/4: Reading input MRF metadata...")
    info = get_mrf_info(input_mrf_path)
    size_x = info['size'][0]
    size_y = info['size'][1]
    
    # Get overview dimensions
    overview_sizes = []
    if 'overviews' in info:
        for ov in info['overviews']:
            overview_sizes.append((ov['size'][0], ov['size'][1]))
    
    print(f"Detected: {size_x}x{size_y}, overviews: {len(overview_sizes)} levels")
    
    # Read source MRF metadata to get quality and blocksize
    with open(input_mrf_path, 'r') as f:
        mrf_content = f.read()
        import re
        quality_match = re.search(r'<Quality>(\d+)</Quality>', mrf_content)
        quality = int(quality_match.group(1)) if quality_match else 80
        blocksize_match = re.search(r'<PageSize x="(\d+)"', mrf_content)
        blocksize = int(blocksize_match.group(1)) if blocksize_match else DEFAULT_BLOCK_SIZE
    
    print(f"Quality: {quality}, Block size: {blocksize}")
    
    # Setup paths
    input_mrf_path_obj = Path(input_mrf_path)
    source_data_path = input_mrf_path_obj.with_suffix('.pjg')
    source_idx_path = input_mrf_path_obj.with_suffix('.idx')
    
    os.makedirs(output_dir, exist_ok=True)
    output_basename = os.path.basename(input_mrf_path)
    final_mrf_path = os.path.join(output_dir, output_basename)
    final_data_path = final_mrf_path.replace('.mrf', '.pjg')
    final_idx_path = final_mrf_path.replace('.mrf', '.idx')
    
    # Copy and modify MRF metadata (remove JFIF:on for brunsli)
    print("\n2/4: Creating output MRF metadata...")
    
    # Read original MRF content
    with open(input_mrf_path, 'r') as f:
        mrf_content = f.read()
    
    # Remove JFIF:on option for brunsli
    import re
    mrf_content = re.sub(r'\s*<Options>JFIF:on</Options>\s*\n?', '', mrf_content)
    
    # Write modified MRF
    with open(final_mrf_path, 'w') as f:
        f.write(mrf_content)
    
    tiles_x = (size_x + blocksize - 1) // blocksize
    tiles_y = (size_y + blocksize - 1) // blocksize
    total_base_tiles = tiles_x * tiles_y
    
    # Get actual number of tiles from source index file
    source_idx_size = os.path.getsize(source_idx_path)
    total_index_entries = source_idx_size // 16  # Each index entry is 16 bytes
    
    print(f"\n3/4: Converting {total_base_tiles} base tiles ({tiles_x}x{tiles_y})...")
    print(f"   Total tiles in source (including overviews): {total_index_entries}")
    print(f"   Pre-allocating index for {total_index_entries} tiles")
    
    # Create temp directory for intermediate files
    temp_dir = tempfile.mkdtemp()
    
    try:
        with open(source_data_path, 'rb') as source_data:
            with open(source_idx_path, 'rb') as source_idx:
                with open(final_data_path, 'wb') as output_data:
                    with open(final_idx_path, 'wb') as output_idx:
                        # Pre-allocate index
                        output_idx.write(b'\x00' * (16 * total_index_entries))
                        output_idx.seek(0)
                        
                        current_offset = 0
                        processed = 0
                        
                        # Process all tiles (base + overviews)
                        for tile_idx in range(total_index_entries):
                            idx_entry = source_idx.read(16)
                            if len(idx_entry) < 16:
                                break  # End of index
                            
                            src_offset, src_size = struct.unpack('>QQ', idx_entry)
                            
                            if src_size == 0:
                                # Empty tile
                                output_idx.write(struct.pack('>QQ', 0, 0))
                                continue
                            
                            # Read JPEG data from source
                            source_data.seek(src_offset)
                            jpeg_data = source_data.read(src_size)
                            
                            if len(jpeg_data) != src_size:
                                raise ValueError(f"Failed to read complete tile data at tile {tile_idx}")
                            
                            # Write JPEG to temp file
                            jpeg_file = os.path.join(temp_dir, f"tile_{tile_idx}.jpg")
                            brunsli_file = os.path.join(temp_dir, f"tile_{tile_idx}.brn")
                            
                            with open(jpeg_file, 'wb') as f:
                                f.write(jpeg_data)
                            
                            # Compress with cbrunsli
                            cbrunsli_cmd = ['cbrunsli', jpeg_file, brunsli_file]
                            run_command(cbrunsli_cmd, quiet=True)
                            
                            # Read brunsli data
                            with open(brunsli_file, 'rb') as f:
                                brunsli_data = f.read()
                            
                            brunsli_size = len(brunsli_data)
                            
                            # Write to output
                            output_data.write(brunsli_data)
                            output_idx.write(struct.pack('>QQ', current_offset, brunsli_size))
                            
                            current_offset += brunsli_size
                            processed += 1
                            
                            # Clean up temp files
                            try:
                                os.remove(jpeg_file)
                                os.remove(brunsli_file)
                            except:
                                pass
                            
                            if processed % 100 == 0 or tile_idx == total_index_entries - 1:
                                print(f"  Processed {processed}/{total_index_entries} tiles...")
        
        print(f"\n4/4: Conversion complete")
        print(f"   Output data file size: {current_offset} bytes")
        print(f"   Compression ratio: {(1 - current_offset / os.path.getsize(source_data_path)) * 100:.1f}% reduction")
        
        print("\n" + "=" * 60)
        print(f"SUCCESS: Brunsli MRF created at: {final_mrf_path}")
        print("=" * 60)
        
        return True
        
    finally:
        # Clean up temp directory
        try:
            shutil.rmtree(temp_dir)
        except:
            pass


def brunsli_to_jpeg(input_mrf_path, output_dir):
    """Convert Brunsli MRF to JPEG MRF (lossless decompression)."""
    print("Converting Brunsli MRF to JPEG MRF (Lossless Decompression)")
    print(f"Input:  {input_mrf_path}")
    print(f"Output: {output_dir}")
    print("-" * 60)
    
    # Get input MRF info
    print("\n1/4: Reading input MRF metadata...")
    info = get_mrf_info(input_mrf_path)
    size_x = info['size'][0]
    size_y = info['size'][1]
    
    # Get overview dimensions
    overview_sizes = []
    if 'overviews' in info:
        for ov in info['overviews']:
            overview_sizes.append((ov['size'][0], ov['size'][1]))
    
    print(f"Detected: {size_x}x{size_y}, overviews: {len(overview_sizes)} levels")
    
    # Read source MRF metadata to get quality and blocksize
    with open(input_mrf_path, 'r') as f:
        mrf_content = f.read()
        import re
        quality_match = re.search(r'<Quality>(\d+)</Quality>', mrf_content)
        quality = int(quality_match.group(1)) if quality_match else 80
        blocksize_match = re.search(r'<PageSize x="(\d+)"', mrf_content)
        blocksize = int(blocksize_match.group(1)) if blocksize_match else DEFAULT_BLOCK_SIZE
    
    print(f"Quality: {quality}, Block size: {blocksize}")
    
    # Setup paths
    input_mrf_path_obj = Path(input_mrf_path)
    source_data_path = input_mrf_path_obj.with_suffix('.pjg')
    source_idx_path = input_mrf_path_obj.with_suffix('.idx')
    
    os.makedirs(output_dir, exist_ok=True)
    output_basename = os.path.basename(input_mrf_path)
    final_mrf_path = os.path.join(output_dir, output_basename)
    final_data_path = final_mrf_path.replace('.mrf', '.pjg')
    final_idx_path = final_mrf_path.replace('.mrf', '.idx')
    
    # Copy and modify MRF metadata (add JFIF:on for standard JPEG)
    print("\n2/4: Creating output MRF metadata...")
    
    # Read original MRF content
    with open(input_mrf_path, 'r') as f:
        mrf_content = f.read()
    
    # Add JFIF:on option if not present (insert before </Raster>)
    import re
    if '<Options>JFIF:on</Options>' not in mrf_content:
        # Insert JFIF:on before </Raster> closing tag
        mrf_content = re.sub(r'(\s*)</Raster>', r'\1  <Options>JFIF:on</Options>\n\1</Raster>', mrf_content)
    
    # Write modified MRF
    with open(final_mrf_path, 'w') as f:
        f.write(mrf_content)
    
    tiles_x = (size_x + blocksize - 1) // blocksize
    tiles_y = (size_y + blocksize - 1) // blocksize
    total_base_tiles = tiles_x * tiles_y
    
    # Get actual number of tiles from source index file
    source_idx_size = os.path.getsize(source_idx_path)
    total_index_entries = source_idx_size // 16  # Each index entry is 16 bytes
    
    print(f"\n3/4: Converting {total_base_tiles} base tiles ({tiles_x}x{tiles_y})...")
    print(f"   Total tiles in source (including overviews): {total_index_entries}")
    print(f"   Pre-allocating index for {total_index_entries} tiles")
    
    # Create temp directory for intermediate files
    temp_dir = tempfile.mkdtemp()
    
    try:
        with open(source_data_path, 'rb') as source_data:
            with open(source_idx_path, 'rb') as source_idx:
                with open(final_data_path, 'wb') as output_data:
                    with open(final_idx_path, 'wb') as output_idx:
                        # Pre-allocate index
                        output_idx.write(b'\x00' * (16 * total_index_entries))
                        output_idx.seek(0)
                        
                        current_offset = 0
                        processed = 0
                        
                        # Process all tiles (base + overviews)
                        for tile_idx in range(total_index_entries):
                            idx_entry = source_idx.read(16)
                            if len(idx_entry) < 16:
                                break  # End of index
                            
                            src_offset, src_size = struct.unpack('>QQ', idx_entry)
                            
                            if src_size == 0:
                                # Empty tile
                                output_idx.write(struct.pack('>QQ', 0, 0))
                                continue
                            
                            # Read brunsli data from source
                            source_data.seek(src_offset)
                            brunsli_data = source_data.read(src_size)
                            
                            if len(brunsli_data) != src_size:
                                raise ValueError(f"Failed to read complete tile data at tile {tile_idx}")
                            
                            # Write brunsli to temp file
                            brunsli_file = os.path.join(temp_dir, f"tile_{tile_idx}.brn")
                            jpeg_file = os.path.join(temp_dir, f"tile_{tile_idx}.jpg")
                            
                            with open(brunsli_file, 'wb') as f:
                                f.write(brunsli_data)
                            
                            # Decompress with dbrunsli
                            dbrunsli_cmd = ['dbrunsli', brunsli_file, jpeg_file]
                            run_command(dbrunsli_cmd, quiet=True)
                            
                            # Read JPEG data
                            with open(jpeg_file, 'rb') as f:
                                jpeg_data = f.read()
                            
                            jpeg_size = len(jpeg_data)
                            
                            # Write to output
                            output_data.write(jpeg_data)
                            output_idx.write(struct.pack('>QQ', current_offset, jpeg_size))
                            
                            current_offset += jpeg_size
                            processed += 1
                            
                            # Clean up temp files
                            try:
                                os.remove(brunsli_file)
                                os.remove(jpeg_file)
                            except:
                                pass
                            
                            if processed % 100 == 0 or tile_idx == total_index_entries - 1:
                                print(f"  Processed {processed}/{total_index_entries} tiles...")
        
        print(f"\n4/4: Conversion complete")
        print(f"   Output data file size: {current_offset} bytes")
        
        print("\n" + "=" * 60)
        print(f"SUCCESS: JPEG MRF created at: {final_mrf_path}")
        print("=" * 60)
        
        return True
        
    finally:
        # Clean up temp directory
        try:
            shutil.rmtree(temp_dir)
        except:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Convert MRF between JPEG and Brunsli compression (lossless)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # JPEG to Brunsli (auto-detected, lossless compression)
  python3 convert_mrf_compression.py jpeg_input.mrf output_dir/
  
  # Brunsli to JPEG (auto-detected, lossless decompression)
  python3 convert_mrf_compression.py brunsli_input.mrf output_dir/

Note: This script uses cbrunsli/dbrunsli for truly lossless conversions.
      The output will be byte-for-byte identical when converting back.
        """
    )
    
    parser.add_argument("input_mrf", help="Path to input MRF file (JPEG or Brunsli)")
    parser.add_argument("output_dir", help="Output directory for converted MRF")
    
    args = parser.parse_args()
    
    # Validate input
    if not os.path.exists(args.input_mrf):
        print(f"Error: Input MRF not found: {args.input_mrf}")
        sys.exit(1)
    
    # Detect compression type
    print("Detecting input MRF compression...")
    try:
        input_compression = detect_compression(args.input_mrf)
        print(f"Input compression: {input_compression}")
    except Exception as e:
        print(f"Error detecting compression: {e}")
        sys.exit(1)
    
    # Perform conversion
    try:
        if input_compression == 'JPEG':
            success = jpeg_to_brunsli(args.input_mrf, args.output_dir)
        elif input_compression == 'JPEG_BRUNSLI':
            success = brunsli_to_jpeg(args.input_mrf, args.output_dir)
        else:
            print(f"Error: Unsupported compression type: {input_compression}")
            sys.exit(1)
        
        sys.exit(0 if success else 1)
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
