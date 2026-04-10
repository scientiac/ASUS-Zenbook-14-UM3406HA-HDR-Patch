# ASUS-Zenbook-14-UM3406HA-HDR-Patch
Patches the EDID of Samsung OLED laptop panels (ATNA40CU09-0 / SDC 16797) to enable HDR support in GNOME and KDE on ASUS Zenbook 14 UM3406HA.

## The Problem

GNOME (Mutter) and KDE (KWin) read HDR capability metadata from **CTA-861
extension blocks** in a display's EDID. Samsung OLED laptop panels like the
ATNA40CU09-0 store their HDR metadata inside a **DisplayID 2.0** extension
block instead. The compositors ignore this, so no HDR toggle appears in
display settings even though the panel fully supports HDR.

## The Solution

This script reads the original EDID, extracts the HDR metadata from the
DisplayID 2.0 block, and creates a new **CTA-861 extension block** containing
that same data. The patched EDID is loaded at boot via a kernel parameter,
making the compositor see HDR support.

**The original EDID is never modified.** The patched version only adds a
block; it does not change existing blocks. Reverting is as simple as removing
the kernel parameter and rebooting.

---

## Prerequisites

- Python 3.6+
- `edid-decode` (optional, for validation)
- Root access (for installation steps only — the script itself does not need root)

---

## How to Check Your Display Properties

Before running the patcher, verify your display setup:

### 1. Identify Your DRM Connector

```bash
ls /sys/class/drm/
```

Look for entries like `card1-eDP-1`, `card0-eDP-1`, etc. Internal laptop
panels use `eDP-*` connectors. Note the full name (e.g. `card1-eDP-1`).

### 2. Check Display Status

```bash
cat /sys/class/drm/card1-eDP-1/status
```

Should print `connected`.

### 3. Dump the Current EDID

```bash
sudo cp /sys/class/drm/card1-eDP-1/edid ~/edid-original.bin
```

This copies the raw EDID binary from the kernel's DRM subsystem.

### 4. Check EDID Size

```bash
wc -c < /sys/class/drm/card1-eDP-1/edid
```

- **256 bytes** = 2 blocks (Base EDID + DisplayID 2.0) — this is what we
  expect for the unpatched panel.
- **384 bytes** = 3 blocks — already patched or has a CTA block.

### 5. Decode the EDID

```bash
sudo cat /sys/class/drm/card1-eDP-1/edid | edid-decode
```

Look for these in the output:

| What to look for                     | Where it should appear   | What it means                    |
|--------------------------------------|--------------------------|----------------------------------|
| `DisplayID Extension Block`          | Block 1                  | Panel uses DisplayID 2.0         |
| `HDR Static Metadata Data Block`     | Inside DisplayID block   | Panel advertises HDR capability  |
| `SMPTE ST2084`                       | Inside HDR metadata      | PQ (Perceptual Quantizer) HDR    |
| `BT2020RGB`                          | Colorimetry section      | Wide color gamut support         |
| `CTA-861 Extension Block`            | Block 2 (if patched)     | What GNOME/KDE actually reads    |

If you see `HDR Static Metadata` only inside the DisplayID block and there is
**no** standalone `CTA-861 Extension Block`, this patcher is what you need.

### 6. Check Current HDR Kernel Support

```bash
cat /sys/class/drm/card1-eDP-1/hdr_output_metadata
```

If this file exists, the kernel driver supports HDR output. The issue is that
the compositor cannot read the metadata from the DisplayID block format.

### 7. Check Current Kernel Parameters

```bash
cat /proc/cmdline
```

If `drm.edid_firmware` is already present, an EDID override is already active.

---

## What the Script Checks

The patcher runs a series of validations before producing the patched file.
Here is every check and the logic behind it:

### EDID Header Validation

**Check:** First 8 bytes must be `00 FF FF FF FF FF FF 00`.

**Logic:** This is the EDID magic number defined by the VESA standard. Every
valid EDID starts with this sequence. If it's missing, the file is not an
EDID.

### Block Checksum Validation

**Check:** For each 128-byte block, the sum of all bytes must equal 0 (mod 256).

**Logic:** The last byte of every EDID block is a checksum byte chosen so
that the entire block sums to zero. This is how EDID readers detect
corruption. The script validates every block in the original EDID.

### Panel Identity Verification

**Check:** Manufacturer code = `SDC`, Product code = `16797`.

**Logic:** The manufacturer ID is encoded in bytes 8-9 as compressed ASCII
(3 letters, 5 bits each). The product code is in bytes 10-11 (little-endian).
`SDC` = Samsung Display Corporation. `16797` = the ATNA40CU09-0 panel. The
hardcoded byte offsets in this script are specific to this panel. A different
panel will have its HDR data at different offsets.

### AMD VSDB (Vendor-Specific Data Block) at Offset 0xDA

**Check:**
- CTA tag type (upper 3 bits) = 3 (vendor-specific)
- Length (lower 5 bits) = 19 bytes
- OUI (bytes 1-3) = `00-00-1a` (AMD's IEEE OUI)

**Logic:** This data block contains AMD FreeSync / Adaptive Sync parameters.
The tag byte encodes both the block type and payload length in a single byte
(CTA-861 format). The OUI identifies the vendor — `1a-00-00` is AMD. This
block is copied into the new CTA-861 extension to preserve FreeSync support.

### Colorimetry Data Block at Offset 0xEE

**Check:**
- CTA tag type = 7 (extended tag)
- Length = 3 bytes
- Extended tag code (byte 1) = 5 (colorimetry)
- Bit 7 of payload byte = 1 (BT2020RGB)

**Logic:** Extended tag type 5 is the Colorimetry Data Block (CTA-861-H
Section 7.5.5). It declares which color spaces the display supports. Bit 7
of the first payload byte indicates BT.2020 RGB support, which is required
for HDR wide color gamut.

### HDR Static Metadata Data Block at Offset 0xF2

**Check:**
- CTA tag type = 7 (extended tag)
- Length = 6 bytes
- Extended tag code (byte 1) = 6 (HDR static metadata)

**What it reads:**
- **EOTF byte** (byte 2): Electro-Optical Transfer Function support
  - Bit 0: Traditional gamma (SDR)
  - Bit 2: SMPTE ST2084 (PQ — the HDR transfer function)
  - Bit 3: HLG (Hybrid Log-Gamma)
- **Max luminance** (byte 4): Encoded as `2^(value/32)` cd/m²
- **Avg luminance** (byte 5): Same encoding
- **Min luminance** (byte 6): Encoded value

**Logic:** This is the core HDR capability block (CTA-861-H Section 7.5.13).
GNOME specifically looks for this block in a CTA-861 extension to determine
HDR support. ST2084 (PQ) support is the critical flag for HDR10.

### Patched EDID Checksum Validation

**Check:** After building the patched EDID, every 128-byte block is
checksummed again.

**Logic:** The script recalculates checksums for Block 0 (because the
extension count byte changed) and the new CTA-861 block (Block 2). All three
blocks must pass the sum-to-zero check. Block 1 (DisplayID) is unchanged.

### edid-decode Validation (Optional)

**Check:** If `edid-decode` is installed, the script runs it against the
patched file and verifies that:
- `HDR Static Metadata Data Block` appears in the output
- `SMPTE ST2084` is present
- `BT2020RGB` is present
- `Block 2, CTA-861 Extension Block` is recognized

**Logic:** `edid-decode` is the reference EDID parser. If it can parse the
patched EDID and finds the HDR metadata in the CTA-861 block, the compositor
will too.

---

## Usage

### Step 1: Dump the Original EDID

If the script cannot read sysfs directly (permission issue), dump it first:

```bash
sudo cp /sys/class/drm/card1-eDP-1/edid ./edid-original.bin
```

### Step 2: Run the Patcher

```bash
python3 fix_hdr.py
```

The script will:
1. Read the EDID from `edid-original.bin` (or sysfs)
2. Validate all checksums and the header
3. Confirm the panel is SDC 16797
4. Verify HDR metadata exists at expected offsets
5. Build and write the patched EDID to `edid-patched-hdr.bin`
6. Optionally validate with `edid-decode`

Output file: `edid-patched-hdr.bin` (384 bytes, 3 blocks).

---

## Installation (Requires Root)

These steps install the patched EDID so the kernel loads it at boot.

> **Note:** These instructions assume **Arch Linux** with **systemd-boot**
> and **mkinitcpio**. If you use GRUB, see the GRUB section below.

### Step 1: Copy the Patched EDID to the Firmware Directory

```bash
sudo mkdir -p /lib/firmware/edid
sudo cp edid-patched-hdr.bin /lib/firmware/edid/edp-hdr.bin
```

### Step 2: Add the EDID File to initramfs

Edit `/etc/mkinitcpio.conf` and find the `FILES=()` line. Change it to:

```
FILES=(/lib/firmware/edid/edp-hdr.bin)
```

If the `FILES` line already has entries, append to the list:

```
FILES=(/existing/file /lib/firmware/edid/edp-hdr.bin)
```

This ensures the patched EDID binary is included inside the initramfs image,
making it available to the kernel early in the boot process before the root
filesystem is mounted.

### Step 3: Add the Kernel Parameter

#### For systemd-boot (UKI)

Edit `/etc/kernel/cmdline` and append:

```
drm.edid_firmware=eDP-1:edid/edp-hdr.bin
```

The full line should look something like:

```
root=PARTUUID=xxxx rw rootfstype=btrfs quiet splash drm.edid_firmware=eDP-1:edid/edp-hdr.bin
```

#### For GRUB

Edit `/etc/default/grub` and append to `GRUB_CMDLINE_LINUX_DEFAULT`:

```
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash drm.edid_firmware=eDP-1:edid/edp-hdr.bin"
```

Then regenerate the GRUB config:

```bash
sudo grub-mkconfig -o /boot/grub/grub.cfg
```

### Step 4: Rebuild initramfs / UKI

```bash
sudo mkinitcpio -P
```

This rebuilds all kernel images (including the Unified Kernel Image if using
systemd-boot with UKI presets).

### Step 5: Reboot

```bash
sudo reboot
```

### Step 6: Verify After Reboot

Check that the patched EDID is loaded:

```bash
cat /sys/class/drm/card1-eDP-1/edid | wc -c
```

Expected output: `384` (was `256`).

Decode and confirm the CTA-861 block:

```bash
sudo cat /sys/class/drm/card1-eDP-1/edid | edid-decode
```

Look for `Block 2, CTA-861 Extension Block` containing `HDR Static Metadata`.

Check kernel log for EDID override confirmation:

```bash
dmesg | grep -i edid
```

The HDR toggle should now appear in **Settings > Displays**.

---

## How to Revert

Reverting removes the EDID override and restores the original display
behavior. No data is lost — the original EDID is stored in the display's
hardware and is always used when no override is active.

### Step 1: Remove the Kernel Parameter

#### For systemd-boot (UKI)

Edit `/etc/kernel/cmdline` and remove:

```
drm.edid_firmware=eDP-1:edid/edp-hdr.bin
```

#### For GRUB

Edit `/etc/default/grub` and remove `drm.edid_firmware=eDP-1:edid/edp-hdr.bin`
from `GRUB_CMDLINE_LINUX_DEFAULT`. Then regenerate:

```bash
sudo grub-mkconfig -o /boot/grub/grub.cfg
```

### Step 2: Remove the EDID from initramfs

Edit `/etc/mkinitcpio.conf` and change:

```
FILES=(/lib/firmware/edid/edp-hdr.bin)
```

back to:

```
FILES=()
```

(Or remove just the EDID entry if other files are listed.)

### Step 3: Rebuild initramfs / UKI

```bash
sudo mkinitcpio -P
```

### Step 4: Reboot

```bash
sudo reboot
```

### Step 5: Verify Revert

```bash
cat /sys/class/drm/card1-eDP-1/edid | wc -c
```

Expected output: `256` (original, unpatched).

### Optional: Clean Up Firmware File

```bash
sudo rm /lib/firmware/edid/edp-hdr.bin
```

---

## File Reference

| File                    | Description                                        |
|-------------------------|----------------------------------------------------|
| `fix_hdr.py`            | The EDID patcher script                            |
| `edid-original.bin`     | Original EDID dump from sysfs (created by script)  |
| `edid-patched-hdr.bin`  | Patched EDID with CTA-861 block (output of script) |
| `INFO.md`               | Background research and reference material         |

---

## Panel Compatibility

This script is written for the **SDC 16797 / ATNA40CU09-0** Samsung OLED
panel. The byte offsets for the AMD VSDB, Colorimetry, and HDR Static
Metadata blocks are hardcoded. If you have a different panel:

1. Dump and decode your EDID with `edid-decode`
2. Find the byte offsets of the three data blocks in the DisplayID section
3. Update the offset constants at the top of `fix_hdr.py`

The script will warn you if the detected panel does not match SDC 16797.

---

## References

- [VESA EDID Standard](https://vesa.org/vesa-standards/)
- [CTA-861 Standard](https://www.cta.tech/Resources/Standards)
- [Linux DRM EDID Override](https://docs.kernel.org/gpu/drm-uapi.html)
- [Related GitHub Issue](https://github.com/colorcube/Linux-on-Honor-Magicbook-14-Pro/issues/16)
