# PPK Processing Analysis Report

**Date**: April 7, 2026
**Session**: E1/2026-04-07
**CORS Station**: MAMI (Massachusetts Maritime Academy)

---

## Executive Summary

PPK processing is functional but achieving only **single-point accuracy (~30m)** instead of the target **sub-meter RTK accuracy**. The primary blocker is signal incompatibility between the rover (LG290P tracking L2C) and CORS base station (Leica GR50 tracking L2 P/Y).

| Metric | Current | Target |
|--------|---------|--------|
| Points | 31 | ~2500 (42 min × 1Hz) |
| Fix Rate | 0% | >90% |
| Accuracy | ~30m | <1m |

---

## Issue 1: Why Only 31 Points?

### Observation

The RTCM3 file contains ~11 minutes of data (14:49:12 to ~15:00 UTC), but only 31 epochs produced solutions out of ~660 possible.

### RTKLIB Output Analysis

```
processing : 2026/04/07 14:49:33 Q=0
processing : 2026/04/07 14:49:34 Q=0
...
processing : 2026/04/07 14:56:12 Q=5  ← Single solution (rare)
processing : 2026/04/07 14:56:13 Q=0
...
```

### Quality Codes

| Q Value | Meaning | Accuracy |
|---------|---------|----------|
| 0 | No solution | N/A |
| 1 | Fix (RTK) | 1-3 cm |
| 2 | Float (RTK) | 10-50 cm |
| 5 | Single | 2-30+ m |

### Root Cause

**Q=0 (no solution)** occurs when:
- Insufficient common satellites between rover and base (need ≥4)
- All satellites rejected due to elevation mask, SNR threshold, or missing data
- Observation type mismatch between rover and base RINEX files
- Time synchronization issues

The rover outputs RINEX 3 observation codes (C1C, L1C, C2X, L2X) while CORS uses RINEX 2 codes (C1, L1, P2, L2). Even after converting to RINEX 2.11, RTKLIB may not correctly map all observation types.

---

## Issue 2: Why 30m Accuracy Instead of Sub-Meter?

### The L2 Signal Incompatibility Problem

RTK positioning requires **carrier phase observations** from identical signals on both rover and base.

**CORS Station (Leica GR50):**
| Signal | Code | Carrier Phase | Notes |
|--------|------|---------------|-------|
| L1 C/A | C1 | L1 | ✓ Compatible |
| L2 P(Y) | P2 | L2 | Military signal |

**Rover (Quectel LG290P):**
| Signal | Code | Carrier Phase | Notes |
|--------|------|---------------|-------|
| L1 C/A | C1C | L1C | ✓ Compatible |
| L2C | C2X | L2X | Civilian signal |

### Why This Matters

Both L2 P(Y) and L2C transmit on 1227.60 MHz, but they use **different code modulations**:
- **L2 P(Y)**: Encrypted military signal, survey receivers use semi-codeless tracking
- **L2C**: Open civilian signal with different PRN codes

RTKLIB cannot form double-difference observations between P(Y) and L2C because they are fundamentally different signals. This breaks dual-frequency ambiguity resolution.

### Why L1-Only Processing Also Failed

Changed configuration to `pos1-frequency=l1` (L1 only), but still no RTK fix. Possible reasons:

1. **Carrier phase quality**: LG290P may have noisy or discontinuous phase
2. **Cycle slips**: Frequent loss of phase lock
3. **Ionospheric delay**: Single-frequency cannot cancel ionosphere
4. **Observation alignment**: Subtle timing offsets between rover and base

---

## Issue 3: CORS Hourly File Mapping Bug

### The Bug

NOAA CORS provides hourly observation files with single-letter suffixes.

**Original (incorrect) mapping:**
```python
# Assumed 4-hour blocks
hourly_map = {
    0: 'a', 1: 'a', 2: 'a', 3: 'a',    # 00:00-04:00
    4: 'b', 5: 'b', 6: 'b', 7: 'b',    # 04:00-08:00
    8: 'c', 9: 'c', 10: 'c', 11: 'c',  # 08:00-12:00
    12: 'd', 13: 'd', 14: 'd', 15: 'd', # 12:00-16:00 ← Hour 14 → 'd'
    ...
}
```

**Correct mapping:**
```python
# One letter per hour
hourly_letter = chr(ord('a') + session_hour)  # Hour 14 → 'o'
```

### NOAA CORS Hourly File Convention

| Letter | UTC Hours |
|--------|-----------|
| a | 00:00-01:00 |
| b | 01:00-02:00 |
| ... | ... |
| o | 14:00-15:00 |
| p | 15:00-16:00 |
| ... | ... |
| x | 23:00-24:00 |

### Impact

Session at 14:49 UTC requires `mami097o.26o.gz` (hour 14).

Using `mami097d.26o.gz` (hour 3) meant **zero time overlap** between rover observations (14:49-15:00) and base observations (03:00-04:00), resulting in Q=0 for all epochs.

---

## Issue 4: Missing GPS Ephemeris in RTCM3

### RTCM3 Message Analysis

```
Observation Messages (MSM7):
  1077:   676 messages - GPS MSM7        ✓
  1087:   663 messages - GLONASS MSM7    ✓
  1097:   635 messages - Galileo MSM7    ✓
  1127:   680 messages - BeiDou MSM7     ✓

Navigation/Ephemeris Messages:
  1019:     0 messages - GPS Ephemeris   ✗ MISSING
  1020:     5 messages - GLONASS Ephemeris ✓
  1042:     4 messages - BeiDou Ephemeris  ✓
  1046:    27 messages - Galileo Ephemeris ✓
```

### Impact

Without GPS ephemeris (message 1019), RTKLIB cannot compute GPS satellite positions. Since CORS primarily tracks GPS, this severely limits the available satellites for positioning.

### E1 Firmware Configuration

The firmware sends this command at boot:
```cpp
sendPQTM("PQTMCFGMSGRATE,W,RTCM3-1019,1");  // GPS ephemeris every epoch
```

Possible failure modes:
- LG290P not acknowledging the command
- GPS satellites not broadcasting ephemeris during session
- Firmware not capturing 1019 messages to SD card

### Workaround Applied

Download IGS broadcast navigation file:
```
BRDC00WRD_R_20260970000_01D_MN.rnx.gz
```

This file contains global GPS/GLONASS/Galileo/BeiDou ephemeris for the entire day, eliminating dependence on rover-captured ephemeris.

---

## Issue 5: Wrong RTCM3 File Selection

### The Problem

Multiple RTCM3 files existed for the same date:

| File | Size | Session Start |
|------|------|---------------|
| E1_20260407_144009_raw.rtcm3 | 324 KB | 14:40:09 |
| E1_20260407_144912_raw.rtcm3 | 593 KB | 14:49:12 ← Correct |

The Lambda was selecting the first file alphabetically instead of using the manifest.

### The Fix

Read the RTCM3 S3 key from the session manifest:

```python
if 'sensors' in manifest and 'rtcm3' in manifest['sensors']:
    rtcm3_s3_key = manifest['sensors']['rtcm3'].get('s3_key')
```

Manifest structure:
```json
{
  "sensors": {
    "rtcm3": {
      "s3_key": "raw/E1/2026-04-07/E1_20260407_144912_raw.rtcm3",
      "size_bytes": 592969
    }
  }
}
```

---

## Issue 6: RTKLIB Configuration Errors

### Invalid SNR Mask Format

```
invalid option value pos1-snrmask_r (/opt/rtklib/ppk.conf:8)
invalid option value pos1-snrmask_b (/opt/rtklib/ppk.conf:9)
```

**Original (invalid):**
```
pos1-snrmask_r     =35,35,35
pos1-snrmask_b     =35,35,35
```

**Fixed:**
```
pos1-snrmask_r     =off
pos1-snrmask_b     =off
```

---

## Summary of Fixes Applied

| Issue | Impact | Fix |
|-------|--------|-----|
| Wrong CORS hourly file (d vs o) | No time overlap → Q=0 | Fixed hour-to-letter mapping |
| Missing GPS ephemeris (1019) | No GPS satellite positions | Download IGS broadcast nav |
| Wrong RTCM3 file selected | Processing wrong session | Read S3 key from manifest |
| L2 P(Y) vs L2C incompatibility | No dual-freq RTK | Changed to L1-only |
| Invalid SNR mask format | Config parse error | Set to 'off' |
| RINEX 3 vs RINEX 2 codes | Observation mismatch | Output RINEX 2.11 |

---

## Files Modified

| File | Changes |
|------|---------|
| `lambda/ppk_process/handler.py` | RTCM3 from manifest, obs header logging, navigation file handling |
| `lambda/ppk_process/ppk.conf` | L1-only, SNR mask off, GLONASS AR on |
| `lambda/cors_download/handler.py` | Correct hourly letters (a-x), IGS nav download |

---

## Recommendations for Sub-Meter Accuracy

### Option 1: Find L2C-Capable CORS Station

Some newer NOAA CORS stations track L2C. Check the CORS map:
https://geodesy.noaa.gov/CORS_Map/

Look for stations with receivers like:
- Trimble NetR9
- Septentrio PolaRx5
- Leica GR30/GR50 with recent firmware

### Option 2: Fix GPS Ephemeris in E1 Firmware

Debug the LG290P configuration:

1. Verify PQTM command responses:
```cpp
void sendPQTM(const char* cmd) {
    Serial2.println(cmd);
    // Log response to verify ACK
    delay(100);
    while (Serial2.available()) {
        Serial.write(Serial2.read());
    }
}
```

2. Check if 1019 messages appear in serial monitor during recording

3. Verify RTCM3 file contains 1019 after recording:
```python
# Parse and count message types
python3 analyze_rtcm3.py E1_*.rtcm3
```

### Option 3: Use PPP Instead of PPK

Precise Point Positioning (PPP) doesn't require a base station but needs:
- Precise ephemeris (IGS final products, 2-week delay)
- Longer convergence time (10-30 minutes)
- Lower accuracy than RTK (~10cm vs ~2cm)

Configure in ppk.conf:
```
pos1-posmode       =ppp-kine
pos1-sateph        =precise
```

### Option 4: Try Different PPK Software

- **RTKExplorer's RTKLIB fork**: Better handling of mixed observation codes
- **CSRS-PPP (NRCan)**: Free online PPP service
- **Trimble RTX**: Commercial, works with consumer receivers

---

## Appendix: RTKLIB Configuration Reference

Current `ppk.conf` settings:

```ini
pos1-posmode       =kinematic
pos1-frequency     =l1           # L1 only (L2 incompatible)
pos1-soltype       =forward
pos1-elmask        =15           # 15° elevation mask
pos1-snrmask_r     =off
pos1-snrmask_b     =off
pos1-dynamics      =on
pos1-ionoopt       =brdc         # Broadcast ionosphere model
pos1-tropopt       =saas         # Saastamoinen troposphere
pos1-sateph        =brdc         # Broadcast ephemeris
pos1-navsys        =15           # GPS+GLO+GAL+BDS (1+2+4+8)

pos2-armode        =continuous   # Continuous ambiguity resolution
pos2-gloarmode     =on           # GLONASS AR enabled
pos2-arthres       =3.0          # AR validation threshold

out-solformat      =llh
out-outsingle      =on           # Output single solutions
out-outstat        =residual     # Output residuals for debugging
```

---

## Appendix: Useful Commands

### Check RTCM3 Message Types
```bash
python3 << 'EOF'
import struct

def parse_rtcm3(filepath):
    msg_counts = {}
    with open(filepath, 'rb') as f:
        data = f.read()
    i = 0
    while i < len(data) - 6:
        if data[i] != 0xD3:
            i += 1
            continue
        msg_len = ((data[i+1] & 0x03) << 8) | data[i+2]
        if msg_len > 1023 or i + msg_len + 6 > len(data):
            i += 1
            continue
        msg_type = (data[i+3] << 4) | (data[i+4] >> 4)
        msg_counts[msg_type] = msg_counts.get(msg_type, 0) + 1
        i += msg_len + 6
    return msg_counts

for mt, count in sorted(parse_rtcm3('/path/to/file.rtcm3').items()):
    print(f"{mt}: {count}")
EOF
```

### List CORS Files for a Day
```bash
curl -s "https://geodesy.noaa.gov/corsdata/rinex/2026/097/mami/" | \
    grep -o 'href="[^"]*\.gz"' | sed 's/href="//;s/"$//'
```

### Download IGS Broadcast Navigation
```bash
curl -O "https://igs.bkg.bund.de/root_ftp/IGS/BRDC/2026/097/BRDC00WRD_R_20260970000_01D_MN.rnx.gz"
```

### Invoke PPK Lambda Manually
```bash
aws lambda invoke --profile sailframes --region us-east-1 \
    --function-name sailframes-ppk-process \
    --cli-binary-format raw-in-base64-out \
    --payload '{"device_id":"E1","folder":"2026-04-07","date":"2026-04-07","cors_station":"mami"}' \
    /tmp/result.json
```

---

*Last updated: April 7, 2026*
