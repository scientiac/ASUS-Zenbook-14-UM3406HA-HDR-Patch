Solution: Patch EDID to add CTA-861 HDR block

I have the same panel (SDC 16797 / ATNA40CU09-0) on an ASUS Vivobook S 14 with AMD Ryzen AI 9 365. Got HDR working on GNOME 49.

The issue is that GNOME (Mutter) and KDE (KWin) can't read HDR metadata from DisplayID 2.0 extension blocks, they only parse CTA-861 blocks. Our panel has the HDR Static Metadata in DisplayID 2.0 format, so the compositors ignore it even though the kernel exposes HDR_OUTPUT_METADATA.

fix: Create a patched EDID that adds a CTA-861 extension block containing the same HDR data.

1. Dump your original EDID:

sudo cp /sys/class/drm/card1-eDP-1/edid ~/edid-original.bin

2. Create the patched EDID:

Save this as patch_edid.py and run it:

#!/usr/bin/env python3
with open("edid-original.bin", "rb") as f:
    original = bytearray(f.read())

block0 = bytearray(original[0:128])
block1 = bytearray(original[128:256])

# Update extension count 1 -> 2
block0[0x7E] = 0x02

# Recalculate Block 0 checksum
block0[0x7F] = 0
block0[0x7F] = (256 - (sum(block0) % 256)) % 256

# Create CTA-861 block with HDR data copied from DisplayID block
cta = bytearray(128)
cta[0:4] = [0x02, 0x03, 0x23, 0x00]  # CTA header
cta[4:24] = original[0xDA:0xDA+20]   # AMD VSDB (FreeSync)
cta[24:28] = original[0xEE:0xEE+4]   # Colorimetry (BT2020RGB)
cta[28:35] = original[0xF2:0xF2+7]   # HDR Static Metadata

# CTA checksum
cta[127] = (256 - (sum(cta) % 256)) % 256

patched = bytes(block0) + bytes(block1) + bytes(cta)
with open("edid-patched-hdr.bin", "wb") as f:
    f.write(patched)

print(f"Patched EDID saved: {len(patched)} bytes")

cd ~
python3 patch_edid.py

3. Install the patched EDID:

sudo mkdir -p /lib/firmware/edid
sudo cp ~/edid-patched-hdr.bin /lib/firmware/edid/edp-hdr.bin

4. Add to initramfs:

Edit /etc/mkinitcpio.conf:

FILES=(/lib/firmware/edid/edp-hdr.bin)

Rebuild:

sudo mkinitcpio -P

5. Add kernel parameter:

Edit /etc/default/grub:

GRUB_CMDLINE_LINUX_DEFAULT="drm.edid_firmware=eDP-1:edid/edp-hdr.bin loglevel=0 quiet splash"

sudo grub-mkconfig -o /boot/grub/grub.cfg

6. Reboot and verify:

cat /sys/class/drm/card1-eDP-1/edid | wc -c  # Should show 384

HDR toggle should now appear in Settings → Displays.

Note: The Python script offsets (0xDA, 0xEE, 0xF2) work for SDC 16797 panels. If you have a different panel, you may need to adjust based on your edid-decode output to find where the AMD VSDB, Colorimetry, and HDR Static Metadata blocks are located.


another:

Currently GNOME 49 and maybe KDE can't get info about HDR support info from our OLED display. Its only can do that from legacy EDID block that not present in our DisplayID info:

Block 1, DisplayID Extension Block:
  Version: 2.0
  Extension Count: 0
  Display Product Primary Use Case: None of the listed primary use cases; generic display
  Product Identification Data Block (0x20), OUI 94-0B-D5:
    Product Code: 17666
    Year of Manufacture: 2024, Week 1
  Display Parameters Data Block (0x21):
    Image size: 307.5 mm x 204.5 mm
    Display native pixel format: 3120x2080
    Scan Orientation: Left to Right, Top to Bottom
    Luminance Information: Minimum guaranteed value
    Color Information: CIE 1931
    Audio Speaker Information: not integrated
    Native Color Chromaticity:
      Primary #1:  (0.683838, 0.315918)
      Primary #2:  (0.243896, 0.726807)
      Primary #3:  (0.138916, 0.041992)
      White Point: (0.312988, 0.328857)
    Native Maximum Luminance (Full Coverage): 700.000000 cd/m^2
    Native Maximum Luminance (10% Rectangular Coverage): 700.000000 cd/m^2
    Native Minimum Luminance: 2.000000 cd/m^2
    Native Color Depth: 12 bpc
    Display Device Technology: Organic LED
    Native Gamma EOTF: 2.20
  Display Interface Features Data Block:
    Supported bpc for RGB encoding: 6, 8, 10
    Supported color space and EOTF standard combination 1: BT.2020/SMPTE ST 2084
  Video Timing Modes Type 7 - Detailed Timings Data Block:
    DTD:  3120x2080  120.000000 Hz   1:1    264.960 kHz    900.864000 MHz (aspect 1:1, no 3D stereo, preferred)
               Hfront  212 Hsync   4 Hback   64 Hpol N
               Vfront  122 Vsync   1 Vback    5 Vpol N
    DTD:  3120x2080   60.000000 Hz   1:1    264.960 kHz    900.864000 MHz (aspect 1:1, no 3D stereo)
               Hfront  212 Hsync   4 Hback   64 Hpol N
               Vfront 2330 Vsync   1 Vback    5 Vpol N
  CTA-861 DisplayID Data Block:
  Colorimetry Data Block:
    BT2020RGB
  HDR Static Metadata Data Block:
    Electro optical transfer functions:
      Traditional gamma - SDR luminance range
      SMPTE ST2084
    Supported static metadata descriptors:
      Static metadata type 1
    Desired content max luminance: 122 (702.501 cd/m^2)
    Desired content max frame-average luminance: 122 (702.501 cd/m^2)
    Desired content min luminance: 7 (0.005 cd/m^2)
  Checksum: 0x9d
Checksum: 0x90

So, to access HDR settings in GNOME we must add EDID block to DisplayID. I add one, but use it with caution, cause DTD in EDID can't store our display modes correctly. On my system there is no any issue with incorrect EDID block.

To use patched DisplayID place edp-hdr.bin in /lib/firmware/edid/ in initramfs.
On Manjaro do following:

    place edp-hdr.zip to /lib/firmware/edid/
    change FILES=() in /etc/mkinitcpio.conf to FILES=(/lib/firmware/edid/edp-hdr.bin)
    add drm.edid_firmware=eDP-1:edid/edp-hdr.bin to GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub
    run mkinitcpio -P and update-grub
    reboot

Interest think about 60Hz, just look on it description:

    DTD:  3120x2080   60.000000 Hz   1:1    264.960 kHz    900.864000 MHz (aspect 1:1, no 3D stereo)
               Hfront  212 Hsync   4 Hback   64 Hpol N
               Vfront 2330 Vsync   1 Vback    5 Vpol N

Vfront is huge! This because on 60Hz pixel refresh rate is the same as on 120Hz but every odd frame just skiped (pixels stay black) because of Vfront. Look: 2330=122+2080+1+5+122.

So display just skip whole frame Vpol+Vfront+Vactive+Vback and again Vfront for normal frame.


https://bbs.archlinux.org/viewtopic.php?id=303434
https://github.com/colorcube/Linux-on-Honor-Magicbook-14-Pro/issues/16
