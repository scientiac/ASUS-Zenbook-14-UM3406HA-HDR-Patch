#!/usr/bin/env python3
"""
HDR EDID Patcher for Samsung OLED Panel (ATNA40CU09-0 / SDC 16797)
===================================================================

Problem:
  GNOME/Mutter and KDE/KWin only read HDR metadata from CTA-861
  extension blocks in EDID. This OLED panel stores HDR data inside a
  DisplayID 2.0 extension block, which the compositors ignore.
  Result: no HDR toggle in Settings.

Solution:
  Reads the original EDID, extracts the HDR metadata from the
  DisplayID 2.0 block, and creates a new CTA-861 extension block
  containing that same metadata. Writes the patched EDID to disk.

What this script does:
  1. Reads the original EDID (from sysfs or a saved file)
  2. Validates the EDID header and block checksums
  3. Verifies panel identity (SDC 16797)
  4. Locates HDR metadata at known offsets in the DisplayID block
  5. Builds a new CTA-861 extension block with that metadata
  6. Writes the patched EDID binary to edid-patched-hdr.bin

Usage:
  python3 fix_hdr.py
"""

import os
import sys
import shutil
import subprocess
import hashlib
from pathlib import Path


# ============================================================================
# Configuration
# ============================================================================

DRM_CONNECTOR = "card1-eDP-1"
DRM_CARD = "card1"
EDID_SYSFS_PATH = f"/sys/class/drm/{DRM_CONNECTOR}/edid"

# Offsets of CTA data blocks inside DisplayID 2.0 block (Block 1)
# These are specific to SDC 16797 / ATNA40CU09-0
AMD_VSDB_OFFSET = 0xDA       # 20 bytes: Vendor-Specific Data Block (AMD FreeSync)
AMD_VSDB_SIZE = 20
COLORIMETRY_OFFSET = 0xEE    # 4 bytes: Extended tag 5 (Colorimetry, BT2020RGB)
COLORIMETRY_SIZE = 4
HDR_STATIC_OFFSET = 0xF2     # 7 bytes: Extended tag 6 (HDR Static Metadata)
HDR_STATIC_SIZE = 7

# Output paths
SCRIPT_DIR = Path(__file__).parent.resolve()
ORIGINAL_EDID = SCRIPT_DIR / "edid-original.bin"
PATCHED_EDID = SCRIPT_DIR / "edid-patched-hdr.bin"


# ============================================================================
# Logging — plain text, [module] prefix format
# ============================================================================

def log(module, msg):
    print(f"[{module}] {msg}")


# ============================================================================
# EDID Validation
# ============================================================================

def validate_edid_header(data):
    """
    Check EDID header magic bytes.

    The first 8 bytes of any valid EDID must be: 00 FF FF FF FF FF FF 00.
    This is defined in the VESA EDID standard and identifies the binary
    as an EDID data structure.
    """
    expected = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
    return data[0:8] == expected


def validate_edid_block(data, block_num):
    """
    Validate checksum of a 128-byte EDID block.

    Each 128-byte EDID block must have a checksum such that the sum of
    all 128 bytes in the block equals 0 (mod 256). The last byte of
    each block is the checksum byte.
    """
    start = block_num * 128
    block = data[start:start + 128]
    return sum(block) % 256 == 0


def get_panel_info(data):
    """
    Extract panel identification from EDID Block 0.

    - Bytes 8-9: Manufacturer ID (3-letter code, compressed ASCII)
    - Bytes 10-11: Product code (little-endian 16-bit)
    - Descriptor blocks at offsets 54, 72, 90, 108: may contain panel
      name string (tag 0xFE = alphanumeric data string)
    """
    # Manufacturer ID: 3 letters packed into 2 bytes (5 bits each)
    mfg_raw = (data[8] << 8) | data[9]
    c1 = chr(((mfg_raw >> 10) & 0x1F) + ord('A') - 1)
    c2 = chr(((mfg_raw >> 5) & 0x1F) + ord('A') - 1)
    c3 = chr((mfg_raw & 0x1F) + ord('A') - 1)
    manufacturer = f"{c1}{c2}{c3}"

    # Product code: 16-bit little-endian
    product_code = data[10] | (data[11] << 8)

    # Panel name from descriptor blocks (tag 0xFE)
    panel_name = ""
    for desc_start in [54, 72, 90, 108]:
        if data[desc_start] == 0 and data[desc_start + 1] == 0:
            tag = data[desc_start + 3]
            if tag == 0xFE:
                name_bytes = data[desc_start + 5:desc_start + 18]
                panel_name = bytes(
                    b for b in name_bytes if 32 <= b < 127
                ).decode('ascii', errors='ignore').strip()
                break

    return manufacturer, product_code, panel_name


# ============================================================================
# EDID Reading
# ============================================================================

def dump_live_edid():
    """Read the current EDID binary from sysfs."""
    try:
        with open(EDID_SYSFS_PATH, "rb") as f:
            return bytearray(f.read())
    except PermissionError:
        log("edid", f"Cannot read {EDID_SYSFS_PATH} (permission denied, try sudo)")
        return None
    except FileNotFoundError:
        log("edid", f"Connector not found: {EDID_SYSFS_PATH}")
        return None


def read_edid_source():
    """
    Read EDID from the best available source.

    Priority:
      1. Saved original file (edid-original.bin) in script directory
      2. Live sysfs node (/sys/class/drm/card1-eDP-1/edid)

    If reading from sysfs, a copy is saved as edid-original.bin for
    future runs.
    """
    if ORIGINAL_EDID.exists():
        log("edid", f"Using saved EDID: {ORIGINAL_EDID}")
        with open(ORIGINAL_EDID, "rb") as f:
            return bytearray(f.read()), "file"

    live = dump_live_edid()
    if live and len(live) >= 256:
        log("edid", f"Using live EDID from {EDID_SYSFS_PATH}")
        with open(ORIGINAL_EDID, "wb") as f:
            f.write(live)
        log("edid", f"Saved original EDID to {ORIGINAL_EDID}")
        return live, "sysfs"

    log("error", "No EDID source available.")
    log("error", f"Run: sudo cp {EDID_SYSFS_PATH} {ORIGINAL_EDID}")
    sys.exit(1)


# ============================================================================
# Panel and HDR Verification
# ============================================================================

def verify_panel_identity(data):
    """
    Verify the EDID belongs to the expected panel.

    Checks:
      - Manufacturer code should be "SDC" (Samsung Display Corp.)
      - Product code should be 16797
      - If mismatch, warns that hardcoded offsets may be wrong
    """
    mfg, product, name = get_panel_info(data)
    log("panel", f"Manufacturer: {mfg}, Product: {product}, Name: {name}")

    if mfg != "SDC" or product != 16797:
        log("warn", f"Expected SDC 16797 (ATNA40CU09-0), got {mfg} {product}")
        log("warn", "The hardcoded offsets may not be correct for your panel.")
        resp = input("  Continue anyway? [y/N]: ").strip().lower()
        if resp != 'y':
            sys.exit(1)
    else:
        log("panel", "Panel identity confirmed: SDC 16797 / ATNA40CU09-0")

    return mfg, product, name


def verify_hdr_data_present(data):
    """
    Verify the HDR metadata blocks exist at the expected byte offsets.

    This checks three data blocks inside the DisplayID 2.0 extension:

    1. AMD VSDB (Vendor-Specific Data Block) at 0xDA:
       - CTA tag type should be 3 (vendor-specific)
       - Length should be 19 bytes
       - OUI should be 00-00-1a (AMD)
       - Contains FreeSync / Adaptive Sync parameters

    2. Colorimetry Data Block at 0xEE:
       - CTA tag type should be 7 (extended)
       - Extended tag should be 5 (colorimetry)
       - Bit 7 of payload byte = BT2020RGB support

    3. HDR Static Metadata Data Block at 0xF2:
       - CTA tag type should be 7 (extended)
       - Extended tag should be 6 (HDR static metadata)
       - Contains EOTF support flags, luminance values
       - EOTF byte: bit 0 = SDR, bit 2 = ST2084 (PQ), bit 3 = HLG
    """
    errors = []

    # --- AMD VSDB ---
    vsdb_tag = data[AMD_VSDB_OFFSET]
    vsdb_type = (vsdb_tag >> 5) & 0x07   # upper 3 bits = tag type
    vsdb_len = vsdb_tag & 0x1F           # lower 5 bits = length
    if vsdb_type != 3 or vsdb_len != 19:
        errors.append(f"AMD VSDB at 0x{AMD_VSDB_OFFSET:03X}: unexpected tag 0x{vsdb_tag:02x}")
    else:
        oui = (
            f"{data[AMD_VSDB_OFFSET+3]:02x}-"
            f"{data[AMD_VSDB_OFFSET+2]:02x}-"
            f"{data[AMD_VSDB_OFFSET+1]:02x}"
        )
        if oui != "00-00-1a":
            errors.append(f"AMD VSDB OUI mismatch: {oui} (expected 00-00-1a)")
        else:
            log("check", f"AMD VSDB (FreeSync) at 0x{AMD_VSDB_OFFSET:03X}: OK")

    # --- Colorimetry ---
    col_tag = data[COLORIMETRY_OFFSET]
    col_type = (col_tag >> 5) & 0x07
    col_len = col_tag & 0x1F
    if col_type != 7 or col_len != 3 or data[COLORIMETRY_OFFSET + 1] != 5:
        errors.append(f"Colorimetry at 0x{COLORIMETRY_OFFSET:03X}: unexpected data")
    else:
        bt2020 = "BT2020RGB" if data[COLORIMETRY_OFFSET + 2] & 0x80 else "unknown"
        log("check", f"Colorimetry at 0x{COLORIMETRY_OFFSET:03X}: {bt2020} OK")

    # --- HDR Static Metadata ---
    hdr_tag = data[HDR_STATIC_OFFSET]
    hdr_type = (hdr_tag >> 5) & 0x07
    hdr_len = hdr_tag & 0x1F
    if hdr_type != 7 or hdr_len != 6 or data[HDR_STATIC_OFFSET + 1] != 6:
        errors.append(f"HDR Static Metadata at 0x{HDR_STATIC_OFFSET:03X}: unexpected data")
    else:
        eotf = data[HDR_STATIC_OFFSET + 2]
        max_lum = data[HDR_STATIC_OFFSET + 4]
        avg_lum = data[HDR_STATIC_OFFSET + 5]
        min_lum = data[HDR_STATIC_OFFSET + 6]
        sdr_support = "yes" if eotf & 0x01 else "no"
        pq_support = "yes" if eotf & 0x04 else "no"
        hlg_support = "yes" if eotf & 0x08 else "no"

        log("check", f"HDR Static Metadata at 0x{HDR_STATIC_OFFSET:03X}: OK")
        log("check", f"  EOTF: SDR={sdr_support}, ST2084(PQ)={pq_support}, HLG={hlg_support}")
        log("check", f"  Max luminance:  {max_lum} ({2**(max_lum/32):.1f} cd/m2)")
        log("check", f"  Avg luminance:  {avg_lum} ({2**(avg_lum/32):.1f} cd/m2)")
        log("check", f"  Min luminance:  {min_lum}")

    if errors:
        for e in errors:
            log("error", e)
        log("error", "EDID structure does not match expected layout.")
        log("error", "The hardcoded offsets in this script need adjustment.")
        sys.exit(1)

    return True


# ============================================================================
# Patching
# ============================================================================

def create_patched_edid(original):
    """
    Create a patched EDID by appending a CTA-861 extension block.

    Steps:
      1. Take Block 0 (base EDID) and Block 1 (DisplayID 2.0)
      2. Increment the extension count in Block 0 (byte 0x7E)
      3. Recalculate Block 0 checksum (byte 0x7F)
      4. Extract AMD VSDB, Colorimetry, and HDR Static Metadata
         from their known offsets in Block 1
      5. Build a new 128-byte CTA-861 block:
         - Byte 0: 0x02 (CTA extension tag)
         - Byte 1: 0x03 (CTA-861-G revision 3)
         - Byte 2: DTD offset (where data blocks end)
         - Byte 3: 0x00 (no native DTDs, no special capabilities)
         - Bytes 4+: data block collection (VSDB + Colorimetry + HDR)
         - Byte 127: checksum (makes block sum to 0 mod 256)
      6. Concatenate: Block 0 + Block 1 + CTA Block
    """
    if len(original) < 256:
        log("error", f"EDID too small: {len(original)} bytes (need >= 256)")
        sys.exit(1)

    if len(original) > 256:
        log("warn", f"EDID already has {len(original) // 128} blocks")
        if len(original) >= 384 and original[256] == 0x02:
            log("warn", "A CTA-861 extension block already exists.")
            log("warn", "Your EDID may already be patched.")
            resp = input("  Overwrite and re-patch? [y/N]: ").strip().lower()
            if resp != 'y':
                sys.exit(0)

    block0 = bytearray(original[0:128])
    block1 = bytearray(original[128:256])

    # Update extension count
    old_ext_count = block0[0x7E]
    new_ext_count = old_ext_count + 1
    log("patch", f"Extension count: {old_ext_count} -> {new_ext_count}")
    block0[0x7E] = new_ext_count

    # Recalculate Block 0 checksum
    block0[0x7F] = 0
    block0[0x7F] = (256 - (sum(block0) % 256)) % 256
    log("patch", f"Block 0 checksum recalculated: 0x{block0[0x7F]:02x}")

    # Extract CTA data blocks from DisplayID
    amd_vsdb = original[AMD_VSDB_OFFSET:AMD_VSDB_OFFSET + AMD_VSDB_SIZE]
    colorimetry = original[COLORIMETRY_OFFSET:COLORIMETRY_OFFSET + COLORIMETRY_SIZE]
    hdr_static = original[HDR_STATIC_OFFSET:HDR_STATIC_OFFSET + HDR_STATIC_SIZE]

    log("patch", f"Extracted AMD VSDB: {len(amd_vsdb)} bytes from 0x{AMD_VSDB_OFFSET:03X}")
    log("patch", f"Extracted Colorimetry: {len(colorimetry)} bytes from 0x{COLORIMETRY_OFFSET:03X}")
    log("patch", f"Extracted HDR Static Metadata: {len(hdr_static)} bytes from 0x{HDR_STATIC_OFFSET:03X}")

    # Build CTA-861 extension block
    dbc_size = len(amd_vsdb) + len(colorimetry) + len(hdr_static)  # 31 bytes
    dtd_offset = 4 + dbc_size  # = 35 (0x23)

    cta = bytearray(128)
    cta[0] = 0x02        # CTA extension tag
    cta[1] = 0x03        # Revision 3 (CTA-861-G)
    cta[2] = dtd_offset  # DTD offset (end of data blocks, no DTDs follow)
    cta[3] = 0x00        # No native DTDs, no underscan/audio/YCbCr flags

    pos = 4
    cta[pos:pos + len(amd_vsdb)] = amd_vsdb
    pos += len(amd_vsdb)
    cta[pos:pos + len(colorimetry)] = colorimetry
    pos += len(colorimetry)
    cta[pos:pos + len(hdr_static)] = hdr_static

    # CTA block checksum
    cta[127] = 0
    cta[127] = (256 - (sum(cta) % 256)) % 256
    log("patch", f"CTA-861 block checksum: 0x{cta[127]:02x}")

    # Assemble final EDID
    patched = bytes(block0) + bytes(block1) + bytes(cta)

    # Validate all blocks
    for i in range(len(patched) // 128):
        block_sum = sum(patched[i * 128:(i + 1) * 128]) % 256
        status = "OK" if block_sum == 0 else "INVALID"
        log("validate", f"Block {i} checksum: {status}")

    return patched


# ============================================================================
# Optional validation with edid-decode
# ============================================================================

def validate_with_edid_decode(patched_path):
    """
    If edid-decode is installed, run it against the patched EDID and
    verify the CTA-861 block was parsed correctly.

    Checks:
      - "HDR Static Metadata Data Block" appears in output
      - "SMPTE ST2084" (PQ transfer function) is present
      - "BT2020RGB" (wide color gamut) is present
      - "Block 2, CTA-861 Extension Block" exists
    """
    edid_decode = shutil.which("edid-decode")
    if not edid_decode:
        log("validate", "edid-decode not found, skipping detailed validation")
        return

    log("validate", f"Running: edid-decode {patched_path}")
    result = subprocess.run(
        [edid_decode, str(patched_path)],
        capture_output=True, text=True
    )
    output = result.stdout

    if "HDR Static Metadata Data Block" in output:
        count = output.count("HDR Static Metadata Data Block")
        log("validate", f"HDR Static Metadata found in {count} block(s)")
    else:
        log("error", "HDR Static Metadata not found in patched EDID")
        sys.exit(1)

    if "SMPTE ST2084" in output:
        log("validate", "SMPTE ST2084 (PQ) HDR transfer function: present")

    if "BT2020RGB" in output:
        log("validate", "BT.2020 wide color gamut: present")

    if "Block 2, CTA-861 Extension Block" in output:
        log("validate", "CTA-861 Extension Block (Block 2): present")
    else:
        log("error", "CTA-861 Extension Block not found in patched output")
        sys.exit(1)

    # Print Block 2 section from edid-decode
    lines = output.split('\n')
    in_block2 = False
    log("validate", "--- edid-decode Block 2 output ---")
    for line in lines:
        if "Block 2" in line:
            in_block2 = True
        if in_block2:
            print(f"  {line}")


# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 60)
    print("  HDR EDID Patcher - ATNA40CU09-0 / SDC 16797")
    print("=" * 60)
    print()

    # Step 1: Read EDID
    log("step", "1/6 - Reading EDID source")
    edid_data, source = read_edid_source()
    log("edid", f"EDID size: {len(edid_data)} bytes ({len(edid_data) // 128} blocks)")

    # Step 2: Validate original EDID
    log("step", "2/6 - Validating original EDID")
    if not validate_edid_header(edid_data):
        log("error", "Invalid EDID header (missing magic bytes)")
        sys.exit(1)
    log("validate", "EDID header: valid")

    for i in range(len(edid_data) // 128):
        if not validate_edid_block(edid_data, i):
            log("error", f"Block {i} checksum: invalid")
            sys.exit(1)
    log("validate", "All block checksums: valid")

    # Step 3: Verify panel identity
    log("step", "3/6 - Verifying panel identity")
    mfg, product, name = verify_panel_identity(edid_data)

    # Step 4: Verify HDR metadata at expected offsets
    log("step", "4/6 - Verifying HDR metadata blocks in DisplayID")
    verify_hdr_data_present(edid_data)

    # Step 5: Create patched EDID
    log("step", "5/6 - Creating patched EDID")
    patched = create_patched_edid(edid_data)
    log("patch", f"Patched EDID size: {len(patched)} bytes ({len(patched) // 128} blocks)")

    with open(PATCHED_EDID, "wb") as f:
        f.write(patched)
    log("patch", f"Patched EDID written to: {PATCHED_EDID}")

    # Step 6: Validate with edid-decode (if available)
    log("step", "6/6 - Validating patched EDID")
    validate_with_edid_decode(PATCHED_EDID)

    # Print checksums
    with open(ORIGINAL_EDID, "rb") as f:
        orig_hash = hashlib.sha256(f.read()).hexdigest()[:16]
    patched_hash = hashlib.sha256(patched).hexdigest()[:16]
    log("hash", f"Original:  {ORIGINAL_EDID.name}  sha256:{orig_hash}...")
    log("hash", f"Patched:   {PATCHED_EDID.name}  sha256:{patched_hash}...")

    print()
    print("=" * 60)
    print("  Patching complete. See README.md for installation steps.")
    print("=" * 60)


if __name__ == "__main__":
    main()
