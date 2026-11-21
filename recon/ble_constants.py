#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ble_constants.py - verified BLE tables / encoders for the EricssonBluForge demo.

Source of truth: Bluetooth Core Specification 5.4 (author's knowledge) CROSS-CHECKED against
  (a) the Linux kernel header /usr/src/kernels/<ver>/include/net/bluetooth/hci.h (opcodes, event codes, sizes)
  (b) BlueZ 5.77 `btmon -r` decoding of synthetic btsnoop files built from OUR byte layouts
  (c) Python assertions / golden vectors in selftest().

Pure python3 stdlib, no hardware access.  Importing this module never touches the system.
Run `python3 ble_constants.py` for the self-test, `--tables` to print the markdown tables,
`--fill-md FILE` to refresh the generated blocks of 06_ble_spec_reference.md.

Conventions
  * All multi-octet HCI fields are LITTLE-endian on the wire (opcode, intervals, event properties, ...).
  * BD_ADDR textual form "C0:11:22:33:44:55" (MSB first) is sent REVERSED (LSB first) on the wire.
  * AD-structure payload fields: little-endian EXCEPT where the beacon formats say otherwise
    (iBeacon UUID/major/minor are big-endian; Eddystone TLM fields are big-endian; 128-bit service
    UUIDs in AD lists are little-endian i.e. reversed vs. the canonical string).
  * "RF channel" = physical channel number k = (F - 2402) / 2 , k = 0..39.   "LL index" = Link Layer
    channel index (0..36 data, 37..39 advertising).  HCI LE Transmitter/Receiver Test use the RF channel k.
"""
import math
import os
import re
import shutil
import struct
import subprocess
import sys

# ----------------------------------------------------------------------------------------------
# tiny assertion helper (works even under python -O)
# ----------------------------------------------------------------------------------------------
_N_CHECKS = [0]


def _check(cond, msg):
    _N_CHECKS[0] += 1
    if not cond:
        raise AssertionError(msg)


def _rng(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError("%s must be int, got %r" % (name, v))
    if not lo <= v <= hi:
        raise ValueError("%s=%r out of range [%#x..%#x]" % (name, v, lo, hi))
    return v


# ==============================================================================================
# 1. CHANNEL MATH
# ==============================================================================================
BLE_FIRST_MHZ = 2402
BLE_NUM_RF_CHANNELS = 40
ADV_LL_INDICES = (37, 38, 39)
ADV_LL_TO_RF = {37: 0, 38: 12, 39: 39}            # LL index -> RF channel k
RF_TO_ADV_LL = {v: k for k, v in ADV_LL_TO_RF.items()}
ADV_RF_CHANNELS = (0, 12, 39)
ADV_FREQS_MHZ = (2402, 2426, 2480)


def freq_mhz(rf_channel):
    """RF channel k (0..39) -> centre frequency in MHz = 2402 + 2k."""
    _rng(rf_channel, 0, 39, "rf_channel")
    return BLE_FIRST_MHZ + 2 * rf_channel


def rf_channel_from_freq_mhz(mhz):
    """Centre frequency (MHz) -> RF channel k = (F-2402)/2.  This is the HCI TX_Channel / RX_Channel value."""
    _rng(mhz, 2402, 2480, "mhz")
    if (mhz - 2402) % 2:
        raise ValueError("%d MHz is not a BLE channel centre (must be even MHz)" % mhz)
    return (mhz - 2402) // 2


def ll_index_to_rf_channel(ll_index):
    """Link Layer channel index (0..39) -> RF channel number k (0..39)."""
    _rng(ll_index, 0, 39, "ll_index")
    if ll_index in ADV_LL_TO_RF:
        return ADV_LL_TO_RF[ll_index]
    if ll_index <= 10:            # data 0..10  -> RF 1..11   (2404..2424 MHz)
        return ll_index + 1
    return ll_index + 2           # data 11..36 -> RF 13..38  (2428..2478 MHz)


def rf_channel_to_ll_index(rf_channel):
    """RF channel number k (0..39) -> Link Layer channel index (0..39)."""
    _rng(rf_channel, 0, 39, "rf_channel")
    if rf_channel in RF_TO_ADV_LL:
        return RF_TO_ADV_LL[rf_channel]
    if 1 <= rf_channel <= 11:
        return rf_channel - 1
    return rf_channel - 2         # 13..38 -> 11..36


def ll_index_to_freq_mhz(ll_index):
    return freq_mhz(ll_index_to_rf_channel(ll_index))


def freq_mhz_to_ll_index(mhz):
    return rf_channel_to_ll_index(rf_channel_from_freq_mhz(mhz))


def is_advertising_rf_channel(rf_channel):
    return rf_channel in RF_TO_ADV_LL


def channel_role(rf_channel):
    return "adv" if is_advertising_rf_channel(rf_channel) else "data"


# --- HCI LE Transmitter/Receiver Test channel parameter -----------------------------------------
# Core 5.4 Vol 4 Part E (LE Transmitter/Receiver Test, all versions): "TX_Channel: N = (F - 2402) / 2, Range 0x00..0x27".
# I.e. the value is the RF channel number k, NOT the LL index.  Passing 37/38/39 means 2476/2478/2480 MHz.
def hci_test_channel_from_freq_mhz(mhz):
    return rf_channel_from_freq_mhz(mhz)


def hci_test_channel_from_ll_index(ll_index):
    """What to put in TX_Channel/RX_Channel to hit the frequency of Link Layer channel `ll_index`."""
    return ll_index_to_rf_channel(ll_index)


def hci_test_channel_to_freq_mhz(tx_channel):
    return freq_mhz(_rng(tx_channel, 0, 0x27, "TX_Channel"))


# Full table: list of dicts, index = RF channel
CHANNELS = [
    {"rf": k, "ll": rf_channel_to_ll_index(k), "mhz": freq_mhz(k), "role": channel_role(k)}
    for k in range(BLE_NUM_RF_CHANNELS)
]

# --- Wi-Fi 2.4 GHz overlap ----------------------------------------------------------------------
WIFI_2G4_CENTER_MHZ = {ch: 2407 + 5 * ch for ch in range(1, 14)}
WIFI_2G4_CENTER_MHZ[14] = 2484          # Japan, 802.11b only


def wifi_overlap_rf_channels(wifi_channel, width_mhz=20):
    """BLE RF channels (list of k) whose centre lies within fc +/- width/2 of the Wi-Fi channel centre."""
    fc = WIFI_2G4_CENTER_MHZ[wifi_channel]
    half = width_mhz / 2.0
    return [k for k in range(BLE_NUM_RF_CHANNELS) if abs(freq_mhz(k) - fc) <= half]


def ble_rf_channel_wifi_overlaps(rf_channel, width_mhz=20, wifi_channels=range(1, 14)):
    f = freq_mhz(rf_channel)
    return [w for w in wifi_channels if abs(f - WIFI_2G4_CENTER_MHZ[w]) <= width_mhz / 2.0]


def _ranges(nums):
    """[0,1,2,5,6] -> '0-2, 5-6'"""
    out, nums = [], sorted(nums)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else "%d-%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(out) if out else "-"


# ==============================================================================================
# 2. HCI REFERENCE
# ==============================================================================================
H4_COMMAND, H4_ACL, H4_SCO, H4_EVENT, H4_ISO = 0x01, 0x02, 0x03, 0x04, 0x05

OGF_LINK_CONTROL, OGF_LINK_POLICY, OGF_CTRL_BB, OGF_INFO, OGF_STATUS, OGF_TESTING, OGF_LE, OGF_VENDOR = \
    0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x3F


def make_opcode(ogf, ocf):
    _rng(ogf, 0, 0x3F, "ogf")
    _rng(ocf, 0, 0x3FF, "ocf")
    return (ogf << 10) | ocf


def split_opcode(op):
    return op >> 10, op & 0x3FF


# key: (spec name, OGF, OCF, kernel-hci.h macro (or None), parameter layout, return layout)
_CMD_TABLE = {
    "reset":                    ("Reset", 0x03, 0x0003, "HCI_OP_RESET", "-", "Status(1)"),
    "set_event_mask":           ("Set Event Mask", 0x03, 0x0001, "HCI_OP_SET_EVENT_MASK", "Event_Mask(8)", "Status(1)"),
    "read_local_supported_commands": ("Read Local Supported Commands", 0x04, 0x0002, None, "-", "Status(1), Supported_Commands(64)"),
    "le_set_event_mask":        ("LE Set Event Mask", 0x08, 0x0001, "HCI_OP_LE_SET_EVENT_MASK", "LE_Event_Mask(8)", "Status(1)"),
    "le_read_local_features":   ("LE Read Local Supported Features", 0x08, 0x0003, "HCI_OP_LE_READ_LOCAL_FEATURES", "-", "Status(1), LE_Features(8)"),
    "le_set_random_address":    ("LE Set Random Address", 0x08, 0x0005, "HCI_OP_LE_SET_RANDOM_ADDR", "Random_Address(6)", "Status(1)"),
    "le_set_adv_params":        ("LE Set Advertising Parameters", 0x08, 0x0006, "HCI_OP_LE_SET_ADV_PARAM",
                                 "Interval_Min(2) Interval_Max(2) Adv_Type(1) Own_Addr_Type(1) Peer_Addr_Type(1) Peer_Addr(6) Chan_Map(1) Filter_Policy(1) = 15", "Status(1)"),
    "le_set_adv_data":          ("LE Set Advertising Data", 0x08, 0x0008, "HCI_OP_LE_SET_ADV_DATA", "Data_Length(1) Data(31, zero padded) = 32", "Status(1)"),
    "le_set_scan_rsp_data":     ("LE Set Scan Response Data", 0x08, 0x0009, "HCI_OP_LE_SET_SCAN_RSP_DATA", "Data_Length(1) Data(31, zero padded) = 32", "Status(1)"),
    "le_set_adv_enable":        ("LE Set Advertising Enable", 0x08, 0x000A, "HCI_OP_LE_SET_ADV_ENABLE", "Advertising_Enable(1)", "Status(1)"),
    "le_set_scan_params":       ("LE Set Scan Parameters", 0x08, 0x000B, "HCI_OP_LE_SET_SCAN_PARAM",
                                 "Scan_Type(1) Interval(2) Window(2) Own_Addr_Type(1) Filter_Policy(1) = 7", "Status(1)"),
    "le_set_scan_enable":       ("LE Set Scan Enable", 0x08, 0x000C, "HCI_OP_LE_SET_SCAN_ENABLE", "Scan_Enable(1) Filter_Duplicates(1) = 2", "Status(1)"),
    "le_read_supported_states": ("LE Read Supported States", 0x08, 0x001C, "HCI_OP_LE_READ_SUPPORTED_STATES", "-", "Status(1), LE_States(8)"),
    "le_receiver_test_v1":      ("LE Receiver Test [v1]", 0x08, 0x001D, None, "RX_Channel(1)", "Status(1)"),
    "le_transmitter_test_v1":   ("LE Transmitter Test [v1]", 0x08, 0x001E, None, "TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) = 3", "Status(1)"),
    "le_test_end":              ("LE Test End", 0x08, 0x001F, None, "-", "Status(1), Num_Packets(2)"),
    "le_receiver_test_v2":      ("LE Receiver Test [v2]", 0x08, 0x0033, None, "RX_Channel(1) PHY(1) Modulation_Index(1) = 3", "Status(1)"),
    "le_transmitter_test_v2":   ("LE Transmitter Test [v2]", 0x08, 0x0034, None, "TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) PHY(1) = 4", "Status(1)"),
    "le_set_adv_set_random_address": ("LE Set Advertising Set Random Address", 0x08, 0x0035, "HCI_OP_LE_SET_ADV_SET_RAND_ADDR", "Advertising_Handle(1) Random_Address(6) = 7", "Status(1)"),
    "le_set_ext_adv_params":    ("LE Set Extended Advertising Parameters [v1]", 0x08, 0x0036, "HCI_OP_LE_SET_EXT_ADV_PARAMS",
                                 "Handle(1) Event_Properties(2) Pri_Int_Min(3) Pri_Int_Max(3) Pri_Chan_Map(1) Own_Addr_Type(1) Peer_Addr_Type(1) Peer_Addr(6) Filter_Policy(1) "
                                 "TX_Power(1,s) Pri_PHY(1) Sec_Max_Skip(1) Sec_PHY(1) SID(1) Scan_Req_Notif(1) = 25", "Status(1), Selected_TX_Power(1,s)"),
    "le_set_ext_adv_data":      ("LE Set Extended Advertising Data", 0x08, 0x0037, "HCI_OP_LE_SET_EXT_ADV_DATA",
                                 "Handle(1) Operation(1) Fragment_Pref(1) Data_Length(1) Data(0..251)", "Status(1)"),
    "le_set_ext_scan_rsp_data": ("LE Set Extended Scan Response Data", 0x08, 0x0038, "HCI_OP_LE_SET_EXT_SCAN_RSP_DATA",
                                 "Handle(1) Operation(1) Fragment_Pref(1) Data_Length(1) Data(0..251)", "Status(1)"),
    "le_set_ext_adv_enable":    ("LE Set Extended Advertising Enable", 0x08, 0x0039, "HCI_OP_LE_SET_EXT_ADV_ENABLE",
                                 "Enable(1) Num_Sets(1) { Handle(1) Duration(2) Max_Ext_Adv_Events(1) } x Num_Sets", "Status(1)"),
    "le_read_max_adv_data_len": ("LE Read Maximum Advertising Data Length", 0x08, 0x003A, None, "-", "Status(1), Max_Adv_Data_Length(2)"),
    "le_read_num_adv_sets":     ("LE Read Number of Supported Advertising Sets", 0x08, 0x003B, "HCI_OP_LE_READ_NUM_SUPPORTED_ADV_SETS", "-", "Status(1), Num_Sets(1)"),
    "le_remove_adv_set":        ("LE Remove Advertising Set", 0x08, 0x003C, "HCI_OP_LE_REMOVE_ADV_SET", "Advertising_Handle(1)", "Status(1)"),
    "le_clear_adv_sets":        ("LE Clear Advertising Sets", 0x08, 0x003D, "HCI_OP_LE_CLEAR_ADV_SETS", "-", "Status(1)"),
    "le_set_periodic_adv_params": ("LE Set Periodic Advertising Parameters [v1]", 0x08, 0x003E, "HCI_OP_LE_SET_PER_ADV_PARAMS",
                                   "Handle(1) Interval_Min(2) Interval_Max(2) Properties(2) = 7", "Status(1)"),
    "le_set_periodic_adv_data": ("LE Set Periodic Advertising Data", 0x08, 0x003F, "HCI_OP_LE_SET_PER_ADV_DATA",
                                 "Handle(1) Operation(1) Data_Length(1) Data(0..252)", "Status(1)"),
    "le_set_periodic_adv_enable": ("LE Set Periodic Advertising Enable", 0x08, 0x0040, "HCI_OP_LE_SET_PER_ADV_ENABLE", "Enable(1) Handle(1) = 2", "Status(1)"),
    "le_set_ext_scan_params":   ("LE Set Extended Scan Parameters", 0x08, 0x0041, "HCI_OP_LE_SET_EXT_SCAN_PARAMS",
                                 "Own_Addr_Type(1) Filter_Policy(1) Scanning_PHYs(1) { Scan_Type(1) Interval(2) Window(2) } x popcount(PHYs)", "Status(1)"),
    "le_set_ext_scan_enable":   ("LE Set Extended Scan Enable", 0x08, 0x0042, "HCI_OP_LE_SET_EXT_SCAN_ENABLE",
                                 "Enable(1) Filter_Duplicates(1) Duration(2) Period(2) = 6", "Status(1)"),
    "le_read_transmit_power":   ("LE Read Transmit Power", 0x08, 0x004B, "HCI_OP_LE_READ_TRANSMIT_POWER", "-", "Status(1), Min_TX_Power(1,s), Max_TX_Power(1,s)"),
    "le_receiver_test_v3":      ("LE Receiver Test [v3]", 0x08, 0x004F, None,
                                 "RX_Channel(1) PHY(1) Modulation_Index(1) Expected_CTE_Length(1) Expected_CTE_Type(1) Slot_Durations(1) Switching_Pattern_Length(1) Antenna_IDs(n)", "Status(1)"),
    "le_transmitter_test_v3":   ("LE Transmitter Test [v3]", 0x08, 0x0050, None,
                                 "TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) PHY(1) CTE_Length(1) CTE_Type(1) Switching_Pattern_Length(1) Antenna_IDs(n)", "Status(1)"),
    "le_transmitter_test_v4":   ("LE Transmitter Test [v4]", 0x08, 0x007B, None,
                                 "TX_Channel(1) Test_Data_Length(1) Packet_Payload(1) PHY(1) CTE_Length(1) CTE_Type(1) Switching_Pattern_Length(1) Antenna_IDs(n) Transmit_Power_Level(1,s)", "Status(1)"),
}

COMMANDS = {}                 # key -> dict(name, ogf, ocf, opcode, kernel_macro, params, ret)
OPCODE = {}                   # key -> opcode (int)
for _k, (_n, _ogf, _ocf, _kmac, _p, _r) in _CMD_TABLE.items():
    COMMANDS[_k] = dict(key=_k, name=_n, ogf=_ogf, ocf=_ocf, opcode=make_opcode(_ogf, _ocf),
                        kernel_macro=_kmac, params=_p, ret=_r)
    OPCODE[_k] = COMMANDS[_k]["opcode"]

# Hand-typed, INDEPENDENT literals (not derived from the table) used by the self-test.
_EXPECTED_OPCODES = {
    "reset": 0x0C03, "set_event_mask": 0x0C01, "read_local_supported_commands": 0x1002,
    "le_set_event_mask": 0x2001, "le_read_local_features": 0x2003, "le_set_random_address": 0x2005,
    "le_set_adv_params": 0x2006, "le_set_adv_data": 0x2008, "le_set_scan_rsp_data": 0x2009,
    "le_set_adv_enable": 0x200A, "le_set_scan_params": 0x200B, "le_set_scan_enable": 0x200C,
    "le_read_supported_states": 0x201C, "le_receiver_test_v1": 0x201D, "le_transmitter_test_v1": 0x201E,
    "le_test_end": 0x201F, "le_receiver_test_v2": 0x2033, "le_transmitter_test_v2": 0x2034,
    "le_set_adv_set_random_address": 0x2035, "le_set_ext_adv_params": 0x2036, "le_set_ext_adv_data": 0x2037,
    "le_set_ext_scan_rsp_data": 0x2038, "le_set_ext_adv_enable": 0x2039, "le_read_max_adv_data_len": 0x203A,
    "le_read_num_adv_sets": 0x203B, "le_remove_adv_set": 0x203C, "le_clear_adv_sets": 0x203D,
    "le_set_periodic_adv_params": 0x203E, "le_set_periodic_adv_data": 0x203F, "le_set_periodic_adv_enable": 0x2040,
    "le_set_ext_scan_params": 0x2041, "le_set_ext_scan_enable": 0x2042, "le_read_transmit_power": 0x204B,
    "le_receiver_test_v3": 0x204F, "le_transmitter_test_v3": 0x2050, "le_transmitter_test_v4": 0x207B,
}

# --- HCI event codes / LE meta subevents ----------------------------------------------------------
EVT_COMMAND_COMPLETE, EVT_COMMAND_STATUS, EVT_LE_META = 0x0E, 0x0F, 0x3E
LE_SUBEVENTS = {
    0x01: "LE Connection Complete", 0x02: "LE Advertising Report", 0x03: "LE Connection Update Complete",
    0x04: "LE Read Remote Features Complete", 0x05: "LE Long Term Key Request",
    0x06: "LE Remote Connection Parameter Request", 0x07: "LE Data Length Change",
    0x08: "LE Read Local P-256 Public Key Complete", 0x09: "LE Generate DHKey Complete",
    0x0A: "LE Enhanced Connection Complete", 0x0B: "LE Directed Advertising Report", 0x0C: "LE PHY Update Complete",
    0x0D: "LE Extended Advertising Report", 0x0E: "LE Periodic Advertising Sync Established",
    0x0F: "LE Periodic Advertising Report", 0x10: "LE Periodic Advertising Sync Lost", 0x11: "LE Scan Timeout",
    0x12: "LE Advertising Set Terminated", 0x13: "LE Scan Request Received", 0x14: "LE Channel Selection Algorithm",
}
# LE Set Event Mask: bit n enables subevent (n+1)  (bit0 = LE Connection Complete = subevent 0x01)
LE_EVENT_MASK_BIT = {name: code - 1 for code, name in LE_SUBEVENTS.items()}
LE_EVENT_MASK_DEFAULT = 0x000000000000001F        # spec default (bits 0-4)
EVENT_MASK_DEFAULT = 0x00001FFFFFFFFFFF           # Set Event Mask default (does NOT include LE Meta!)
EVENT_MASK_LE_META = 1 << 61                      # must be set (kernel does events[7] |= 0x20) or no LE subevent is ever delivered
EVENT_MASK_HOST_TYPICAL = EVENT_MASK_DEFAULT | EVENT_MASK_LE_META


def le_event_mask(*subevent_names_or_codes):
    m = 0
    for x in subevent_names_or_codes:
        code = x if isinstance(x, int) else {v: k for k, v in LE_SUBEVENTS.items()}[x]
        m |= 1 << (code - 1)
    return m


# Commonly seen HCI status codes (Core Vol 1 Part F)
HCI_STATUS = {
    0x00: "Success", 0x01: "Unknown HCI Command", 0x07: "Memory Capacity Exceeded", 0x0C: "Command Disallowed",
    0x11: "Unsupported Feature or Parameter Value", 0x12: "Invalid HCI Command Parameters", 0x3A: "Controller Busy",
    0x3C: "Advertising Timeout",
    0x42: "Unknown Advertising Identifier", 0x43: "Limit Reached", 0x45: "Packet Too Long",
}

# --- Parameter value tables -----------------------------------------------------------------------
ADV_IND, ADV_DIRECT_IND_HIGH, ADV_SCAN_IND, ADV_NONCONN_IND, ADV_DIRECT_IND_LOW = 0x00, 0x01, 0x02, 0x03, 0x04
ADV_TYPE_NAMES = {0x00: "ADV_IND", 0x01: "ADV_DIRECT_IND (high duty)", 0x02: "ADV_SCAN_IND",
                  0x03: "ADV_NONCONN_IND", 0x04: "ADV_DIRECT_IND (low duty)"}
# On-air PDU_Type nibble (advertising channel PDU header bits 0-3) - note it DIFFERS from the HCI Advertising_Type
PDU_TYPE = {"ADV_IND": 0x0, "ADV_DIRECT_IND": 0x1, "ADV_NONCONN_IND": 0x2, "SCAN_REQ": 0x3, "SCAN_RSP": 0x4,
            "CONNECT_IND": 0x5, "ADV_SCAN_IND": 0x6, "ADV_EXT_IND": 0x7}   # 0x7 = ADV_EXT_IND / AUX_*; 0x8 = AUX_CONNECT_RSP
# LE Advertising Report Event_Type (legacy HCI event) values
ADV_REPORT_EVT_TYPE = {0x00: "ADV_IND", 0x01: "ADV_DIRECT_IND", 0x02: "ADV_SCAN_IND", 0x03: "ADV_NONCONN_IND", 0x04: "SCAN_RSP"}

CHAN_MAP_37, CHAN_MAP_38, CHAN_MAP_39, CHAN_MAP_ALL = 0x01, 0x02, 0x04, 0x07
ADV_INTERVAL_UNIT_MS = 0.625
ADV_INTERVAL_MIN_UNITS, ADV_INTERVAL_MAX_UNITS = 0x0020, 0x4000          # 20 ms .. 10.24 s  (Core >= 5.0, all legacy types)
ADV_INTERVAL_MIN_UNITS_NONCONN_PRE50 = 0x00A0                             # 100 ms floor for NONCONN/SCAN_IND in Core <= 4.2
SCAN_INTERVAL_MIN_UNITS, SCAN_INTERVAL_MAX_UNITS = 0x0004, 0x4000         # 2.5 ms .. 10.24 s

OWN_ADDR_PUBLIC, OWN_ADDR_RANDOM, OWN_ADDR_RPA_OR_PUBLIC, OWN_ADDR_RPA_OR_RANDOM = 0x00, 0x01, 0x02, 0x03
PEER_ADDR_PUBLIC, PEER_ADDR_RANDOM = 0x00, 0x01
ADV_FILTER_ALL, ADV_FILTER_SCAN_FAL, ADV_FILTER_CONN_FAL, ADV_FILTER_BOTH_FAL = 0, 1, 2, 3
SCAN_PASSIVE, SCAN_ACTIVE = 0x00, 0x01
SCAN_FILTER_ALL, SCAN_FILTER_FAL = 0x00, 0x01
DUP_FILTER_OFF, DUP_FILTER_ON, DUP_FILTER_ON_RESET_PER_PERIOD = 0x00, 0x01, 0x02    # 0x02 only for Ext Scan Enable

# Extended advertising Event_Properties (LE Set Extended Advertising Parameters)
EXT_PROP_CONNECTABLE = 1 << 0
EXT_PROP_SCANNABLE = 1 << 1
EXT_PROP_DIRECTED = 1 << 2
EXT_PROP_HIGH_DUTY_DIRECTED = 1 << 3
EXT_PROP_LEGACY_PDU = 1 << 4
EXT_PROP_ANONYMOUS = 1 << 5
EXT_PROP_INCLUDE_TX_POWER = 1 << 6
EXT_PROP_LEGACY = {  # legacy-PDU emulation values (Core table "Advertising_Event_Properties" legacy combinations)
    "ADV_IND": 0x0013, "ADV_DIRECT_IND_LOW": 0x0015, "ADV_DIRECT_IND_HIGH": 0x001D,
    "ADV_SCAN_IND": 0x0012, "ADV_NONCONN_IND": 0x0010,
}
# Extended Advertising Report Event_Type (16-bit)
EXT_REPORT_CONNECTABLE, EXT_REPORT_SCANNABLE, EXT_REPORT_DIRECTED, EXT_REPORT_SCAN_RSP, EXT_REPORT_LEGACY = \
    0x0001, 0x0002, 0x0004, 0x0008, 0x0010
EXT_REPORT_DATA_STATUS_SHIFT = 5          # bits 5-6: 0 complete, 1 incomplete(more to come), 2 incomplete truncated
EXT_REPORT_LEGACY_TYPES = {
    0x0013: "ADV_IND", 0x0015: "ADV_DIRECT_IND", 0x0012: "ADV_SCAN_IND", 0x0010: "ADV_NONCONN_IND",
    0x001B: "SCAN_RSP to ADV_IND", 0x001A: "SCAN_RSP to ADV_SCAN_IND",
}
PHY_1M, PHY_2M, PHY_CODED_S8, PHY_CODED_S2 = 0x01, 0x02, 0x03, 0x04     # TX-test v2+: 3=Coded S=8, 4=Coded S=2
EXT_ADV_OP_INTERMEDIATE, EXT_ADV_OP_FIRST, EXT_ADV_OP_LAST, EXT_ADV_OP_COMPLETE, EXT_ADV_OP_UNCHANGED = 0, 1, 2, 3, 4
MAX_LEGACY_ADV_DATA = 31
MAX_EXT_ADV_DATA_PER_CMD = 251
MAX_PERIODIC_ADV_DATA_PER_CMD = 252
MAX_EXT_ADV_DATA_TOTAL = 1650
MAX_EXT_ADV_REPORT_DATA = 229

# DTM Packet_Payload codes (LE Transmitter Test) -> (name, on-air bit order as written in spec, byte value if repeating)
def _byte_from_tx_order(bits):
    """'11110000' written in TRANSMISSION order (first bit sent first, LSB first) -> octet value."""
    assert len(bits) == 8
    return int(bits[::-1], 2)


DTM_PAYLOAD = {
    0x00: ("PRBS9", None),
    0x01: ("Repeated 11110000", _byte_from_tx_order("11110000")),
    0x02: ("Repeated 10101010", _byte_from_tx_order("10101010")),
    0x03: ("PRBS15", None),
    0x04: ("Repeated 11111111", _byte_from_tx_order("11111111")),
    0x05: ("Repeated 00000000", _byte_from_tx_order("00000000")),
    0x06: ("Repeated 00001111", _byte_from_tx_order("00001111")),
    0x07: ("Repeated 01010101", _byte_from_tx_order("01010101")),
}
# readable aliases by octet value
DTM_PAYLOAD_BY_OCTET = {0x0F: 0x01, 0x55: 0x02, 0xFF: 0x04, 0x00: 0x05, 0xF0: 0x06, 0xAA: 0x07}


def prbs9_bits(n):
    """PRBS9, x^9 + x^5 + 1, all-ones seed; first output bits '11111111100000111101' (spec Vol 6 Part F, test packet payload)."""
    s, out = 0x1FF, []
    for _ in range(n):
        o = (s >> 8) & 1
        fb = ((s >> 8) ^ (s >> 4)) & 1
        s = ((s << 1) | fb) & 0x1FF
        out.append(o)
    return out


def dtm_payload_bytes(pattern, length):
    """Octets of the DTM test payload (LSB of each octet is transmitted first)."""
    _rng(length, 0, 255, "length")
    if pattern == 0x00:
        bits = prbs9_bits(8 * length)
        return bytes(sum(bits[8 * i + j] << j for j in range(8)) for i in range(length))
    if pattern in DTM_PAYLOAD and DTM_PAYLOAD[pattern][1] is not None:
        return bytes([DTM_PAYLOAD[pattern][1]]) * length
    raise NotImplementedError("pattern %#x (PRBS15 not implemented / reserved)" % pattern)


# --- address helpers --------------------------------------------------------------------------------
def addr_to_wire(addr):
    if isinstance(addr, (bytes, bytearray)):
        if len(addr) != 6:
            raise ValueError("address bytes must be 6 long")
        return bytes(addr)
    parts = addr.split(":")
    if len(parts) != 6:
        raise ValueError("bad address %r" % addr)
    return bytes(int(p, 16) for p in reversed(parts))


def addr_from_wire(b):
    return ":".join("%02X" % x for x in reversed(b))


def random_address_kind(addr):
    """Classify a random address by its two MSBs (Core Vol 6 Part B 1.3): 11 static, 01 RPA, 00 non-resolvable."""
    msb = addr_to_wire(addr)[5] >> 6
    return {0b11: "static", 0b01: "resolvable-private", 0b00: "non-resolvable-private"}.get(msb, "invalid(10)")


# --- units -------------------------------------------------------------------------------------------
def ms_to_adv_units(ms):
    u = ms / ADV_INTERVAL_UNIT_MS
    if abs(u - round(u)) > 1e-9:
        raise ValueError("%r ms is not a multiple of 0.625 ms" % ms)
    return int(round(u))


def adv_units_to_ms(u):
    return u * ADV_INTERVAL_UNIT_MS


# --- command builders (return full H4 packet: 01 | opcode LE16 | plen | params) --------------------------
def hci_command(opcode, params=b""):
    _rng(opcode, 0, 0xFFFF, "opcode")
    if len(params) > 255:
        raise ValueError("HCI command parameters > 255 octets")
    return bytes([H4_COMMAND]) + struct.pack("<HB", opcode, len(params)) + bytes(params)


def _cmd(key, params=b""):
    return hci_command(OPCODE[key], params)


def reset():
    return _cmd("reset")


def set_event_mask(mask=EVENT_MASK_DEFAULT):
    return _cmd("set_event_mask", struct.pack("<Q", mask))


def le_set_event_mask(mask=LE_EVENT_MASK_DEFAULT):
    return _cmd("le_set_event_mask", struct.pack("<Q", mask))


def le_set_random_address(addr):
    return _cmd("le_set_random_address", addr_to_wire(addr))


def le_set_adv_params(interval_min=0x0800, interval_max=0x0800, adv_type=ADV_IND, own_addr_type=OWN_ADDR_PUBLIC,
                      peer_addr_type=PEER_ADDR_PUBLIC, peer_addr="00:00:00:00:00:00", channel_map=CHAN_MAP_ALL,
                      filter_policy=ADV_FILTER_ALL, interval_floor=ADV_INTERVAL_MIN_UNITS):
    """Intervals are in 0.625 ms units (use ms_to_adv_units). interval_floor=0x00A0 reproduces the pre-5.0 rule."""
    _rng(interval_min, interval_floor, ADV_INTERVAL_MAX_UNITS, "interval_min")
    _rng(interval_max, interval_min, ADV_INTERVAL_MAX_UNITS, "interval_max")
    _rng(adv_type, 0, 4, "adv_type")
    _rng(own_addr_type, 0, 3, "own_addr_type")
    _rng(peer_addr_type, 0, 1, "peer_addr_type")
    _rng(channel_map, 1, 7, "channel_map")
    _rng(filter_policy, 0, 3, "filter_policy")
    p = struct.pack("<HHBBB6sBB", interval_min, interval_max, adv_type, own_addr_type, peer_addr_type,
                    addr_to_wire(peer_addr), channel_map, filter_policy)
    assert len(p) == 15
    return _cmd("le_set_adv_params", p)


def _pad31(data, what):
    if len(data) > MAX_LEGACY_ADV_DATA:
        raise ValueError("%s is %d octets, legacy limit is 31" % (what, len(data)))
    return bytes([len(data)]) + bytes(data) + bytes(MAX_LEGACY_ADV_DATA - len(data))


def le_set_adv_data(data):
    return _cmd("le_set_adv_data", _pad31(data, "advertising data"))


def le_set_scan_rsp_data(data):
    return _cmd("le_set_scan_rsp_data", _pad31(data, "scan response data"))


def le_set_adv_enable(enable=True):
    return _cmd("le_set_adv_enable", bytes([1 if enable else 0]))


def le_set_scan_params(scan_type=SCAN_PASSIVE, interval=0x0010, window=0x0010, own_addr_type=OWN_ADDR_PUBLIC,
                       filter_policy=SCAN_FILTER_ALL):
    _rng(scan_type, 0, 1, "scan_type")
    _rng(interval, SCAN_INTERVAL_MIN_UNITS, SCAN_INTERVAL_MAX_UNITS, "interval")
    _rng(window, SCAN_INTERVAL_MIN_UNITS, interval, "window (<= interval)")
    _rng(own_addr_type, 0, 3, "own_addr_type")
    _rng(filter_policy, 0, 3, "filter_policy")
    return _cmd("le_set_scan_params", struct.pack("<BHHBB", scan_type, interval, window, own_addr_type, filter_policy))


def le_set_scan_enable(enable=True, filter_duplicates=DUP_FILTER_OFF):
    _rng(filter_duplicates, 0, 1, "filter_duplicates (legacy cmd: 0/1 only)")
    return _cmd("le_set_scan_enable", bytes([1 if enable else 0, filter_duplicates]))


# --- Direct Test Mode --------------------------------------------------------------------------------
def le_receiver_test_v1(rx_channel):
    return _cmd("le_receiver_test_v1", bytes([_rng(rx_channel, 0, 0x27, "RX_Channel")]))


def le_transmitter_test_v1(tx_channel, test_data_length, packet_payload=0x00):
    return _cmd("le_transmitter_test_v1", bytes([_rng(tx_channel, 0, 0x27, "TX_Channel"),
                                                 _rng(test_data_length, 0, 255, "Test_Data_Length"),
                                                 _rng(packet_payload, 0, 7, "Packet_Payload")]))


def le_test_end():
    return _cmd("le_test_end")


def le_receiver_test_v2(rx_channel, phy=PHY_1M, modulation_index=0):
    return _cmd("le_receiver_test_v2", bytes([_rng(rx_channel, 0, 0x27, "RX_Channel"), _rng(phy, 1, 3, "PHY"),
                                              _rng(modulation_index, 0, 1, "Modulation_Index")]))


def le_transmitter_test_v2(tx_channel, test_data_length, packet_payload=0x00, phy=PHY_1M):
    return _cmd("le_transmitter_test_v2", bytes([_rng(tx_channel, 0, 0x27, "TX_Channel"),
                                                 _rng(test_data_length, 0, 255, "Test_Data_Length"),
                                                 _rng(packet_payload, 0, 7, "Packet_Payload"),
                                                 _rng(phy, 1, 4, "PHY")]))


def _tx_v3_body(tx_channel, test_data_length, packet_payload, phy, cte_length, cte_type, antenna_ids):
    ids = bytes(antenna_ids)
    if cte_length == 0 and ids:
        raise ValueError("antenna_ids given but cte_length == 0")
    return bytes([_rng(tx_channel, 0, 0x27, "TX_Channel"), _rng(test_data_length, 0, 255, "Test_Data_Length"),
                  _rng(packet_payload, 0, 7, "Packet_Payload"), _rng(phy, 1, 4, "PHY"),
                  _rng(cte_length, 0, 0x14, "CTE_Length"), _rng(cte_type, 0, 2, "CTE_Type"), len(ids)]) + ids


def le_transmitter_test_v3(tx_channel, test_data_length, packet_payload=0x00, phy=PHY_1M, cte_length=0, cte_type=0,
                           antenna_ids=()):
    return _cmd("le_transmitter_test_v3", _tx_v3_body(tx_channel, test_data_length, packet_payload, phy, cte_length,
                                                      cte_type, antenna_ids))


def le_transmitter_test_v4(tx_channel, test_data_length, packet_payload=0x00, phy=PHY_1M, cte_length=0, cte_type=0,
                           antenna_ids=(), tx_power_dbm=0x7F):
    """tx_power_dbm: signed dBm -127..+20; 0x7E (=126) min / 0x7F (=127) max request (sent raw)."""
    body = _tx_v3_body(tx_channel, test_data_length, packet_payload, phy, cte_length, cte_type, antenna_ids)
    if tx_power_dbm in (0x7E, 0x7F):
        pw = bytes([tx_power_dbm])
    else:
        pw = struct.pack("<b", _rng(tx_power_dbm, -127, 20, "tx_power_dbm"))
    return _cmd("le_transmitter_test_v4", body + pw)


# --- extended / periodic advertising ----------------------------------------------------------------
def le_set_adv_set_random_address(handle, addr):
    return _cmd("le_set_adv_set_random_address", bytes([_rng(handle, 0, 0xEF, "handle")]) + addr_to_wire(addr))


def le_set_ext_adv_params(handle=0, event_properties=EXT_PROP_LEGACY["ADV_IND"], interval_min=0x0030,
                          interval_max=0x0030, channel_map=CHAN_MAP_ALL, own_addr_type=OWN_ADDR_PUBLIC,
                          peer_addr_type=PEER_ADDR_PUBLIC, peer_addr="00:00:00:00:00:00", filter_policy=ADV_FILTER_ALL,
                          tx_power=0x7F, primary_phy=PHY_1M, secondary_max_skip=0, secondary_phy=PHY_1M, sid=0,
                          scan_req_notification=0):
    """Intervals are 24-bit, 0.625 ms units (min 0x000020). tx_power: signed dBm, 0x7F = no preference."""
    _rng(handle, 0, 0xEF, "handle")
    _rng(event_properties, 0, 0x7F, "event_properties")
    _rng(interval_min, 0x20, 0xFFFFFF, "interval_min")
    _rng(interval_max, interval_min, 0xFFFFFF, "interval_max")
    _rng(channel_map, 1, 7, "channel_map")
    _rng(primary_phy, 1, 3, "primary_phy")
    if primary_phy == 2:
        raise ValueError("primary PHY 0x02 (2M) is reserved")
    _rng(secondary_phy, 1, 3, "secondary_phy")
    _rng(sid, 0, 0x0F, "sid")
    txp = bytes([tx_power]) if tx_power == 0x7F else struct.pack("<b", _rng(tx_power, -127, 20, "tx_power"))
    p = (bytes([handle]) + struct.pack("<H", event_properties) + interval_min.to_bytes(3, "little") +
         interval_max.to_bytes(3, "little") +
         bytes([channel_map, own_addr_type, peer_addr_type]) + addr_to_wire(peer_addr) + bytes([filter_policy]) + txp +
         bytes([primary_phy, secondary_max_skip, secondary_phy, sid, scan_req_notification]))
    assert len(p) == 25
    return _cmd("le_set_ext_adv_params", p)


def le_set_ext_adv_data(handle, data, operation=EXT_ADV_OP_COMPLETE, fragment_preference=1, _key="le_set_ext_adv_data"):
    _rng(handle, 0, 0xEF, "handle")
    _rng(operation, 0, 4 if _key == "le_set_ext_adv_data" else 3, "operation")
    if len(data) > MAX_EXT_ADV_DATA_PER_CMD:
        raise ValueError("%d octets > %d per command (fragment with Operation 1/0/2)" % (len(data), MAX_EXT_ADV_DATA_PER_CMD))
    return _cmd(_key, bytes([handle, operation, _rng(fragment_preference, 0, 1, "fragment_preference"), len(data)]) + bytes(data))


def le_set_ext_scan_rsp_data(handle, data, operation=EXT_ADV_OP_COMPLETE, fragment_preference=1):
    return le_set_ext_adv_data(handle, data, operation, fragment_preference, _key="le_set_ext_scan_rsp_data")


def le_set_ext_adv_enable(enable, sets=()):
    """sets: iterable of (handle, duration_10ms_units, max_ext_adv_events). enable=False with no sets disables all."""
    sets = list(sets)
    p = bytes([1 if enable else 0, len(sets)])
    for h, dur, mx in sets:
        p += struct.pack("<BHB", _rng(h, 0, 0xEF, "handle"), _rng(dur, 0, 0xFFFF, "duration"), _rng(mx, 0, 255, "max_events"))
    return _cmd("le_set_ext_adv_enable", p)


def le_remove_adv_set(handle):
    return _cmd("le_remove_adv_set", bytes([_rng(handle, 0, 0xEF, "handle")]))


def le_clear_adv_sets():
    return _cmd("le_clear_adv_sets")


def le_set_periodic_adv_params(handle, interval_min=0x0006, interval_max=0x0010, properties=0):
    """Interval units 1.25 ms (min 0x0006 = 7.5 ms). properties bit 6 = include TxPower."""
    return _cmd("le_set_periodic_adv_params", struct.pack("<BHHH", _rng(handle, 0, 0xEF, "handle"),
                                                          _rng(interval_min, 6, 0xFFFF, "interval_min"),
                                                          _rng(interval_max, interval_min, 0xFFFF, "interval_max"),
                                                          _rng(properties, 0, 0xFFFF, "properties")))


def le_set_periodic_adv_data(handle, data, operation=EXT_ADV_OP_COMPLETE):
    if len(data) > MAX_PERIODIC_ADV_DATA_PER_CMD:
        raise ValueError("periodic data > 252 per command")
    return _cmd("le_set_periodic_adv_data", bytes([_rng(handle, 0, 0xEF, "handle"), _rng(operation, 0, 4, "operation"), len(data)]) + bytes(data))


def le_set_periodic_adv_enable(enable, handle, include_adi=False):
    return _cmd("le_set_periodic_adv_enable", bytes([(1 if enable else 0) | (2 if include_adi else 0), _rng(handle, 0, 0xEF, "handle")]))


def le_set_ext_scan_params(own_addr_type=OWN_ADDR_PUBLIC, filter_policy=SCAN_FILTER_ALL, phys=None):
    """phys: dict {PHY_1M: (scan_type, interval, window), PHY_CODED_S8(=3 'Coded'): (...)}; 1M first then Coded on the wire."""
    phys = phys or {PHY_1M: (SCAN_PASSIVE, 0x0010, 0x0010)}
    mask, body = 0, b""
    for phy, bit in ((PHY_1M, 0x01), (3, 0x04)):         # bit0 = 1M, bit2 = Coded (bit1 / 2M is not a scanning PHY)
        if phy in phys:
            st, iv, win = phys[phy]
            body += struct.pack("<BHH", _rng(st, 0, 1, "scan_type"), _rng(iv, 4, 0xFFFF, "interval"), _rng(win, 4, iv, "window"))
            mask |= bit
    if mask == 0:
        raise ValueError("no scanning PHY selected")
    return _cmd("le_set_ext_scan_params", bytes([own_addr_type, filter_policy, mask]) + body)


def le_set_ext_scan_enable(enable=True, filter_duplicates=DUP_FILTER_OFF, duration_10ms=0, period_1p28s=0):
    return _cmd("le_set_ext_scan_enable", struct.pack("<BBHH", 1 if enable else 0, _rng(filter_duplicates, 0, 2, "filter_duplicates"),
                                                      _rng(duration_10ms, 0, 0xFFFF, "duration"), _rng(period_1p28s, 0, 0xFFFF, "period")))


# --- event parsing -----------------------------------------------------------------------------------
def parse_hci_event(pkt):
    """pkt = H4 event packet (04 | evt | plen | params)."""
    if pkt[0] != H4_EVENT:
        raise ValueError("not an H4 event")
    evt, plen = pkt[1], pkt[2]
    params = pkt[3:3 + plen]
    if len(params) != plen:
        raise ValueError("truncated event")
    if evt == EVT_COMMAND_COMPLETE:
        ncmd, op = params[0], struct.unpack_from("<H", params, 1)[0]
        return {"event": "Command Complete", "ncmd": ncmd, "opcode": op, "ret": bytes(params[3:]),
                "status": params[3] if len(params) > 3 else None}
    if evt == EVT_COMMAND_STATUS:
        st, ncmd, op = params[0], params[1], struct.unpack_from("<H", params, 2)[0]
        return {"event": "Command Status", "status": st, "ncmd": ncmd, "opcode": op}
    if evt == EVT_LE_META:
        sub = params[0]
        if sub == 0x02:
            return {"event": LE_SUBEVENTS[0x02], "reports": parse_le_adv_report(params[1:])}
        if sub == 0x0D:
            return {"event": LE_SUBEVENTS[0x0D], "reports": parse_le_ext_adv_report(params[1:])}
        return {"event": LE_SUBEVENTS.get(sub, "LE subevent %#x" % sub), "subevent": sub, "raw": bytes(params[1:])}
    return {"event": "evt %#x" % evt, "raw": bytes(params)}


def parse_le_adv_report(p):
    """LE Advertising Report (subevent 0x02) parameters (after the subevent octet).
    Spec layout = arrays of fields: Num_Reports(1) Event_Type[n] Address_Type[n] Address[n*6] Data_Length[n] Data[sum] RSSI[n].
    For Num_Reports == 1 (what controllers send) this is byte-identical to BlueZ's sequential struct view."""
    n = p[0]
    o = 1
    et = list(p[o:o + n]); o += n
    at = list(p[o:o + n]); o += n
    ad = [bytes(p[o + 6 * i:o + 6 * i + 6]) for i in range(n)]; o += 6 * n
    dl = list(p[o:o + n]); o += n
    data = []
    for L in dl:
        data.append(bytes(p[o:o + L])); o += L
    rssi = list(struct.unpack_from("<%db" % n, p, o)); o += n
    if o != len(p):
        raise ValueError("adv report length mismatch (%d vs %d)" % (o, len(p)))
    return [{"event_type": et[i], "type_name": ADV_REPORT_EVT_TYPE.get(et[i], "?"), "addr_type": at[i],
             "addr": addr_from_wire(ad[i]), "data": data[i], "rssi": rssi[i]} for i in range(n)]


def parse_le_ext_adv_report(p):
    """LE Extended Advertising Report (subevent 0x0D) parameters. Fixed part per report = 24 octets + Data."""
    n = p[0]
    o = 1
    FIX = "<HB6sBBBbbHB6sB"
    fixed = struct.calcsize(FIX)
    assert fixed == 24
    if n != 1:
        # spec arrays-of-fields layout for n>1
        fields = [("event_type", 2), ("addr_type", 1), ("addr", 6), ("pri_phy", 1), ("sec_phy", 1), ("sid", 1),
                  ("tx_power", 1), ("rssi", 1), ("per_interval", 2), ("direct_addr_type", 1), ("direct_addr", 6), ("data_len", 1)]
        cols = {}
        for name, sz in fields:
            cols[name] = [bytes(p[o + sz * i:o + sz * i + sz]) for i in range(n)]
            o += sz * n
        reps = []
        for i in range(n):
            L = cols["data_len"][i][0]
            data = bytes(p[o:o + L]); o += L
            reps.append((i, data))
        out = []
        for i, data in reps:
            out.append({"event_type": int.from_bytes(cols["event_type"][i], "little"), "addr_type": cols["addr_type"][i][0],
                        "addr": addr_from_wire(cols["addr"][i]), "pri_phy": cols["pri_phy"][i][0], "sec_phy": cols["sec_phy"][i][0],
                        "sid": cols["sid"][i][0], "tx_power": struct.unpack("<b", cols["tx_power"][i])[0],
                        "rssi": struct.unpack("<b", cols["rssi"][i])[0],
                        "per_interval": int.from_bytes(cols["per_interval"][i], "little"), "data": data})
        return out
    et, at, ad, pp, sp, sid, txp, rssi, pint, dat, dad, dl = struct.unpack_from(FIX, p, 1)
    data = bytes(p[1 + fixed:1 + fixed + dl])
    if 1 + fixed + dl != len(p):
        raise ValueError("ext adv report length mismatch")
    return [{"event_type": et, "type_name": EXT_REPORT_LEGACY_TYPES.get(et, "extended/other"), "addr_type": at,
             "addr": addr_from_wire(ad), "pri_phy": pp, "sec_phy": sp, "sid": sid, "tx_power": txp, "rssi": rssi,
             "per_interval": pint, "direct_addr_type": dat, "direct_addr": addr_from_wire(dad), "data": data}]


def build_le_adv_report_event(event_type, addr_type, addr, data, rssi):
    p = bytes([0x02, 1, event_type, addr_type]) + addr_to_wire(addr) + bytes([len(data)]) + bytes(data) + struct.pack("<b", rssi)
    return bytes([H4_EVENT, EVT_LE_META, len(p)]) + p


def build_le_ext_adv_report_event(event_type, addr_type, addr, pri_phy, sec_phy, sid, tx_power, rssi, per_interval,
                                  direct_addr_type, direct_addr, data):
    p = bytes([0x0D, 1]) + struct.pack("<HB6sBBBbbHB6sB", event_type, addr_type, addr_to_wire(addr), pri_phy, sec_phy, sid,
                                       tx_power, rssi, per_interval, direct_addr_type, addr_to_wire(direct_addr), len(data)) + bytes(data)
    return bytes([H4_EVENT, EVT_LE_META, len(p)]) + p


def build_command_complete(opcode, status=0, ret_extra=b""):
    p = struct.pack("<BH", 1, opcode) + bytes([status]) + bytes(ret_extra)
    return bytes([H4_EVENT, EVT_COMMAND_COMPLETE, len(p)]) + p


# ==============================================================================================
# 3. AD STRUCTURES
# ==============================================================================================
AD = {
    "flags": 0x01, "uuid16_incomplete": 0x02, "uuid16_complete": 0x03, "uuid32_incomplete": 0x04, "uuid32_complete": 0x05,
    "uuid128_incomplete": 0x06, "uuid128_complete": 0x07, "name_shortened": 0x08, "name_complete": 0x09, "tx_power": 0x0A,
    "peripheral_conn_interval_range": 0x12, "solicit_uuid16": 0x14, "solicit_uuid128": 0x15, "service_data_16": 0x16,
    "appearance": 0x19, "solicit_uuid32": 0x1F, "service_data_32": 0x20, "service_data_128": 0x21, "uri": 0x24,
    "manufacturer": 0xFF,
}
FLAG_LE_LIMITED_DISC, FLAG_LE_GENERAL_DISC, FLAG_BREDR_NOT_SUPPORTED, FLAG_SIMUL_CTRL, FLAG_SIMUL_HOST = 0x01, 0x02, 0x04, 0x08, 0x10
COMPANY_ID_TEST = 0xFFFF           # Assigned Numbers: reserved for internal and interoperability tests
COMPANY_ID_APPLE = 0x004C          # only used for the open iBeacon format
APPEARANCE = {"unknown": 0x0000, "generic_tag": 0x0200, "generic_thermometer": 0x0300, "generic_sensor": 0x0540}
EDDYSTONE_UUID16 = 0xFEAA
ALTBEACON_CODE = 0xBEAC


def ad_structure(ad_type, payload=b""):
    """[len][type][payload]; len counts type+payload (1 octet), so max payload = 254."""
    _rng(ad_type, 0, 0xFF, "ad_type")
    if len(payload) > 254:
        raise ValueError("AD payload > 254")
    return bytes([len(payload) + 1, ad_type]) + bytes(payload)


def ad_flags(le_limited=False, le_general=True, bredr_not_supported=True, simul_ctrl=False, simul_host=False):
    v = (FLAG_LE_LIMITED_DISC if le_limited else 0) | (FLAG_LE_GENERAL_DISC if le_general else 0) | \
        (FLAG_BREDR_NOT_SUPPORTED if bredr_not_supported else 0) | (FLAG_SIMUL_CTRL if simul_ctrl else 0) | \
        (FLAG_SIMUL_HOST if simul_host else 0)
    return ad_structure(AD["flags"], bytes([v]))


def ad_name(name, shortened=False):
    return ad_structure(AD["name_shortened" if shortened else "name_complete"], name.encode("utf-8"))


def uuid16_wire(u):
    return struct.pack("<H", _rng(u, 0, 0xFFFF, "uuid16"))


def uuid32_wire(u):
    return struct.pack("<I", _rng(u, 0, 0xFFFFFFFF, "uuid32"))


def uuid128_wire(u):
    """canonical '12345678-9abc-def0-1234-56789abcdef0' -> 16 octets, LITTLE endian (reversed)."""
    h = u.replace("-", "").replace("{", "").replace("}", "")
    if len(h) != 32:
        raise ValueError("bad uuid %r" % u)
    return bytes.fromhex(h)[::-1]


def ad_uuids16(uuids, complete=True):
    return ad_structure(AD["uuid16_complete" if complete else "uuid16_incomplete"], b"".join(uuid16_wire(u) for u in uuids))


def ad_uuids32(uuids, complete=True):
    return ad_structure(AD["uuid32_complete" if complete else "uuid32_incomplete"], b"".join(uuid32_wire(u) for u in uuids))


def ad_uuids128(uuids, complete=True):
    return ad_structure(AD["uuid128_complete" if complete else "uuid128_incomplete"], b"".join(uuid128_wire(u) for u in uuids))


def ad_service_data16(uuid16, data):
    return ad_structure(AD["service_data_16"], uuid16_wire(uuid16) + bytes(data))


def ad_service_data32(uuid32, data):
    return ad_structure(AD["service_data_32"], uuid32_wire(uuid32) + bytes(data))


def ad_service_data128(uuid128, data):
    return ad_structure(AD["service_data_128"], uuid128_wire(uuid128) + bytes(data))


def ad_manufacturer(company_id, data=b""):
    return ad_structure(AD["manufacturer"], struct.pack("<H", _rng(company_id, 0, 0xFFFF, "company_id")) + bytes(data))


def ad_tx_power(dbm):
    return ad_structure(AD["tx_power"], struct.pack("<b", _rng(dbm, -127, 127, "tx_power dBm")))


def ad_appearance(value):
    return ad_structure(AD["appearance"], struct.pack("<H", _rng(value, 0, 0xFFFF, "appearance")))


def ad_conn_interval_range(min_ms, max_ms):
    """Peripheral connection interval range, units 1.25 ms, valid 0x0006..0x0C80 (7.5 ms..4 s); 0xFFFF = no specific."""
    umin, umax = round(min_ms / 1.25), round(max_ms / 1.25)
    if abs(umin * 1.25 - min_ms) > 1e-9 or abs(umax * 1.25 - max_ms) > 1e-9:
        raise ValueError("connection intervals must be multiples of 1.25 ms")
    _rng(umin, 6, 0x0C80, "min"); _rng(umax, umin, 0x0C80, "max")
    return ad_structure(AD["peripheral_conn_interval_range"], struct.pack("<HH", umin, umax))


def ad_parse(payload):
    """Split an advertising payload into [(type, data)], stopping at zero-length padding. Validates lengths."""
    out, i = [], 0
    payload = bytes(payload)
    while i < len(payload):
        L = payload[i]
        if L == 0:
            if any(payload[i:]):
                raise ValueError("non-zero octets after zero-length AD structure")
            break
        if i + 1 + L > len(payload):
            raise ValueError("AD structure at %d overruns payload" % i)
        out.append((payload[i + 1], payload[i + 2:i + 1 + L]))
        i += 1 + L
    return out


def check_legacy_len(payload):
    """Enforce the 31-octet legacy ADV_IND/ADV_NONCONN_IND/ADV_SCAN_IND/SCAN_RSP payload limit. Returns bytes left."""
    if len(payload) > MAX_LEGACY_ADV_DATA:
        raise ValueError("legacy advertising payload is %d octets (> 31)" % len(payload))
    ad_parse(payload)
    return MAX_LEGACY_ADV_DATA - len(payload)


def _finish(parts, flags):
    p = (ad_flags() if flags is True else (flags or b"")) + b"".join(parts)
    check_legacy_len(p)
    return p


# --- ready-made payload builders ------------------------------------------------------------------------
def build_ibeacon(uuid, major, minor, measured_power_dbm, flags=True):
    """Apple iBeacon open format: FF 4C00 02 15 <UUID BE 16> <major BE 2> <minor BE 2> <power int8>. 30 octets with Flags."""
    uuid_be = bytes.fromhex(uuid.replace("-", ""))
    if len(uuid_be) != 16:
        raise ValueError("bad uuid")
    body = bytes([0x02, 0x15]) + uuid_be + struct.pack(">HH", _rng(major, 0, 0xFFFF, "major"), _rng(minor, 0, 0xFFFF, "minor")) + \
        struct.pack("<b", _rng(measured_power_dbm, -128, 127, "measured_power"))
    assert len(body) == 23
    return _finish([ad_manufacturer(COMPANY_ID_APPLE, body)], flags)


def build_eddystone_uid(namespace10, instance6, tx_power_0m_dbm, flags=True):
    """Eddystone-UID: svc-data AA FE | 00 | txpwr@0m | ns(10) | inst(6) | 00 00  (31 octets with Flags + UUID list)."""
    ns, ins = bytes(namespace10), bytes(instance6)
    if len(ns) != 10 or len(ins) != 6:
        raise ValueError("namespace must be 10 octets, instance 6")
    frame = bytes([0x00]) + struct.pack("<b", tx_power_0m_dbm) + ns + ins + b"\x00\x00"
    return _finish([ad_uuids16([EDDYSTONE_UUID16]), ad_service_data16(EDDYSTONE_UUID16, frame)], flags)


_EDDY_SCHEMES = {"http://www.": 0, "https://www.": 1, "http://": 2, "https://": 3}
_EDDY_EXPAND = [(".com/", 0), (".org/", 1), (".edu/", 2), (".net/", 3), (".info/", 4), (".biz/", 5), (".gov/", 6),
                (".com", 7), (".org", 8), (".edu", 9), (".net", 10), (".info", 11), (".biz", 12), (".gov", 13)]


def eddystone_url_encode(url):
    scheme = None
    for pref in sorted(_EDDY_SCHEMES, key=len, reverse=True):
        if url.startswith(pref):
            scheme, rest = _EDDY_SCHEMES[pref], url[len(pref):]
            break
    if scheme is None:
        raise ValueError("URL must start with http://www. https://www. http:// or https://")
    out, i = b"", 0
    while i < len(rest):
        for txt, code in _EDDY_EXPAND:
            if rest.startswith(txt, i):
                out += bytes([code]); i += len(txt); break
        else:
            c = ord(rest[i])
            if not 0x21 <= c <= 0x7E:
                raise ValueError("illegal URL character %r" % rest[i])
            out += bytes([c]); i += 1
    if len(out) > 17:
        raise ValueError("encoded URL is %d octets (> 17)" % len(out))
    return bytes([scheme]) + out


def build_eddystone_url(url, tx_power_0m_dbm, flags=True):
    frame = bytes([0x10]) + struct.pack("<b", tx_power_0m_dbm) + eddystone_url_encode(url)
    return _finish([ad_uuids16([EDDYSTONE_UUID16]), ad_service_data16(EDDYSTONE_UUID16, frame)], flags)


def build_eddystone_tlm(vbatt_mv, temp_c, adv_count, sec_count_100ms, flags=True):
    """Unencrypted TLM v0: 20 00 | vbatt BE16 mV | temp 8.8 fixed BE16 (0x8000 n/a) | adv_cnt BE32 | uptime BE32 (0.1 s)."""
    temp = 0x8000 if temp_c is None else (int(round(temp_c * 256)) & 0xFFFF)
    frame = bytes([0x20, 0x00]) + struct.pack(">HHII", _rng(vbatt_mv, 0, 0xFFFF, "vbatt"), temp, adv_count & 0xFFFFFFFF, sec_count_100ms & 0xFFFFFFFF)
    return _finish([ad_uuids16([EDDYSTONE_UUID16]), ad_service_data16(EDDYSTONE_UUID16, frame)], flags)


def build_altbeacon(mfg_id, id1_uuid, id2, id3, ref_rssi_dbm, mfg_reserved=0, flags=True):
    """AltBeacon: FF <mfg LE16> BE AC <ID 20: uuid16 BE + id2 BE16 + id3 BE16> <ref RSSI @1m int8> <mfg rsvd>. 31 octets with Flags."""
    uuid_be = bytes.fromhex(id1_uuid.replace("-", ""))
    if len(uuid_be) != 16:
        raise ValueError("bad id1 uuid")
    body = struct.pack(">H", ALTBEACON_CODE) + uuid_be + struct.pack(">HH", id2, id3) + struct.pack("<bB", ref_rssi_dbm, mfg_reserved)
    assert len(body) == 2 + 20 + 2
    return _finish([ad_manufacturer(mfg_id, body)], flags)


SENSOR_FORMAT_VERSION = 0x01


def build_sensor_payload(sensor_id, seq, temp_c=None, humidity_pct=None, battery_pct=None, company_id=COMPANY_ID_TEST, name=None, flags=True):
    """Synthetic demo format (NOT a registered protocol), manufacturer data under company 0xFFFF:
       ver(1)=0x01 sensor_id(1) seq(1) temp int16 LE 0.01C (0x8000=n/a) hum uint16 LE 0.01% (0xFFFF=n/a) batt uint8 % (0xFF=n/a) = 8 octets."""
    if temp_c is None:
        t = 0x8000
    else:
        t = _rng(int(round(temp_c * 100)), -32767, 32767, "temp (0.01 C)") & 0xFFFF
    h = 0xFFFF if humidity_pct is None else _rng(int(round(humidity_pct * 100)), 0, 10000, "humidity (0.01 %)")
    b = 0xFF if battery_pct is None else _rng(battery_pct, 0, 100, "battery %")
    body = struct.pack("<BBBHHB", SENSOR_FORMAT_VERSION, sensor_id & 0xFF, seq & 0xFF, t, h, b)
    assert len(body) == 8
    parts = [ad_manufacturer(company_id, body)]
    if name:
        parts.append(ad_name(name))
    return _finish(parts, flags)


# ==============================================================================================
# 4. INTERFERENCE PHYSICS
# ==============================================================================================
T_IFS_US = 150
DTM_ACCESS_ADDRESS = 0x71764129          # Direct Test Mode access address (Vol 6 Part F)
ADV_ACCESS_ADDRESS = 0x8E89BED6          # advertising physical channel access address
CRC_INIT_ADV_AND_DTM = 0x555555


def dtm_packet_airtime_us(test_data_length, phy=PHY_1M):
    """Air time L (us) of an LE Uncoded test packet = preamble + AA(4) + header(2) + payload + CRC(3).
       LE 1M: preamble 1 octet @ 8 us/octet; LE 2M: preamble 2 octets @ 4 us/octet."""
    _rng(test_data_length, 0, 255, "test_data_length")
    if phy == PHY_1M:
        return (1 + 4 + 2 + test_data_length + 3) * 8
    if phy == PHY_2M:
        return (2 + 4 + 2 + test_data_length + 3) * 4
    raise NotImplementedError("Coded PHY not modelled")


def dtm_packet_interval_us(test_data_length, phy=PHY_1M):
    """Core Vol 6 Part F, Transmitter Test packet interval (uncoded PHYs): I(L) = ceil((L + 249 us) / 625 us) * 625 us."""
    L = dtm_packet_airtime_us(test_data_length, phy)
    return math.ceil((L + 249) / 625.0) * 625


def dtm_duty_cycle(test_data_length, phy=PHY_1M):
    return dtm_packet_airtime_us(test_data_length, phy) / float(dtm_packet_interval_us(test_data_length, phy))


def dtm_best_length_for_duty(phy=PHY_1M):
    best = max(range(0, 256), key=lambda n: (dtm_duty_cycle(n, phy), n))
    return best, dtm_duty_cycle(best, phy)


def adv_pdu_airtime_us(adv_data_len, phy=PHY_1M, pdu="ADV_IND"):
    """Legacy advertising-channel packet air time (1M): preamble1 + AA4 + hdr2 + payload + CRC3 octets, 8 us/octet."""
    payload = {"ADV_IND": 6 + adv_data_len, "ADV_NONCONN_IND": 6 + adv_data_len, "ADV_SCAN_IND": 6 + adv_data_len,
               "SCAN_RSP": 6 + adv_data_len, "ADV_DIRECT_IND": 12, "SCAN_REQ": 12, "CONNECT_IND": 34}[pdu]
    if pdu in ("ADV_IND", "ADV_NONCONN_IND", "ADV_SCAN_IND", "SCAN_RSP"):
        _rng(adv_data_len, 0, 31, "adv_data_len")
    return (1 + 4 + 2 + payload + 3) * 8


def survival_probability(victim_airtime_us, dtm_len, phy=PHY_1M):
    """P(a victim packet at random phase fits entirely in the gap between periodic test packets) - no capture effect,
       same RF channel, equal-or-stronger interferer.  = max(0, I - L - victim) / I."""
    L, I = dtm_packet_airtime_us(dtm_len, phy), dtm_packet_interval_us(dtm_len, phy)
    return max(0.0, (I - L - victim_airtime_us)) / I


WIFI_OFDM_OCCUPIED_MHZ = 52 * 0.3125 + 0.3125     # 52 used subcarriers + DC null spacing = 16.5625 MHz
def wifi_power_in_ble_channel_db(occupied_mhz=WIFI_OFDM_OCCUPIED_MHZ, ble_bw_mhz=2.0):
    """dB below the total Wi-Fi power seen inside one 2 MHz BLE channel for a flat OFDM spectrum."""
    return 10 * math.log10(occupied_mhz / ble_bw_mhz)


# ==============================================================================================
# markdown generation helpers (used by --tables / --fill-md)
# ==============================================================================================
def md_channel_table():
    rows = ["| RF ch k (= HCI TX/RX_Channel) | LL index | MHz | Role | Wi-Fi 20 MHz ch (1-13) whose +/-10 MHz span contains it |",
            "|---:|---:|---:|:--|:--|"]
    for c in CHANNELS:
        rows.append("| %d (0x%02X) | %d | %d | %s | %s |" % (c["rf"], c["rf"], c["ll"], c["mhz"],
                    "**adv**" if c["role"] == "adv" else "data", _ranges(ble_rf_channel_wifi_overlaps(c["rf"]))))
    return "\n".join(rows)


def md_wifi_table():
    rows = ["| Wi-Fi ch | fc MHz | +/-10 MHz span | BLE RF ch k | #BLE ch | adv channels hit |", "|---:|---:|:--|:--|---:|:--|"]
    for w in range(1, 14):
        fc = WIFI_2G4_CENTER_MHZ[w]
        ks = wifi_overlap_rf_channels(w)
        adv = [RF_TO_ADV_LL[k] for k in ks if k in RF_TO_ADV_LL]
        rows.append("| %d | %d | %d-%d | %s | %d | %s |" % (w, fc, fc - 10, fc + 10, _ranges(ks), len(ks), ", ".join(map(str, adv)) or "none"))
    return "\n".join(rows)


def md_duty_table():
    rows = ["| Test_Data_Length | 1M: air time us | 1M: interval us | 1M: duty | 2M: air time us | 2M: interval us | 2M: duty |", "|---:|---:|---:|---:|---:|---:|---:|"]
    for n in (0, 10, 37, 100, 150, 193, 239, 255):
        rows.append("| %d | %d | %d | %.1f %% | %d | %d | %.1f %% |" % (
            n, dtm_packet_airtime_us(n, 1), dtm_packet_interval_us(n, 1), 100 * dtm_duty_cycle(n, 1),
            dtm_packet_airtime_us(n, 2), dtm_packet_interval_us(n, 2), 100 * dtm_duty_cycle(n, 2)))
    return "\n".join(rows)


def md_survival_table():
    rows = ["| Interferer (DTM TX on the SAME RF channel) | air / interval | duty | legacy ADV_IND 31 B (376 us) survives | SCAN_REQ (176 us) | ADV_IND 0 B (128 us) |",
            "|:--|:--|---:|---:|---:|---:|"]
    for phy, n in ((1, 37), (1, 100), (1, 193), (1, 255), (2, 37), (2, 239), (2, 255)):
        L, I = dtm_packet_airtime_us(n, phy), dtm_packet_interval_us(n, phy)
        rows.append("| LE %dM, Test_Data_Length %d | %d / %d us | %.1f %% | %.2f %% | %.1f %% | %.1f %% |" % (
            phy, n, L, I, 100.0 * L / I, 100 * survival_probability(376, n, phy), 100 * survival_probability(176, n, phy),
            100 * survival_probability(128, n, phy)))
    return "\n".join(rows)


def _payload_examples():
    return [
        ("Flags only (LE general discoverable, no BR/EDR)", ad_flags()),
        ("Flags + Complete Local Name 'BluForge-01'", ad_flags() + ad_name("BluForge-01")),
        ("iBeacon (UUID 12345678-9abc-def0-1234-56789abcdef0, major 1, minor 2, -59 dBm @1 m)", build_ibeacon("12345678-9abc-def0-1234-56789abcdef0", 1, 2, -59)),
        ("Eddystone-UID (ns edd1ebeac04e5defa017, inst 000000000001, -21 dBm @0 m)", build_eddystone_uid(bytes.fromhex("edd1ebeac04e5defa017"), bytes.fromhex("000000000001"), -21)),
        ("Eddystone-URL https://www.example.com/ (-20 dBm @0 m)", build_eddystone_url("https://www.example.com/", -20)),
        ("Eddystone-TLM (3000 mV, 22.5 C, 1000 adv, 3600.0 s)", build_eddystone_tlm(3000, 22.5, 1000, 36000)),
        ("AltBeacon (mfg 0xFFFF, id 8492e75f-4fd6-469d-b132-043fe94921d8 / 1 / 2, -59 dBm)", build_altbeacon(COMPANY_ID_TEST, "8492E75F-4FD6-469D-B132-043FE94921D8", 1, 2, -59)),
        ("Sensor (company 0xFFFF, id 7, seq 42, -5.25 C, 55.5 %RH, 87 %, name 'S7')", build_sensor_payload(7, 42, temp_c=-5.25, humidity_pct=55.5, battery_pct=87, name="S7")),
        ("Service Data 0x181A + 2 octets, Tx Power +4 dBm, Appearance Generic Sensor", ad_flags() + ad_service_data16(0x181A, b"\x01\x02") + ad_tx_power(4) + ad_appearance(APPEARANCE["generic_sensor"])),
        ("Peripheral conn-interval range 30-50 ms + 16-bit UUID list 0x180F,0x180A", ad_conn_interval_range(30, 50) + ad_uuids16([0x180F, 0x180A])),
    ]


def md_payload_table():
    rows = ["| Payload | Octets | Free (of 31) | Hex |", "|:--|---:|---:|:--|"]
    for label, pl in _payload_examples():
        rows.append("| %s | %d | %d | `%s` |" % (label, len(pl), check_legacy_len(pl), pl.hex(" ")))
    return "\n".join(rows)


def md_opcode_table():
    rows = ["| Command | Opcode | OGF | OCF | Parameters (octets) | Return parameters |", "|:--|:--|:--|:--|:--|:--|"]
    for k, c in sorted(COMMANDS.items(), key=lambda kv: kv[1]["opcode"]):
        rows.append("| %s | `0x%04X` | `0x%02X` | `0x%04X` | %s | %s |" % (c["name"], c["opcode"], c["ogf"], c["ocf"], c["params"], c["ret"]))
    return "\n".join(rows)


def md_event_mask_table():
    rows = ["| LE Event Mask bit | Subevent code | Name |", "|---:|:--|:--|"]
    for code, name in sorted(LE_SUBEVENTS.items()):
        rows.append("| %d | `0x%02X` | %s |" % (code - 1, code, name))
    return "\n".join(rows)


def md_golden_table():
    rows = ["| Packet | H4 bytes |", "|:--|:--|"]
    for label, pkt in _golden_vectors():
        rows.append("| %s | `%s` |" % (label, pkt.hex(" ")))
    return "\n".join(rows)


_BLOCKS = {"channels": md_channel_table, "wifi": md_wifi_table, "duty": md_duty_table, "opcodes": md_opcode_table,
           "eventmask": md_event_mask_table, "golden": md_golden_table, "survival": md_survival_table, "payloads": md_payload_table}


def fill_markdown(path, extra_blocks=None):
    txt = open(path, encoding="utf-8").read()
    blocks = dict(_BLOCKS)
    blocks.update(extra_blocks or {})
    for name, fn in blocks.items():
        pat = re.compile(r"(<!-- BEGIN:%s -->\n).*?(\n<!-- END:%s -->)" % (re.escape(name), re.escape(name)), re.S)
        body = fn() if callable(fn) else fn
        txt, n = pat.subn(lambda m: m.group(1) + body + m.group(2), txt)
        if n != 1:
            raise RuntimeError("markdown block %s found %d times" % (name, n))
    open(path, "w", encoding="utf-8").write(txt)


# ==============================================================================================
# SELF-TEST
# ==============================================================================================
def _golden_vectors():
    ADDR = "C0:11:22:33:44:55"
    return [
        ("Reset", reset()),
        ("Set Event Mask (default)", set_event_mask()),
        ("LE Set Event Mask (0x1F)", le_set_event_mask(0x1F)),
        ("LE Set Random Address C0:11:22:33:44:55", le_set_random_address(ADDR)),
        ("LE Set Advertising Parameters (defaults: 0x0800, ADV_IND, ch map 7)", le_set_adv_params()),
        ("LE Set Advertising Data 02 01 06", le_set_adv_data(bytes.fromhex("020106"))),
        ("LE Set Advertising Enable = 1", le_set_adv_enable(True)),
        ("LE Set Scan Parameters passive 10ms/10ms", le_set_scan_params()),
        ("LE Set Scan Enable = 1, dup filter off", le_set_scan_enable(True, 0)),
        ("LE Transmitter Test v1 ch=19 (2440) len=37 PRBS9", le_transmitter_test_v1(19, 37, 0)),
        ("LE Transmitter Test v2 ch=0 len=255 all-ones LE 2M", le_transmitter_test_v2(0, 255, DTM_PAYLOAD_BY_OCTET[0xFF], PHY_2M)),
        ("LE Transmitter Test v4 ch=12 len=37 PRBS9 1M -10 dBm", le_transmitter_test_v4(12, 37, 0, PHY_1M, tx_power_dbm=-10)),
        ("LE Receiver Test v1 ch=39 (2480)", le_receiver_test_v1(39)),
        ("LE Receiver Test v2 ch=12 1M std modulation", le_receiver_test_v2(12, PHY_1M, 0)),
        ("LE Test End", le_test_end()),
        ("LE Set Ext Adv Params handle0 legacy ADV_IND 30ms", le_set_ext_adv_params(0, 0x0013, 0x30, 0x30)),
        ("LE Set Ext Adv Data handle0 complete 02 01 06", le_set_ext_adv_data(0, bytes.fromhex("020106"))),
        ("LE Set Ext Adv Enable handle0 until disabled", le_set_ext_adv_enable(True, [(0, 0, 0)])),
        ("LE Set Adv Set Random Address handle0", le_set_adv_set_random_address(0, ADDR)),
        ("LE Set Ext Scan Params 1M passive 10ms/10ms", le_set_ext_scan_params()),
        ("LE Set Ext Scan Enable continuous", le_set_ext_scan_enable(True)),
        ("LE Set Periodic Adv Params handle0 7.5-20ms", le_set_periodic_adv_params(0)),
        ("LE Set Periodic Adv Data handle0 complete", le_set_periodic_adv_data(0, bytes.fromhex("020106"))),
        ("LE Set Periodic Adv Enable handle0", le_set_periodic_adv_enable(True, 0)),
    ]


_GOLDEN_HEX = [
    "01 03 0c 00",
    "01 01 0c 08 ff ff ff ff ff 1f 00 00",
    "01 01 20 08 1f 00 00 00 00 00 00 00",
    "01 05 20 06 55 44 33 22 11 c0",
    "01 06 20 0f 00 08 00 08 00 00 00 00 00 00 00 00 00 07 00",
    "01 08 20 20 03 02 01 06" + " 00" * 28,
    "01 0a 20 01 01",
    "01 0b 20 07 00 10 00 10 00 00 00",
    "01 0c 20 02 01 00",
    "01 1e 20 03 13 25 00",
    "01 34 20 04 00 ff 04 02",
    "01 7b 20 08 0c 25 00 01 00 00 00 f6",
    "01 1d 20 01 27",
    "01 33 20 03 0c 01 00",
    "01 1f 20 00",
    "01 36 20 19 00 13 00 30 00 00 30 00 00 07" + " 00" * 9 + " 7f 01 00 01 00 00",
    "01 37 20 07 00 03 01 03 02 01 06",
    "01 39 20 06 01 01 00 00 00 00",
    "01 35 20 07 00 55 44 33 22 11 c0",
    "01 41 20 08 00 00 01 00 10 00 10 00",
    "01 42 20 06 01 00 00 00 00 00",
    "01 3e 20 07 00 06 00 10 00 00 00",
    "01 3f 20 06 00 03 03 02 01 06",
    "01 40 20 02 01 00",
]


def _kernel_header():
    import glob
    c = sorted(glob.glob("/usr/src/kernels/*/include/net/bluetooth/hci.h"))
    return c[-1] if c else None


def _btsnoop(packets):
    """packets: list of (opcode_mon, data). Monitor-format btsnoop (datalink 2001), index 0."""
    out = b"btsnoop\0" + struct.pack(">II", 1, 2001)
    ts = 0x00E03AB44A676000
    for i, (mop, data) in enumerate(packets):
        out += struct.pack(">IIIIQ", len(data), len(data), mop, 0, ts + i * 1000) + data
    return out


# Documented limitations of BlueZ 5.77 btmon's decoder (NOT errors in our layouts); opcode + plen still compared.
_BTMON_QUIRKS = {
    0x207B: "LE Transmitter Test [v4]: btmon 5.77 has no decoder (prints 'Unknown (0x08|0x007b)'); opcode/plen still parsed",
    0x2050: "LE Transmitter Test [v3]: btmon 5.77 decodes it with the RECEIVER-v3 field labels (CTE/slot-duration/antenna count) and says "
            "'too short packet' for 7 octets; spec layout (Core): ..., CTE_Type, Switching_Pattern_Length, Antenna_IDs[i]",
}


def btmon_crosscheck(workdir, log):
    """Feed our command bytes + sample events to `btmon -r` (offline decoder, no hardware) and compare
       opcode (OGF|OCF) and plen with our tables."""
    exe = shutil.which("btmon")
    if not exe:
        log("btmon crosscheck SKIPPED (btmon not found)")
        return None
    os.makedirs(workdir, exist_ok=True)
    pk = [(0, bytes([0, 1]) + bytes(6) + b"hci0\0\0\0\0")]
    sent = []
    cmds = [(label, pkt) for label, pkt in _golden_vectors()]
    cmds += [("LE Read Local Supported Features", hci_command(OPCODE["le_read_local_features"])),
             ("LE Receiver Test v3", hci_command(OPCODE["le_receiver_test_v3"], bytes([19, 1, 0, 0, 0, 1, 0]))),
             ("LE Transmitter Test v3", le_transmitter_test_v3(19, 37, 0, PHY_1M)),
             ("LE Remove Adv Set", le_remove_adv_set(0)), ("LE Clear Adv Sets", le_clear_adv_sets()),
             ("LE Read Max Adv Data Len", hci_command(OPCODE["le_read_max_adv_data_len"])),
             ("LE Read Num Adv Sets", hci_command(OPCODE["le_read_num_adv_sets"])),
             ("LE Read Transmit Power", hci_command(OPCODE["le_read_transmit_power"])),
             ("Read Local Supported Commands", hci_command(OPCODE["read_local_supported_commands"])),
             ("LE Read Supported States", hci_command(OPCODE["le_read_supported_states"])),
             ("Set Event Mask incl. LE Meta bit 61", set_event_mask(EVENT_MASK_HOST_TYPICAL)),
             ("LE Set Ext Scan Response Data handle0 complete", le_set_ext_scan_rsp_data(0, bytes.fromhex("020106"))),
             ("LE Set Scan Response Data 'BluForge'", le_set_scan_rsp_data(ad_name("BluForge")))]
    covered = {struct.unpack_from("<H", pkt, 1)[0] for _, pkt in cmds}
    _check(covered == set(OPCODE.values()), "crosscheck does not cover opcodes: %r" % sorted(hex(o) for o in set(OPCODE.values()) - covered))
    for label, pkt in cmds:
        pk.append((2, pkt[1:]))
        sent.append((struct.unpack_from("<H", pkt, 1)[0], pkt[3], label))
    ev = [build_le_adv_report_event(0x03, 1, "C0:11:22:33:44:55", ad_flags() + ad_name("Demo"), -61),
          build_le_ext_adv_report_event(0x0013, 1, "C0:11:22:33:44:55", 1, 0, 0xFF, 127, -55, 0, 0, "00:00:00:00:00:00", bytes.fromhex("020106")),
          build_command_complete(OPCODE["le_set_adv_enable"], 0x0C),
          build_command_complete(OPCODE["le_test_end"], 0x00, struct.pack("<H", 1234))]
    for e in ev:
        pk.append((3, e[1:]))
    n_fixed_events = len(ev)
    status_codes = sorted(HCI_STATUS)
    for st in status_codes:                                   # Command Complete with each status code
        pk.append((3, build_command_complete(OPCODE["le_set_adv_enable"], st)[1:]))
    sub_codes = sorted(LE_SUBEVENTS)
    for code in sub_codes:                                    # LE Meta events with dummy bodies: only the header name is compared
        body = bytes([code]) + bytes(40)
        pk.append((3, bytes([EVT_LE_META, len(body)]) + body))
    mask_bits = (1, 12, 17, 18, 19)
    for bit in mask_bits:                                     # LE Set Event Mask with single bits
        pk.append((2, le_set_event_mask(1 << bit)[1:]))
    path = os.path.join(workdir, "ble_constants_crosscheck.btsnoop")
    with open(path, "wb") as f:
        f.write(_btsnoop(pk))
    r = subprocess.run([exe, "-r", path, "-P", "-c", "never"], capture_output=True, text=True, timeout=30)
    out = r.stdout
    with open(os.path.join(workdir, "ble_constants_crosscheck.btmon.txt"), "w") as f:
        f.write(out)
    seen = re.findall(r"< HCI Command: (.*?) \(0x([0-9a-f]{2})\|0x([0-9a-f]{4})\) plen (\d+)", out)
    _check(len(seen) == len(sent) + len(mask_bits), "btmon decoded %d commands, we sent %d" % (len(seen), len(sent) + len(mask_bits)))
    seen = seen[:len(sent)]
    bad = []
    for (op, plen, label), (bname, ogf, ocf, bplen) in zip(sent, seen):
        bop = (int(ogf, 16) << 10) | int(ocf, 16)
        if bop != op or int(bplen) != plen:
            bad.append((label, hex(op), hex(bop), plen, bplen))
        if "Unknown" in bname and op not in _BTMON_QUIRKS:
            bad.append((label, "btmon says unknown: " + bname))
    _check(not bad, "btmon mismatches: %r" % bad)
    log("btmon -r (BlueZ %s) decoded all %d of our command packets (covering all %d registry opcodes): OGF|OCF and plen agree, none 'Unknown' except documented v4." %
        (re.search(r"ver ([\d.]+)", out).group(1) if re.search(r"ver ([\d.]+)", out) else "?", len(seen), len(OPCODE)))
    # ---- statuses
    st_lines = re.findall(r"Status: (.*?) \(0x([0-9a-f]{2})\)", out)
    st_map = {}
    for nm, hx in st_lines:
        st_map.setdefault(int(hx, 16), nm)
    miss = [hex(c) for c in status_codes if c not in st_map or st_map[c].strip().lower() == "unknown"]
    _check(not miss, "btmon does not know status codes %r" % miss)
    diff = {hex(c): (HCI_STATUS[c], st_map[c]) for c in status_codes if st_map[c].lower() != HCI_STATUS[c].lower()}
    log("  btmon status names: %d/%d identical to ours%s" % (len(status_codes) - len(diff), len(status_codes),
        ("; differing wording: %r" % diff) if diff else ""))
    # ---- LE subevent names, in order of appearance after the fixed events
    sub_names = re.findall(r"^\s+(LE [A-Za-z0-9 \-]+?) \(0x([0-9a-f]{2})\)$", out, re.M)
    sub_seen = {}
    for nm, hx in sub_names:
        sub_seen[int(hx, 16)] = nm
    bad_sub = [hex(c) for c in sub_codes if c not in sub_seen or sub_seen[c].strip().lower() == "unknown"]
    _check(not bad_sub, "btmon does not know LE subevent codes %r" % bad_sub)
    sdiff = {hex(c): (LE_SUBEVENTS[c], sub_seen[c]) for c in sub_codes if sub_seen[c].lower() != LE_SUBEVENTS[c].lower()}
    log("  btmon LE subevent names 0x01..0x14: all %d codes recognised; %d identical wording%s" % (
        len(sub_codes), len(sub_codes) - len(sdiff), ("; differing wording: %r" % sdiff) if sdiff else ""))
    # ---- LE event mask single-bit decode: bit n <-> subevent n+1
    chunks = out.split("< HCI Command: LE Set Event Mask")
    got = []
    for ch in chunks[-len(mask_bits):]:
        m = re.search(r"Mask: 0x([0-9a-f]+)\n\s+(LE [A-Za-z0-9 \-]+)", ch)
        got.append((int(m.group(1), 16), m.group(2).strip()) if m else None)
    for bit, g in zip(mask_bits, got):
        _check(g is not None and g[0] == 1 << bit, "mask decode %d" % bit)
    log("  btmon LE Set Event Mask single-bit decode: " + "; ".join("bit %d -> %s" % (b, g[1]) for b, g in zip(mask_bits, got)))
    for op, why in _BTMON_QUIRKS.items():
        log("  btmon quirk (informational): " + why)
    # spot-check text of events and TX test fields
    m61 = re.search(r"Mask: 0x2000[0-9a-f]{12}\n(?:\s+.*\n)*?\s+LE Meta\n", out)
    _check(m61 is not None, "btmon does not name Set Event Mask bit 61 as 'LE Meta'")
    log("  btmon decodes Set Event Mask bit 61 (0x2000000000000000) as 'LE Meta'.")
    for needle in ("TX frequency: 2440 MHz (0x13)", "Test data length: 37 bytes", "Status: Command Disallowed (0x0c)",
                   "Number of packets: 1234", "Event type: Non connectable undirected - ADV_NONCONN_IND (0x03)"):
        if needle not in out:
            log("  btmon text NOT found (informational): %r" % needle)
        else:
            log("  btmon text ok: %r" % needle)
    return out


def selftest(verbose=True, workdir=None, check_md=None):
    lines = []

    def log(s):
        lines.append(s)
        if verbose:
            print(s)

    _N_CHECKS[0] = 0
    # ---- 1. channel math ----
    _check(len(CHANNELS) == 40, "40 channels")
    _check(sorted(c["rf"] for c in CHANNELS) == list(range(40)), "rf ids")
    _check(sorted(c["ll"] for c in CHANNELS) == list(range(40)), "ll ids are a permutation of 0..39")
    _check(sum(1 for c in CHANNELS if c["role"] == "adv") == 3 and sum(1 for c in CHANNELS if c["role"] == "data") == 37, "3 adv + 37 data")
    for ll in range(40):
        rf = ll_index_to_rf_channel(ll)
        _check(rf_channel_to_ll_index(rf) == ll, "roundtrip ll %d" % ll)
        if ll <= 10:
            _check(freq_mhz(rf) == 2404 + 2 * ll, "spec formula data 0-10")
        elif ll <= 36:
            _check(freq_mhz(rf) == 2428 + 2 * (ll - 11), "spec formula data 11-36")
    _check([ll_index_to_freq_mhz(i) for i in (37, 38, 39)] == [2402, 2426, 2480], "adv freqs")
    _check([ll_index_to_rf_channel(i) for i in (37, 38, 39)] == [0, 12, 39], "adv RF channels")
    _check(ll_index_to_rf_channel(0) == 1 and ll_index_to_rf_channel(10) == 11 and ll_index_to_rf_channel(11) == 13
           and ll_index_to_rf_channel(36) == 38, "data boundaries")
    _check(freq_mhz(12) == 2426 and freq_mhz(39) == 2480 and freq_mhz(0) == 2402, "freqs")
    _check(rf_channel_from_freq_mhz(2440) == 19 and hci_test_channel_to_freq_mhz(19) == 2440, "TX_Channel 19 = 2440 MHz")
    _check(hci_test_channel_to_freq_mhz(37) == 2476 and hci_test_channel_to_freq_mhz(38) == 2478, "GOTCHA: TX_Channel 37/38 are 2476/2478 MHz, not adv")
    _check(hci_test_channel_from_ll_index(37) == 0 and hci_test_channel_from_ll_index(38) == 12 and hci_test_channel_from_ll_index(39) == 39, "adv LL->TX_Channel")
    for bad in (-1, 40):
        try:
            freq_mhz(bad); _check(False, "range check")
        except ValueError:
            pass
    try:
        rf_channel_from_freq_mhz(2403); _check(False, "odd mhz")
    except ValueError:
        pass
    _check(wifi_overlap_rf_channels(1) == list(range(0, 11)), "Wi-Fi ch1 -> BLE RF 0-10")
    _check(wifi_overlap_rf_channels(6) == list(range(13, 23)), "Wi-Fi ch6 -> BLE RF 13-22")
    _check(wifi_overlap_rf_channels(11) == list(range(25, 36)), "Wi-Fi ch11 -> BLE RF 25-35")
    _check(wifi_overlap_rf_channels(13) == list(range(30, 40)), "Wi-Fi ch13 -> BLE RF 30-39")
    _check(all(len(wifi_overlap_rf_channels(w)) in (10, 11) or w in (1, 13) for w in range(1, 14)), "10/11 overlap")
    for w in (1, 6, 11):    # US/EU non-overlapping trio: adv channels
        advhit = [RF_TO_ADV_LL[k] for k in wifi_overlap_rf_channels(w) if k in RF_TO_ADV_LL]
        _check(advhit == ([37] if w == 1 else []), "adv hit by wifi %d" % w)
    log("[1] channel math: %d checks ok. 37/38/39 -> RF 0/12/39 -> 2402/2426/2480 MHz; Wi-Fi1->RF%s, Wi-Fi6->RF%s, Wi-Fi11->RF%s, Wi-Fi13->RF%s" % (
        _N_CHECKS[0], _ranges(wifi_overlap_rf_channels(1)), _ranges(wifi_overlap_rf_channels(6)),
        _ranges(wifi_overlap_rf_channels(11)), _ranges(wifi_overlap_rf_channels(13))))

    # ---- 2. opcodes ----
    n0 = _N_CHECKS[0]
    _check(set(_EXPECTED_OPCODES) == set(COMMANDS), "expected/opcode table cover the same commands: %r" % (set(_EXPECTED_OPCODES) ^ set(COMMANDS)))
    for k, c in COMMANDS.items():
        _check(c["opcode"] == (c["ogf"] << 10) | c["ocf"], "opcode formula %s" % k)
        _check(split_opcode(c["opcode"]) == (c["ogf"], c["ocf"]), "split %s" % k)
        _check(c["opcode"] == _EXPECTED_OPCODES[k], "%s opcode %#06x != hand-typed %#06x" % (k, c["opcode"], _EXPECTED_OPCODES[k]))
        _check(0 <= c["ocf"] <= 0x3FF and 0 <= c["ogf"] <= 0x3F, "field widths")
        if c["ogf"] == 0x08:
            _check(c["opcode"] >> 8 == 0x20, "LE opcodes high byte 0x20 (%s)" % k)
    _check(len(set(OPCODE.values())) == len(OPCODE), "opcodes unique")
    _check(make_opcode(0x08, 0x0006) == 0x2006 and make_opcode(0x03, 0x0003) == 0x0C03 and make_opcode(0x03, 0x0001) == 0x0C01, "spot opcodes")
    # LE subevent / mask consistency
    for code, name in LE_SUBEVENTS.items():
        _check(LE_EVENT_MASK_BIT[name] == code - 1, "event mask bit = subevent-1")
    _check(le_event_mask("LE Advertising Report") == 0x2 and le_event_mask(0x0D) == 1 << 12, "event mask bits")
    _check(LE_EVENT_MASK_DEFAULT == sum(1 << b for b in range(5)), "default LE mask = bits 0-4")
    _check(ADV_IND == 0 and ADV_DIRECT_IND_HIGH == 1 and ADV_SCAN_IND == 2 and ADV_NONCONN_IND == 3 and ADV_DIRECT_IND_LOW == 4, "adv types")
    _check(EXT_PROP_LEGACY["ADV_IND"] == (EXT_PROP_CONNECTABLE | EXT_PROP_SCANNABLE | EXT_PROP_LEGACY_PDU), "0x13")
    _check(EXT_PROP_LEGACY["ADV_DIRECT_IND_LOW"] == (EXT_PROP_CONNECTABLE | EXT_PROP_DIRECTED | EXT_PROP_LEGACY_PDU), "0x15")
    _check(EXT_PROP_LEGACY["ADV_DIRECT_IND_HIGH"] == (EXT_PROP_CONNECTABLE | EXT_PROP_DIRECTED | EXT_PROP_HIGH_DUTY_DIRECTED | EXT_PROP_LEGACY_PDU), "0x1D")
    _check(EXT_PROP_LEGACY["ADV_SCAN_IND"] == (EXT_PROP_SCANNABLE | EXT_PROP_LEGACY_PDU), "0x12")
    _check(EXT_PROP_LEGACY["ADV_NONCONN_IND"] == EXT_PROP_LEGACY_PDU, "0x10")
    _check(0x0013 | EXT_REPORT_SCAN_RSP == 0x001B and (0x0012 | EXT_REPORT_SCAN_RSP) == 0x001A, "legacy scan-rsp ext types")
    _check(CHAN_MAP_37 | CHAN_MAP_38 | CHAN_MAP_39 == 7, "channel map")
    _check(ms_to_adv_units(20) == 0x20 and ms_to_adv_units(100) == 0xA0 and ms_to_adv_units(1280) == 0x800 and ms_to_adv_units(10240) == 0x4000, "interval units")
    _check(adv_units_to_ms(0x20) == 20.0, "units->ms")
    for bad_ms in (20.3,):
        try:
            ms_to_adv_units(bad_ms); _check(False, "inexact ms")
        except ValueError:
            pass
    log("[2a] opcode table: %d commands, each opcode == (ogf<<10)|ocf and == independently hand-typed literal (%d checks)." % (len(COMMANDS), _N_CHECKS[0] - n0))

    # kernel header cross-check
    kh = _kernel_header()
    if kh:
        txt = open(kh, errors="replace").read()
        kernel = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"#define\s+(HCI_OP_\w+)\s+(0x[0-9a-fA-F]+)", txt)}
        n_k = 0
        for k, c in COMMANDS.items():
            if c["kernel_macro"]:
                _check(c["kernel_macro"] in kernel, "kernel macro %s missing in %s" % (c["kernel_macro"], kh))
                _check(kernel[c["kernel_macro"]] == c["opcode"], "kernel %s=%#06x vs ours %#06x" % (c["kernel_macro"], kernel[c["kernel_macro"]], c["opcode"]))
                n_k += 1
        for needle, val in (("HCI_MAX_AD_LENGTH", 31), ("HCI_MAX_EXT_AD_LENGTH", 251), ("HCI_MAX_PER_AD_LENGTH", 252), ("HCI_MAX_PER_AD_TOT_LEN", 1650)):
            m = re.search(r"#define\s+%s\s+(\d+)" % needle, txt)
            _check(m and int(m.group(1)) == val, "%s" % needle)
        for needle, val in (("HCI_EV_LE_ADVERTISING_REPORT", 2), ("HCI_EV_LE_EXT_ADV_REPORT", 0x0D), ("HCI_EV_LE_META", 0x3E)):
            m = re.search(r"#define\s+%s\s+(0x[0-9a-fA-F]+|\d+)" % needle, txt)
            _check(m and int(m.group(1), 0) == val, needle)
        log("[2b] kernel header %s: %d opcodes + AD/event constants match ours." % (kh, n_k))
    else:
        log("[2b] kernel header cross-check SKIPPED (no /usr/src/kernels/*/include/net/bluetooth/hci.h)")

    # golden vectors
    gv = _golden_vectors()
    _check(len(gv) == len(_GOLDEN_HEX), "golden count")
    for (label, pkt), hx in zip(gv, _GOLDEN_HEX):
        _check(pkt == bytes.fromhex(hx.replace(" ", "")), "golden %s:\n got %s\n exp %s" % (label, pkt.hex(" "), hx))
        _check(pkt[0] == 1 and pkt[3] == len(pkt) - 4, "plen byte consistent: %s" % label)
        op = struct.unpack_from("<H", pkt, 1)[0]
        _check(op in OPCODE.values(), "opcode known: %s" % label)
    # sizes
    _check(len(le_set_adv_params()) == 4 + 15 and len(le_set_adv_data(b"")) == 4 + 32 and len(le_set_scan_rsp_data(b"")) == 4 + 32, "legacy sizes")
    _check(len(le_set_ext_adv_params()) == 4 + 25, "ext adv params 25")
    _check(len(le_set_ext_adv_enable(True, [(0, 0, 0), (1, 100, 5)])) == 4 + 2 + 8, "ext enable 2+4n")
    _check(len(le_set_ext_scan_params()) == 4 + 3 + 5 and len(le_set_ext_scan_params(phys={1: (0, 16, 16), 3: (1, 32, 16)})) == 4 + 3 + 10, "ext scan params 3+5n")
    _check(len(le_transmitter_test_v4(0, 1, antenna_ids=())) == 4 + 7 + 1 and len(le_transmitter_test_v4(0, 1, cte_length=2, antenna_ids=(0, 1))) == 4 + 7 + 2 + 1, "tx v4 size")
    for bad_call in (lambda: le_transmitter_test_v1(40, 37), lambda: le_transmitter_test_v1(0, 256), lambda: le_set_adv_data(bytes(32)),
                     lambda: le_set_adv_params(interval_min=0x10), lambda: le_set_adv_params(channel_map=0), lambda: le_set_ext_adv_data(0, bytes(252)),
                     lambda: le_set_scan_params(interval=0x10, window=0x20)):
        try:
            bad_call(); _check(False, "range check should have raised")
        except ValueError:
            pass
    _check(le_transmitter_test_v4(0, 1, tx_power_dbm=0x7F)[-1] == 0x7F and le_transmitter_test_v4(0, 1, tx_power_dbm=-1)[-1] == 0xFF, "tx power encoding")
    _check(random_address_kind("C0:11:22:33:44:55") == "static" and random_address_kind("40:11:22:33:44:55") == "resolvable-private"
           and random_address_kind("00:11:22:33:44:55") == "non-resolvable-private", "random address classes")
    log("[2c] %d golden H4 command vectors + size/range checks ok." % len(gv))

    # events
    ev = build_le_adv_report_event(ADV_NONCONN_IND, 1, "C0:11:22:33:44:55", ad_flags() + ad_name("Demo"), -61)
    pe = parse_hci_event(ev)
    r = pe["reports"][0]
    _check(ev[:3] == bytes([4, 0x3E, ev[2]]) and ev[3] == 0x02 and r["type_name"] == "ADV_NONCONN_IND" and r["addr"] == "C0:11:22:33:44:55"
           and r["rssi"] == -61 and ad_parse(r["data"]) == [(1, b"\x06"), (9, b"Demo")], "adv report parse")
    # two-report spec array layout
    two = bytes([2]) + bytes([0, 3]) + bytes([0, 1]) + addr_to_wire("01:02:03:04:05:06") + addr_to_wire("C0:00:00:00:00:01") + bytes([3, 0]) + b"\x02\x01\x06" + struct.pack("<2b", -40, -80)
    rr = parse_le_adv_report(two)
    _check(len(rr) == 2 and rr[0]["event_type"] == 0 and rr[1]["event_type"] == 3 and rr[0]["data"] == b"\x02\x01\x06" and rr[1]["data"] == b"" and rr[1]["rssi"] == -80
           and rr[1]["addr"] == "C0:00:00:00:00:01", "2-report array layout")
    xe = build_le_ext_adv_report_event(0x0013, 1, "C0:11:22:33:44:55", 1, 0, 0xFF, 127, -55, 0, 0, "00:00:00:00:00:00", bytes.fromhex("020106"))
    _check(len(xe) == 3 + 1 + 1 + 24 + 3, "ext report = hdr3 + sub1 + num1 + 24 + data")
    px = parse_hci_event(xe)["reports"][0]
    _check(px["type_name"] == "ADV_IND" and px["rssi"] == -55 and px["sid"] == 0xFF and px["tx_power"] == 127 and px["data"] == b"\x02\x01\x06", "ext report parse")
    cc = parse_hci_event(build_command_complete(OPCODE["le_test_end"], 0, struct.pack("<H", 1234)))
    _check(cc["opcode"] == 0x201F and cc["status"] == 0 and struct.unpack("<H", cc["ret"][1:3])[0] == 1234, "test end return Num_Packets LE16")
    log("[2d] event builders/parsers ok (legacy report n=1 and spec array layout n=2; ext report = 24 fixed octets + data).")

    # ---- 3. AD structures ----
    n0 = _N_CHECKS[0]
    _check(ad_flags() == bytes.fromhex("020106"), "flags 02 01 06")
    _check(ad_name("Demo") == bytes.fromhex("0509") + b"Demo" and ad_name("Dem", True)[1] == 0x08, "names")
    _check(ad_uuids16([0x180F, 0x180A]) == bytes.fromhex("05030F180A18"), "uuid16 list LE")
    _check(ad_uuids32([0x12345678]) == bytes.fromhex("0505") + bytes.fromhex("78563412"), "uuid32 LE")
    _check(ad_uuids128(["12345678-9abc-def0-1234-56789abcdef0"]) == bytes([17, 7]) + bytes.fromhex("f0debc9a78563412f0debc9a78563412"), "uuid128 reversed")
    _check(ad_service_data16(0x181A, b"\x01\x02") == bytes.fromhex("05161A1801 02".replace(" ", "")), "service data 16")
    _check(ad_manufacturer(0xFFFF, b"\x01") == bytes.fromhex("04ffffff01"), "manufacturer 04 FF FF FF 01")
    _check(ad_tx_power(-8) == bytes.fromhex("020AF8") and ad_tx_power(4) == bytes.fromhex("020A04"), "tx power signed")
    _check(ad_appearance(APPEARANCE["generic_sensor"]) == bytes.fromhex("03194005") and APPEARANCE["generic_sensor"] == 21 << 6, "appearance LE 0x0540")
    _check(ad_conn_interval_range(30, 50) == bytes.fromhex("051218002800"), "slave conn interval range 30-50 ms = 0x18..0x28")
    _check(ad_flags(le_limited=True, le_general=False) == bytes.fromhex("020105"), "limited flags")
    # iBeacon
    ib = build_ibeacon("12345678-9abc-def0-1234-56789abcdef0", 1, 2, -59)
    _check(ib.hex() == "020106" "1aff4c000215" "123456789abcdef0123456789abcdef0" "0001" "0002" "c5", "iBeacon golden: " + ib.hex())
    _check(len(ib) == 30 and check_legacy_len(ib) == 1, "iBeacon 30 octets")
    _check(ad_parse(ib)[1][0] == 0xFF and ad_parse(ib)[1][1][:2] == b"\x4c\x00", "iBeacon company 0x004C LE")
    # Eddystone
    uid = build_eddystone_uid(bytes.fromhex("edd1ebeac04e5defa017"), bytes.fromhex("000000000001"), -21)
    _check(uid.hex() == "020106" "0303aafe" "1716aafe" "00" "eb" "edd1ebeac04e5defa017" "000000000001" "0000", "Eddystone-UID golden: " + uid.hex())
    _check(len(uid) == 31 and check_legacy_len(uid) == 0, "UID 31 octets")
    _check(eddystone_url_encode("https://www.example.com/") == bytes([1]) + b"example" + bytes([0]), "url enc .com/")
    _check(eddystone_url_encode("http://google.com") == bytes([2]) + b"google" + bytes([7]), "url enc .com")
    url = build_eddystone_url("https://www.example.com/", -20)
    _check(url.hex() == "020106" "0303aafe" + "%02x16aafe10ec01" % (1 + 2 + 3 + 7 + 1) + b"example".hex() + "00", "Eddystone-URL golden: " + url.hex())
    try:
        build_eddystone_url("https://www.example-too-long-url.org", -20); _check(False, "long url")
    except ValueError:
        pass
    tlm = build_eddystone_tlm(3000, 22.5, 1000, 36000)
    _check(tlm.hex() == "020106" "0303aafe" "1116aafe" "2000" "0bb8" "1680" "000003e8" "00008ca0", "TLM golden: " + tlm.hex())
    _check(build_eddystone_tlm(0, None, 0, 0).hex().endswith("2000" "0000" "8000" "00000000" "00000000"), "TLM temp n/a 0x8000")
    # AltBeacon
    ab = build_altbeacon(COMPANY_ID_TEST, "8492E75F-4FD6-469D-B132-043FE94921D8", 1, 2, -59, 0)
    _check(ab.hex() == "020106" "1bffffff" "beac" "8492e75f4fd6469db132043fe94921d8" "0001" "0002" "c5" "00", "AltBeacon golden: " + ab.hex())
    _check(len(ab) == 31 and check_legacy_len(ab) == 0, "AltBeacon 31 octets")
    # sensor
    sp = build_sensor_payload(7, 42, temp_c=-5.25, humidity_pct=55.5, battery_pct=87, name="S7")
    _check(sp.hex().startswith("020106" "0bff" "ffff" "01072a" "f3fd" "ae15" "57"), "sensor payload golden: " + sp.hex())
    _check(len(sp) <= 31 and ad_parse(sp)[-1] == (9, b"S7"), "sensor payload w/ name")
    # length enforcement
    try:
        check_legacy_len(bytes(32)); _check(False, "32 octets must be rejected")
    except ValueError:
        pass
    try:
        build_ibeacon("12345678-9abc-def0-1234-56789abcdef0", 1, 2, -59, flags=ad_flags() + ad_name("x")); _check(False, "32 > 31")
    except ValueError:
        pass
    try:
        ad_parse(bytes([5, 1, 2])); _check(False, "overrun")
    except ValueError:
        pass
    _check(ad_parse(ib + bytes(1)) == ad_parse(ib), "zero pad tolerated")
    log("[3] AD encoders + iBeacon(30B)/Eddystone-UID(31B)/URL/TLM/AltBeacon(31B)/sensor golden vectors + 31-octet limit: %d checks ok." % (_N_CHECKS[0] - n0))

    # ---- 4. physics ----
    n0 = _N_CHECKS[0]
    pr = prbs9_bits(20)
    _check("".join(map(str, pr)) == "11111111100000111101", "PRBS9 first 20 bits per spec text")
    _check(dtm_payload_bytes(0, 8).hex() == "ffc1fbe84c90728b", "PRBS9 first octets FF C1 FB E8 4C 90 72 8B")
    bits = prbs9_bits(1022)
    _check(bits[:511] == bits[511:] and sum(bits[:511]) == 256, "PRBS9 period 511, 256 ones")
    _check([DTM_PAYLOAD[c][1] for c in (1, 2, 4, 5, 6, 7)] == [0x0F, 0x55, 0xFF, 0x00, 0xF0, 0xAA], "pattern octets")
    _check(dtm_packet_airtime_us(37, 1) == 376 and dtm_packet_interval_us(37, 1) == 625, "37 octets -> 376 us -> 625 us interval (known DTM value)")
    _check(dtm_packet_airtime_us(255, 1) == 2120 and dtm_packet_interval_us(255, 1) == 2500, "255 @1M: 2120 us / 2500 us")
    _check(abs(dtm_duty_cycle(255, 1) - 0.848) < 1e-9, "84.8 % duty")
    _check(dtm_packet_airtime_us(255, 2) == 1064 and dtm_packet_interval_us(255, 2) == 1875 and abs(dtm_duty_cycle(255, 2) - 1064 / 1875.0) < 1e-12, "255 @2M: 1064 us / 1875 us")
    _check(dtm_best_length_for_duty(1)[0] == 193 and dtm_best_length_for_duty(2)[0] == 239, "best duty lengths")
    _check(all(dtm_packet_interval_us(n, p) % 625 == 0 and dtm_packet_interval_us(n, p) >= dtm_packet_airtime_us(n, p) + 249 for n in range(256) for p in (1, 2)), "interval is multiple of 625 and >= L+249")
    _check(adv_pdu_airtime_us(31, 1, "ADV_IND") == 376 and adv_pdu_airtime_us(0, 1, "ADV_IND") == 128 and adv_pdu_airtime_us(0, 1, "SCAN_REQ") == 176, "adv packet air time")
    _check(abs(survival_probability(376, 255, 1) - 4 / 2500.0) < 1e-12, "31-octet ADV survival 0.16 %")
    _check(wifi_power_in_ble_channel_db() > 9 and wifi_power_in_ble_channel_db() < 9.5, "Wi-Fi power per 2 MHz")
    log("[4] DTM/physics: PRBS9 matches spec string; 37 B -> 625 us; 255 B -> 1M %d/%d us (%.1f%%), 2M %d/%d us (%.1f%%); best duty 1M len %d (%.1f%%), 2M len %d (%.1f%%); %d checks ok." % (
        dtm_packet_airtime_us(255, 1), dtm_packet_interval_us(255, 1), 100 * dtm_duty_cycle(255, 1),
        dtm_packet_airtime_us(255, 2), dtm_packet_interval_us(255, 2), 100 * dtm_duty_cycle(255, 2),
        dtm_best_length_for_duty(1)[0], 100 * dtm_best_length_for_duty(1)[1], dtm_best_length_for_duty(2)[0], 100 * dtm_best_length_for_duty(2)[1],
        _N_CHECKS[0] - n0))

    # ---- 5. btmon cross-check ----
    if workdir:
        btmon_crosscheck(workdir, log)

    # ---- 6. markdown consistency ----
    if check_md and os.path.exists(check_md):
        txt = open(check_md, encoding="utf-8").read()
        for name, fn in _BLOCKS.items():
            m = re.search(r"<!-- BEGIN:%s -->\n(.*?)\n<!-- END:%s -->" % (name, name), txt, re.S)
            _check(m is not None and m.group(1) == fn(), "markdown block '%s' out of sync with module" % name)
        hdr = re.findall(r"^#### (.+?) +— +`0x([0-9A-Fa-f]{4})`", txt, re.M)
        by_name = {c["name"]: c["opcode"] for c in COMMANDS.values()}
        for nm, op in hdr:
            _check(nm in by_name and by_name[nm] == int(op, 16), "markdown heading '%s 0x%s' disagrees with module" % (nm, op))
        log("[5] markdown: %d generated blocks in sync; %d command headings agree with module opcodes." % (len(_BLOCKS), len(hdr)))

    log("SELFTEST PASSED  (%d assertions)" % _N_CHECKS[0])
    return lines


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    if "--tables" in args:
        for nm, fn in _BLOCKS.items():
            print("<!-- BEGIN:%s -->\n%s\n<!-- END:%s -->\n" % (nm, fn(), nm))
    elif "--fill-md" in args:
        p = args[args.index("--fill-md") + 1]
        wd = os.path.join(here, "_xcheck")
        log = selftest(False, workdir=wd)
        fill_markdown(p, {"selftest": "```text\n" + "\n".join(log) + "\n```"})
        log = selftest(False, workdir=wd, check_md=p)          # second pass: now includes the markdown-sync check
        fill_markdown(p, {"selftest": "```text\n" + "\n".join(log) + "\n```"})
        print("filled", p)
    else:
        md = os.path.join(here, "06_ble_spec_reference.md")
        selftest(True, workdir=os.path.join(here, "_xcheck"), check_md=md)
