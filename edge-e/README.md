# SailFrames Edge Electronics

Hardware designs and firmware for the SailFrames data logger.

Part of [sailframes/core](https://github.com/sailframes/core). See [edge-s](../edge-s) for software.

## Directory Structure

```
edge-e/
├── hardware/          # KiCad PCB designs
│   ├── *.kicad_sch    # Schematic
│   ├── *.kicad_pcb    # PCB layout
│   └── *.kicad_pro    # Project file
└── firmware/          # Microcontroller firmware
    ├── config.txt     # Pi boot configuration
    └── sailframes_e1/ # Arduino sketches
```

## Hardware

The sailframes-e1 board is a custom carrier/interface board for the Raspberry Pi 5.

**Sensors connected via I2C:**
| Device | Address |
|--------|---------|
| LCD Display (PCF8574T) | 0x27 |
| IMU (BNO085) | 0x4A |
| Pressure (DPS310) | 0x77 |

**Other interfaces:**
- GPS (ZED-F9P) via USB
- Camera (Pi Camera 3 Wide) via CSI
- Wind sensor (Calypso) via BLE

## KiCad

Open the project in KiCad 8+:

```bash
cd edge-e/hardware
kicad kicad_sailframes-e1.kicad_pro
```

## Pi Boot Configuration

The `firmware/config.txt` contains required boot settings for I2C and camera:

```ini
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=400000  # Required for BNO085
```

## License

Apache 2.0
