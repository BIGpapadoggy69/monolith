# MONOLITH // Network Identity Utility

![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-050506?style=flat-square&logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.8%2B-050506?style=flat-square&logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/Dependencies-None%20(StdLib)-050506?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-050506?style=flat-square)

**MONOLITH** is a lightweight, zero-dependency Windows network identity utility designed to rotate Layer-2 and Layer-3 identifiers cleanly. Built purely on the Python standard library (`winreg`, `ctypes`, `http.server`), it renders a hardware-accelerated, monochrome desktop interface without requiring external `pip` packages.

---

## Features

* **1-Click Full Identity Rotation:** Simultaneously updates the active adapter's MAC address, modifies the DHCP Option-12 hostname, flushes ARP/DNS/NetBIOS caches, and forces a fresh DHCP lease from the router.
* **Driver-Safe MAC Spoofing:** Uses locally administered unicast prefixes (`02:XX:XX:XX:XX:XX`) to ensure 100% compatibility with modern Intel, Realtek, and Qualcomm Windows NIC drivers. Includes OUI presets (Apple, Intel, Samsung, Realtek) and custom hex input.
* **Instant DHCP Hostname Disguise:** Writes directly to `HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters` (`Hostname` & `NV Hostname`) so the router registers the new device name immediately upon lease renewal—without waiting for a full system reboot.
* **Encrypted DNS Switcher:** Switch active adapter DNS resolvers on the fly between **Cloudflare** (`1.1.1.1`), **Mullvad**, **Quad9** (`9.9.9.9`), or standard Router DHCP.
* **Windows Network History Cleaner:** Purges cached network profiles and registry signatures under `HKLM\...\NetworkList\Profiles` and resets the Winsock catalog.
* **Factory Hardware Restore:** Removes custom `NetworkAddress` registry overrides with a single click to revert to the physical burned-in MAC address.

---

## Quick Start

No `pip install` required. Simply run the script with Python 3.x:

```bash
git clone [https://github.com/BIGpapadoggy69/monolith.git](https://github.com/BIGpapadoggy69/monolith.git)
cd monolith
python monolith.py
