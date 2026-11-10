# OWNER: P1
"""run_server.py: Entry point: starts the whiteboard server.

Imports server.bootstrap (registers every handler and hook), binds
0.0.0.0:config.PORT and prints every LAN address clients could type in,
one line per network interface, with the recommended one marked.

    python run_server.py [port]
"""

import socket
import sys

# QtNetwork only to list the network interfaces (name + IPv4) on every OS;
# the standard library cannot do that portably (no Windows support).
from PyQt5.QtNetwork import QAbstractSocket, QNetworkInterface

from common import config
import server.bootstrap  # noqa: F401  (registers handlers and hooks)
from server import server as server_mod


def lan_ip():
    """Return this machine's LAN IP address.

    Connecting a UDP socket sends no packet; it only makes the OS choose
    the outgoing interface, whose address getsockname() then reports.
    Falls back to 127.0.0.1 when there is no network.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# Interface name prefixes of virtual adapters: container bridges (docker0,
# br-1a2b..., veth...), VM host networks (virbr0, vboxnet0, vmnet8, Windows
# "vEthernet (WSL)", "VirtualBox Host-Only", "VMware Network Adapter") and
# VPN tunnels. Their addresses are not reachable from other lab PCs, so
# printing them would only lead people to type in the wrong IP.
VIRTUAL_PREFIXES = ("docker", "br-", "veth", "virbr", "vboxnet", "vmnet", "vethernet",
                    "virtualbox", "vmware", "tun", "tap", "wg", "zt", "tailscale",
                    "hyper-v", "lxc", "lxd", "podman", "cni", "flannel")


def _is_virtual(iface):
    """Return True if iface (QNetworkInterface) is a virtual/bridge adapter."""
    if iface.type() == QNetworkInterface.Virtual:
        return True
    names = (iface.name().lower(), iface.humanReadableName().lower())
    return any(n.startswith(VIRTUAL_PREFIXES) for n in names)


def lan_addresses():
    """Return [(interface name, IPv4, is_wifi)] of the usable LAN interfaces.

    Skips interfaces that are down, loopback (127.x is only this PC),
    virtual bridges (see VIRTUAL_PREFIXES), IPv6 and link-local
    169.254.x.x addresses (no DHCP answer: not a working network).
    """
    result = []
    for iface in QNetworkInterface.allInterfaces():
        flags = iface.flags()
        if not (flags & QNetworkInterface.IsUp and flags & QNetworkInterface.IsRunning):
            continue
        if flags & QNetworkInterface.IsLoopBack or _is_virtual(iface):
            continue
        for entry in iface.addressEntries():
            ip = entry.ip()
            if ip.protocol() != QAbstractSocket.IPv4Protocol:
                continue
            text = ip.toString()
            if text.startswith(("127.", "169.254.")):
                continue
            result.append((iface.humanReadableName() or iface.name(), text,
                           iface.type() == QNetworkInterface.Wifi))
    return result


def recommended_ip(addresses):
    """Pick the IP to give out from lan_addresses().

    Preference: a Wi-Fi interface (the lab joins over Wi-Fi), and among
    several Wi-Fi ones the one the OS uses for its default route
    (lan_ip()); without Wi-Fi, the default-route address; else the first.
    Returns None if addresses is empty.
    """
    if not addresses:
        return None
    route_ip = lan_ip()
    wifi = [ip for _name, ip, is_wifi in addresses if is_wifi]
    if wifi:
        return route_ip if route_ip in wifi else wifi[0]
    ips = [ip for _name, ip, _wifi in addresses]
    return route_ip if route_ip in ips else ips[0]


def print_addresses(port):
    """Print one line per LAN interface, marking the recommended IP."""
    addresses = lan_addresses()
    best = recommended_ip(addresses)
    if not addresses:
        print("No LAN interface found: only this PC can connect (127.0.0.1:%d)." % port)
        return
    print("Clients type one of these addresses in Join > Server IP (port %d):" % port)
    width = max(len(name) for name, _ip, _wifi in addresses) + 1
    for name, ip, is_wifi in addresses:
        mark = "   <- use this one" if ip == best else ""
        kind = " (Wi-Fi)" if is_wifi else ""
        print("  %-*s %-15s%s%s" % (width, name + ":", ip, kind, mark))


def main():
    """Start the server and run until Ctrl+C."""
    port = int(sys.argv[1]) if len(sys.argv) > 1 else config.PORT
    srv = server_mod.Server("0.0.0.0", port)
    srv.start()
    print("Whiteboard server listening on port %d (all interfaces)" % port)
    print_addresses(port)
    print("Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
