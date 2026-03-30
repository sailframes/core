# SailFrames Edge-E

ESP32-based edge device for sailboat racing data acquisition.

Part of [sailframes/core](https://github.com/sailframes/core). See [edge-s](../edge-s) for Raspberry Pi-based device.

## Variants

- **E1** - First generation ESP32 device (current)

## Hardware (E1)

| Component | Part | Interface |
|-----------|------|-----------|
| MCU | ESP32 | — |
| GPS | u-blox | UART |
| IMU | ICM-20948 or BNO085 | I2C |
| Storage | MicroSD | SPI |

## Directory Structure

```
edge-e/
├── hardware/          # KiCad PCB designs
│   ├── *.kicad_sch    # Schematic
│   ├── *.kicad_pcb    # PCB layout
│   └── *.kicad_pro    # Project file
└── firmware/          # ESP32/Arduino firmware
    └── sailframes_e1/ # E1 device firmware
```

## KiCad

Open the project in KiCad 8+:

```bash
cd edge-e/hardware
kicad kicad_sailframes-e1.kicad_pro
```

## Firmware

Build and flash using Arduino IDE or PlatformIO:

```bash
cd edge-e/firmware/sailframes_e1
# Open in Arduino IDE or use PlatformIO
```

## Edge-S vs Edge-E

| Feature | Edge-S (Pi) | Edge-E (ESP32) |
|---------|-------------|----------------|
| Camera | Yes (Pi Camera 3) | No |
| Video recording | Yes | No |
| Dashboard | Yes (web server) | No |
| Power consumption | ~5-7W | <1W |
| Cost | Higher | Lower |
| Use case | Full data + video | Lightweight logging |

## License

Apache 2.0
