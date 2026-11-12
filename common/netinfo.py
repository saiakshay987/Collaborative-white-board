# OWNER: P1
"""netinfo.py: Find this machine's IPv4 addresses (standard library only).

Used by the server (to print "Clients should connect to: ...") and by the
connect dialog (to show the host which IP to read out). One shared module
keeps the client from importing server code.

Why this exists: a laptop usually has several addresses (Wi-Fi, Ethernet,
WSL / VirtualBox / VMware virtual switches, a VPN). Only ONE of them is on
the network the other laptops are on. Telling people the wrong one is the
number one reason "friends cannot connect", so we list every address with
its adapter name and mark the likely Wi-Fi / hotspot one.

How the addresses are found (no packet is ever sent):
  * Windows: parse the output of `ipconfig` (gives adapter names).
  * Linux:   ioctl SIOCGIFADDR on every interface name (gives names).
  * Everywhere (fallback and cross-check): socket.getaddrinfo() on this
    machine's host name, plus the "UDP connect trick": connect() on a UDP
    socket sends nothing, it only makes the OS pick the outgoing interface,
    and getsockname() then tells us that interface's address. That address is
    the one the OS itself would use to reach other machines.
"""

import re
import socket
import struct
import subprocess
import sys
from collections import namedtuple

try:  # Linux/macOS only; absent on Windows. A static import, so PyInstaller sees it.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

# name: adapter name ("Wi-Fi", "wlan0", "?" when unknown); ip: dotted quad;
# usable: False for loopback / link-local / virtual adapters (never advertise);
# wifi: the name looks like a Wi-Fi adapter; likely: the one to give out;
# note: short reason shown next to unusable addresses.
Address = namedtuple("Address", "name ip usable wifi likely note")

# Adapter-name fragments of virtual adapters. Their addresses (WSL, Hyper-V,
# VirtualBox, VMware, Docker, VPN tunnels) are only reachable from this very
# machine, so handing them out makes friends type an IP that can never work.
VIRTUAL_MARKERS = ("vethernet", "wsl", "hyper-v", "virtualbox", "vbox", "vmware", "vmnet",
                   "docker", "veth", "br-", "virbr", "lxc", "podman", "tailscale",
                   "zerotier", "loopback", "pseudo", "bluetooth", "tap-", "tun")

# Adapter-name fragments of Wi-Fi adapters (Windows "Wi-Fi", "Wireless LAN
# adapter ...", Linux "wlan0" / "wlp3s0"). A phone hotspot is a Wi-Fi network.
WIFI_MARKERS = ("wi-fi", "wifi", "wireless", "wlan")

_IPV4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def default_route_ip():
    """Return the IPv4 address the OS would use to reach another machine, or None.

    The UDP connect trick: no packet is sent; connect() on a datagram socket
    only selects the route, and getsockname() reports the local address of
    that route. 10.255.255.255 is a private address we never actually talk to.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None  # no network at all
    finally:
        s.close()


def _from_ipconfig():
    """Windows: return [(adapter name, ip)] parsed from `ipconfig`.

    The output is a list of blocks. A block starts with a header line at
    column 0 ending in ":" (for example "Wireless LAN adapter Wi-Fi:"),
    followed by indented "key . . . : value" lines. Labels are translated on
    non-English Windows, but every language keeps the word "IPv4" in the
    address line, so that is what we look for. The subnet mask and gateway
    lines do not contain "IPv4" and are ignored.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash from a GUI app
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, timeout=5, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return []
    text = out.stdout.decode("mbcs", errors="replace") if sys.platform == "win32" \
        else out.stdout.decode("utf-8", errors="replace")
    found, adapter = [], "?"
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            # "Wireless LAN adapter Wi-Fi:" -> "Wi-Fi";  "Ethernet adapter Ethernet 2:" -> "Ethernet 2"
            header = line.rstrip()[:-1]
            adapter = header.split("adapter ", 1)[1] if "adapter " in header else header
        elif "IPv4" in line:
            match = _IPV4.search(line)
            if match:
                found.append((adapter, match.group(1)))
    return found


def _from_ioctl():
    """Linux: return [(interface name, ip)] using SIOCGIFADDR on each interface."""
    if fcntl is None or not hasattr(socket, "if_nameindex"):
        return []
    SIOCGIFADDR = 0x8915  # Linux ioctl number: "get the IPv4 address of an interface"
    found = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            try:
                # struct ifreq: 16-byte name + a sockaddr; bytes 20..24 hold the IPv4 address.
                req = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
                found.append((name, socket.inet_ntoa(req[20:24])))
            except OSError:
                continue  # interface without an IPv4 address
    finally:
        s.close()
    return found


def _from_hostname():
    """Portable fallback: addresses the machine's host name resolves to (no names)."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return [("?", info[4][0]) for info in infos]


def _classify(name, ip):
    """Return an Address (without `likely`) for one (name, ip) pair."""
    lowered = name.lower()
    # "wl..." covers Linux names such as wlan0 / wlp3s0 / wlo1.
    wifi = (lowered.startswith("wl") or any(m in lowered for m in WIFI_MARKERS))         and not any(m in lowered for m in VIRTUAL_MARKERS)
    if ip.startswith("127."):
        return Address(name, ip, False, False, False, "loopback: only this PC")
    if ip.startswith("169.254."):
        return Address(name, ip, False, False, False, "no DHCP answer: not a working network")
    if any(m in lowered for m in VIRTUAL_MARKERS):
        return Address(name, ip, False, False, False, "virtual adapter: other PCs cannot reach it")
    return Address(name, ip, True, wifi, False, "")


def local_addresses():
    """Return every IPv4 address of this machine as a list of Address.

    Order: the likely one first, then the other usable ones, then the
    unusable ones (shown only so the host can see why they were skipped).

    The likely address is chosen like this: a usable Wi-Fi adapter wins (the
    lab joins over Wi-Fi or a phone hotspot); if several, the one the OS
    routes through (default_route_ip()) wins; with no Wi-Fi adapter the
    default-route address wins; otherwise the first usable one.
    """
    pairs = _from_ipconfig() if sys.platform == "win32" else _from_ioctl()
    pairs = list(pairs)
    known = {ip for _n, ip in pairs}
    route_ip = default_route_ip()
    # Fallbacks: the name lookup, and the route address, for anything the
    # adapter listing missed (or when ipconfig/ioctl was unavailable).
    for name, ip in _from_hostname() + ([("?", route_ip)] if route_ip else []):
        if ip not in known:
            pairs.append((name, ip))
            known.add(ip)

    entries = [_classify(name, ip) for name, ip in pairs]
    usable = [a for a in entries if a.usable]
    wifi = [a for a in usable if a.wifi]
    pool = wifi or usable
    best = next((a for a in pool if a.ip == route_ip), pool[0] if pool else None)

    ordered = []
    if best is not None:
        ordered.append(best._replace(likely=True))
    ordered += [a for a in usable if a is not best]
    ordered += [a for a in entries if not a.usable]
    return ordered


def likely_address(addresses=None):
    """Return the Address marked likely (or None when there is no usable network)."""
    for a in (addresses if addresses is not None else local_addresses()):
        if a.likely:
            return a
    return None
