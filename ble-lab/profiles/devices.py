#!/usr/bin/env python3
"""
devices.py - Shared BlueZ device registry for the BluForge Profile Emulator.

Wraps org.bluez's D-Bus ObjectManager so the A2DP/AVRCP/HFP drivers and the
Web GUI's peripheral picker all see the same view of bonded/connected
devices. No dependency beyond dbus-python (confirmed installed system-wide -
see /home/lanforge/audio-bluetooth/bt_test_script.py which uses the same
approach for the existing POLQA feature).

CLI:
  python3 devices.py list                # JSON array, for the GUI dropdown
  python3 devices.py info <MAC>
  python3 devices.py connect <MAC> [--profile a2dp-sink|hfp-ag|hfp-hf]
  python3 devices.py disconnect <MAC>
  python3 devices.py scan [seconds]      # discover new devices (default 8s)
"""
import sys
import json
import time
import argparse

import dbus

BLUEZ_SERVICE = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"

# SDP/GATT UUIDs relevant to the profiles BluForge drives. Keyed lowercase.
UUID_NAMES = {
    "0000110a-0000-1000-8000-00805f9b34fb": "A2DP Source",
    "0000110b-0000-1000-8000-00805f9b34fb": "A2DP Sink",
    "0000110c-0000-1000-8000-00805f9b34fb": "AVRCP Target",
    "0000110e-0000-1000-8000-00805f9b34fb": "AVRCP",
    "0000110d-0000-1000-8000-00805f9b34fb": "A/V Distribution",
    "0000111e-0000-1000-8000-00805f9b34fb": "Handsfree",
    "0000111f-0000-1000-8000-00805f9b34fb": "Handsfree Audio Gateway",
    "00001108-0000-1000-8000-00805f9b34fb": "Headset",
    "00001112-0000-1000-8000-00805f9b34fb": "Headset Audio Gateway",
    "0000112f-0000-1000-8000-00805f9b34fb": "PBAP (Phonebook Access)",
    "00001200-0000-1000-8000-00805f9b34fb": "PnP Information",
}

PROFILE_HINTS = {
    "a2dp_sink":  "0000110b-0000-1000-8000-00805f9b34fb",
    "a2dp_source": "0000110a-0000-1000-8000-00805f9b34fb",
    "avrcp":       "0000110e-0000-1000-8000-00805f9b34fb",
    "hfp_hf":      "0000111e-0000-1000-8000-00805f9b34fb",
    "hfp_ag":      "0000111f-0000-1000-8000-00805f9b34fb",
    "pbap":        "0000112f-0000-1000-8000-00805f9b34fb",
}


class BlueZError(Exception):
    pass


class BlueZ:
    """Thin wrapper over org.bluez's D-Bus API (system bus)."""

    def __init__(self):
        self.bus = dbus.SystemBus()

    def _manager(self):
        return dbus.Interface(
            self.bus.get_object(BLUEZ_SERVICE, "/"),
            "org.freedesktop.DBus.ObjectManager")

    def managed_objects(self):
        return self._manager().GetManagedObjects()

    # ---- device discovery / listing ------------------------------------
    def list_devices(self):
        """All known (bonded or just-seen) devices under hci0, decoded."""
        out = []
        for path, ifaces in self.managed_objects().items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not path.startswith(ADAPTER_PATH + "/"):
                continue
            uuids = [str(u) for u in dev.get("UUIDs", [])]
            profiles = sorted({UUID_NAMES.get(u.lower(), u) for u in uuids} - {u for u in uuids if u.lower() not in UUID_NAMES and False})
            profiles = [UUID_NAMES[u.lower()] for u in uuids if u.lower() in UUID_NAMES]
            out.append({
                "mac": str(dev.get("Address", "")),
                "name": str(dev.get("Name", dev.get("Alias", ""))),
                "alias": str(dev.get("Alias", "")),
                "paired": bool(dev.get("Paired", False)),
                "bonded": bool(dev.get("Bonded", False)),
                "trusted": bool(dev.get("Trusted", False)),
                "connected": bool(dev.get("Connected", False)),
                "rssi": int(dev["RSSI"]) if "RSSI" in dev else None,
                "icon": str(dev.get("Icon", "")),
                "profiles": profiles,
                "supports_a2dp": any("A2DP" in p for p in profiles),
                "supports_avrcp": any("AVRCP" in p for p in profiles),
                "supports_hfp": any("Handsfree" in p or "Headset" in p for p in profiles),
                "path": path,
            })
        out.sort(key=lambda d: (not d["connected"], d["name"] or d["mac"]))
        return out

    def find_device_path(self, mac):
        suffix = "dev_" + mac.upper().replace(":", "_")
        for path in self.managed_objects():
            if path.startswith(ADAPTER_PATH + "/") and path.rsplit("/", 1)[-1] == suffix:
                return path
        return None

    def device_info(self, mac):
        path = self.find_device_path(mac)
        if not path:
            raise BlueZError(f"unknown device {mac} (not bonded/seen on {ADAPTER_PATH})")
        for d in self.list_devices():
            if d["mac"].upper() == mac.upper():
                return d
        raise BlueZError(f"device {mac} vanished during lookup")

    # ---- connection management ------------------------------------------
    def _device_iface(self, mac):
        path = self.find_device_path(mac)
        if not path:
            raise BlueZError(f"unknown device {mac}")
        return dbus.Interface(self.bus.get_object(BLUEZ_SERVICE, path), "org.bluez.Device1")

    def connect(self, mac, profile_uuid=None, timeout=15):
        iface = self._device_iface(mac)
        try:
            if profile_uuid:
                iface.ConnectProfile(profile_uuid, timeout=timeout * 1000)
            else:
                iface.Connect(timeout=timeout * 1000)
        except dbus.exceptions.DBusException as e:
            raise BlueZError(f"connect {mac} failed: {e.get_dbus_message() or e}") from e

    def disconnect(self, mac, timeout=10):
        iface = self._device_iface(mac)
        try:
            iface.Disconnect(timeout=timeout * 1000)
        except dbus.exceptions.DBusException as e:
            raise BlueZError(f"disconnect {mac} failed: {e.get_dbus_message() or e}") from e

    def is_connected(self, mac):
        try:
            return self.device_info(mac)["connected"]
        except BlueZError:
            return False

    def wait_connected(self, mac, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_connected(mac):
                return True
            time.sleep(0.3)
        return False

    # ---- discovery (scan) -------------------------------------------------
    def scan(self, seconds=8):
        adapter = dbus.Interface(self.bus.get_object(BLUEZ_SERVICE, ADAPTER_PATH), "org.bluez.Adapter1")
        try:
            adapter.StartDiscovery()
        except dbus.exceptions.DBusException as e:
            raise BlueZError(f"StartDiscovery failed: {e.get_dbus_message() or e}") from e
        time.sleep(seconds)
        try:
            adapter.StopDiscovery()
        except dbus.exceptions.DBusException:
            pass
        return self.list_devices()


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="BlueZ device registry for BluForge profile tests")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    pi = sub.add_parser("info"); pi.add_argument("mac")

    pc = sub.add_parser("connect")
    pc.add_argument("mac")
    pc.add_argument("--profile", choices=list(PROFILE_HINTS.keys()), default=None)
    pc.add_argument("--timeout", type=int, default=15)

    pd = sub.add_parser("disconnect"); pd.add_argument("mac")

    ps = sub.add_parser("scan"); ps.add_argument("seconds", nargs="?", type=int, default=8)

    a = ap.parse_args()
    bz = BlueZ()
    try:
        if a.cmd == "list":
            print(json.dumps(bz.list_devices(), indent=2))
        elif a.cmd == "info":
            print(json.dumps(bz.device_info(a.mac), indent=2))
        elif a.cmd == "connect":
            uuid = PROFILE_HINTS.get(a.profile) if a.profile else None
            bz.connect(a.mac, profile_uuid=uuid, timeout=a.timeout)
            ok = bz.wait_connected(a.mac, timeout=a.timeout)
            print(json.dumps({"ok": ok, "mac": a.mac}))
            sys.exit(0 if ok else 1)
        elif a.cmd == "disconnect":
            bz.disconnect(a.mac)
            print(json.dumps({"ok": True, "mac": a.mac}))
        elif a.cmd == "scan":
            print(json.dumps(bz.scan(a.seconds), indent=2))
    except BlueZError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
