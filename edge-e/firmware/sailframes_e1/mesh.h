// SailFrames Firmware v2.0.0 Stage 2 — ESP-NOW peer-mesh wire types.
// See SF_FIRMWARE_V2_SPEC.md "ESP-NOW peer mesh" for the contract.
//
// Same .h-not-.ino discipline as v2_types.h: the Arduino preprocessor
// auto-generates forward declarations that reference these structs
// before the .ino-level definitions reach the compiler.

#ifndef SAILFRAMES_MESH_H
#define SAILFRAMES_MESH_H

#include <stdint.h>
#include <string.h>

#define MESH_MAGIC_0  0x53  // 'S'
#define MESH_MAGIC_1  0x46  // 'F'
#define MESH_VERSION  1

enum MeshMsgType {
    MSG_BOAT_STATE       = 0x01,
    MSG_LINE_ENDPOINT    = 0x02,  // not yet implemented
    MSG_RACE_ARMED       = 0x10,  // not yet implemented
    MSG_START_LOCKED     = 0x11,  // not yet implemented
    MSG_GENERAL_RECALL   = 0x12,  // not yet implemented
    MSG_INDIVIDUAL_RECALL= 0x13,  // not yet implemented
    MSG_ABANDON          = 0x14,  // not yet implemented
    MSG_SHORTEN_COURSE   = 0x15,  // not yet implemented
    MSG_ACK              = 0x20,  // not yet implemented
};

struct __attribute__((packed)) MeshHeader {
    uint8_t  magic[2];      // 'S','F' (0x53, 0x46)
    uint8_t  version;       // MESH_VERSION
    uint8_t  msg_type;      // MeshMsgType
    uint16_t seq;           // monotonic per sender (rolls 16-bit)
    uint8_t  ttl;           // hops remaining; 0 = no rebroadcast
    uint8_t  reserved;
    uint32_t sender_id;     // FNV1a hash of boat_id string, stable across boots
    uint32_t gps_time_ms;   // GPS time-of-day in ms (rolls daily; 0 if no fix)
};
static_assert(sizeof(MeshHeader) == 16, "MeshHeader must be 16 bytes");

struct __attribute__((packed)) BoatStatePayload {
    int32_t  lat_e7;        // latitude * 1e7  (signed, ~1 cm/lsb at equator)
    int32_t  lon_e7;        // longitude * 1e7
    int16_t  sog_cm_s;      // SOG in cm/s
    int16_t  cog_deg10;     // COG in 0.1°
    int16_t  heading_deg10; // IMU heading in 0.1°
    int8_t   heel_deg;      // heel in degrees, signed
    uint8_t  fix_quality;   // NMEA fix indicator
    uint8_t  sat_count;
    uint8_t  unit_role;     // sender's UnitRole enum value
    uint8_t  reserved[2];   // pad to 20 bytes (spec said "[3]" but its
                            // own size arithmetic listed 20 bytes total —
                            // 4+4+2+2+2+1+1+1+1+2 = 20)
};
static_assert(sizeof(BoatStatePayload) == 20, "BoatStatePayload must be 20 bytes");

// Stage 4.5 — race-armed broadcast. Sent by any boat acting as
// race ops (typically the one tethered to the laptop / RC unit).
// Every receiver translates the relative start time into its own
// millis() clock and arms its boat-local OCS state machine.
//
// We use `seconds_until_start` (signed int32, relative to the
// receiver's millis() at packet arrival) rather than GPS time-of-day
// because not every boat has a GPS fix at the dock. Network latency
// adds ~few-ms drift — acceptable for second-level race timing.
struct __attribute__((packed)) RaceArmedPayload {
    int32_t  pin_lat_e7;
    int32_t  pin_lon_e7;
    int32_t  rc_lat_e7;
    int32_t  rc_lon_e7;
    int32_t  seconds_until_start;   // signed; negative = race already underway
    uint8_t  race_num;              // informational, 1-99
    uint8_t  sequence_mode;         // ISAF Rule 26 = 30, Short = 27, etc. (Stage 7)
    uint8_t  reserved[2];
};
static_assert(sizeof(RaceArmedPayload) == 24, "RaceArmedPayload must be 24 bytes");

// Stage 5 — RC unit broadcasts when it detects a boat over the
// start line at T+0. Target boat (matched by sender_id hash)
// receives this and overrides its local OCS state to over=true,
// regardless of what its own computation said. The RC unit's
// call is authoritative because it knows the canonical line
// endpoints and applies a unified bow_offset_m per class.
//
// Distance is the RC's measurement at the moment of recall —
// useful for post-race auditing if the boat's local state
// disagrees with the RC's.
struct __attribute__((packed)) IndividualRecallPayload {
    uint32_t target_sender_id;   // FNV1a hash of boat being recalled
    int16_t  distance_cm;        // signed; negative = course side (OCS)
    uint8_t  reserved[2];
};
static_assert(sizeof(IndividualRecallPayload) == 8, "IndividualRecallPayload must be 8 bytes");

// FNV-1a 32-bit hash of a NUL-terminated string. Stable across boots
// and across the fleet — every E1/E2/.../B1 hashes its boat_id the
// same way, so receivers can identify senders without a peer registry.
static inline uint32_t boatIdHash(const char* s) {
    uint32_t h = 2166136261u;
    while (s && *s) {
        h ^= (uint8_t)(*s++);
        h *= 16777619u;
    }
    return h;
}

#endif
