import os
import sys
import json
import time
import random
import socket
import ctypes
import tempfile
import threading
import subprocess
import winreg
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# =====================================================================
# WINDOWS KERNEL & REGISTRY BACKEND
# =====================================================================

REGISTRY_ADAPTER_PATH = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"
NETLIST_PROFILES_PATH = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Profiles"

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def ensure_admin():
    if not is_admin():
        script = os.path.abspath(sys.argv[0])
        params = " ".join([f'"{arg}"' for arg in sys.argv[1:]])
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" {params}', None, 1
        )
        sys.exit(0)

def run_cmd(cmd):
    return subprocess.run(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    )

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Disconnected"

def get_gateway_ip():
    try:
        res = run_cmd('powershell -Command "(Get-NetRoute -DestinationPrefix \'0.0.0.0/0\' | Select-Object -First 1).NextHop"')
        gw = res.stdout.strip()
        return gw if gw else "N/A"
    except Exception:
        return "N/A"

def get_current_hostname():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters") as key:
            val, _ = winreg.QueryValueEx(key, "NV Hostname")
            return val
    except Exception:
        return os.environ.get("COMPUTERNAME", "UNKNOWN")

def get_network_adapters():
    adapters = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REGISTRY_ADAPTER_PATH) as key:
            for i in range(120):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    if subkey_name == "Properties":
                        continue
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        desc, _ = winreg.QueryValueEx(subkey, "DriverDesc")
                        if any(x in desc.lower() for x in ["wan miniport", "virtual", "loopback", "bluetooth", "hyper-v", "tap-windows"]):
                            continue
                        try:
                            curr_mac, _ = winreg.QueryValueEx(subkey, "NetworkAddress")
                        except FileNotFoundError:
                            curr_mac = "HARDWARE"
                        adapters.append({
                            "id": subkey_name,
                            "name": desc,
                            "mac": curr_mac
                        })
                except OSError:
                    break
    except Exception:
        pass
    return adapters

def generate_mac(vendor="random"):
    prefixes = {
        "random":  [0x02, random.randint(0x10, 0xFE), random.randint(0x00, 0xFF)],
        "apple":   [0x02, 0x1B, 0x63],
        "intel":   [0x02, 0x15, 0x17],
        "samsung": [0x02, 0x16, 0x32],
        "realtek": [0x02, 0xE0, 0x4C]
    }
    head = prefixes.get(vendor, prefixes["random"])
    tail = [random.randint(0x00, 0xFF) for _ in range(3)]
    return "".join(f"{b:02X}" for b in (head + tail))

def generate_hostname(profile="desktop"):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
    num = "".join(random.choice("0123456789") for _ in range(3))
    alphanum = "".join(random.choice(chars) for _ in range(7))
    profiles = {
        "desktop": f"DESKTOP-{alphanum}",
        "iphone":  f"iPhone-{num}",
        "galaxy":  f"Galaxy-S25-{num}",
        "macbook": f"MacBook-Pro-{alphanum[:4]}",
        "arch":    f"arch-{alphanum[:5].lower()}"
    }
    return profiles.get(profile, f"DESKTOP-{alphanum}")

def apply_mac(subkey_name, adapter_desc, new_mac=None):
    key_path = f"{REGISTRY_ADAPTER_PATH}\\{subkey_name}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
        if new_mac:
            clean_mac = new_mac.replace(":", "").replace("-", "").upper()[:12]
            winreg.SetValueEx(key, "NetworkAddress", 0, winreg.REG_SZ, clean_mac)
        else:
            try:
                winreg.DeleteValue(key, "NetworkAddress")
            except FileNotFoundError:
                pass
    run_cmd(f'powershell -Command "Get-NetAdapter | Where-Object {{$_.InterfaceDescription -eq \'{adapter_desc}\'}} | Restart-NetAdapter"')
    time.sleep(1.4)

def apply_hostname(new_name):
    reg_paths = [
        r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName",
        r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName",
        r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"
    ]
    for path in reg_paths:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_ALL_ACCESS) as key:
            if "Tcpip" in path:
                winreg.SetValueEx(key, "Hostname", 0, winreg.REG_SZ, new_name)
                winreg.SetValueEx(key, "NV Hostname", 0, winreg.REG_SZ, new_name)
            else:
                winreg.SetValueEx(key, "ComputerName", 0, winreg.REG_SZ, new_name)
    run_cmd(f'powershell -Command "Rename-Computer -NewName \'{new_name}\' -Force"')

def flush_network():
    run_cmd("ipconfig /release")
    run_cmd("ipconfig /flushdns")
    run_cmd("arp -d *")
    run_cmd("nbtstat -R")
    run_cmd("nbtstat -RR")
    run_cmd("ipconfig /renew")

def clean_network_profiles():
    """Löscht gespeicherte Netzwerk-Historie aus der Windows-Registry."""
    removed = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NETLIST_PROFILES_PATH, 0, winreg.KEY_ALL_ACCESS) as key:
            subkeys = []
            for i in range(200):
                try:
                    subkeys.append(winreg.EnumKey(key, i))
                except OSError:
                    break
            for sk in subkeys:
                try:
                    winreg.DeleteKey(key, sk)
                    removed += 1
                except OSError:
                    pass
    except Exception:
        pass
    run_cmd("netsh winsock reset")
    return removed

def apply_dns(adapter_desc, provider):
    dns_map = {
        "cloudflare": ("1.1.1.1", "1.0.0.1"),
        "quad9":      ("9.9.9.9", "149.112.112.112"),
        "mullvad":    ("194.242.2.2", "194.242.2.3")
    }
    if provider == "dhcp":
        cmd = f'powershell -Command "Get-NetAdapter | Where-Object {{$_.InterfaceDescription -eq \'{adapter_desc}\'}} | Set-DnsClientServerAddress -ResetServerAddresses"'
    else:
        p1, p2 = dns_map.get(provider, ("1.1.1.1", "1.0.0.1"))
        cmd = f'powershell -Command "Get-NetAdapter | Where-Object {{$_.InterfaceDescription -eq \'{adapter_desc}\'}} | Set-DnsClientServerAddress -ServerAddresses (\'{p1}\',\'{p2}\')"'
    run_cmd(cmd)
    run_cmd("ipconfig /flushdns")


# =====================================================================
# MONOCHROME MINIMALIST UI (VERCEL / LINEAR / NOTHING OS STYLE)
# =====================================================================

HTML_UI = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>MONOLITH — Network Identity Utility</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

  :root {
    --bg: #050506;
    --surface: #0c0c0e;
    --surface-hover: #121216;
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.18);
    --text-primary: #fafafa;
    --text-secondary: #a1a1aa;
    --text-muted: #52525b;
    --accent: #ffffff;
    --status-ok: #10b981;
  }

  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    user-select: none;
    -webkit-font-smoothing: antialiased;
  }

  body {
    background-color: var(--bg);
    color: var(--text-primary);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    height: 100vh;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    /* Subtle radial ambient top light */
    background-image: radial-gradient(ellipse 80% 40% at 50% -15%, rgba(255,255,255,0.06), transparent);
  }

  /* Top Navigation Bar */
  .topbar {
    height: 56px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 28px;
    background: rgba(5, 5, 6, 0.8);
    backdrop-filter: blur(12px);
    z-index: 10;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .brand-mark {
    width: 18px;
    height: 18px;
    background: var(--text-primary);
    border-radius: 3px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .brand-mark::after {
    content: '';
    width: 6px;
    height: 6px;
    background: var(--bg);
    border-radius: 1px;
  }

  .brand-name {
    font-weight: 600;
    font-size: 0.9rem;
    letter-spacing: 0.08em;
    color: var(--text-primary);
  }

  .version-pill {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.68rem;
    color: var(--text-secondary);
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid var(--border);
    padding: 2px 8px;
    border-radius: 999px;
  }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .status-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.75rem;
    color: var(--text-secondary);
    font-family: 'JetBrains Mono', monospace;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--status-ok);
    box-shadow: 0 0 8px var(--status-ok);
  }

  .icon-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-secondary);
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 0.75rem;
    font-family: 'Inter', sans-serif;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .icon-btn:hover {
    color: var(--text-primary);
    border-color: var(--border-strong);
    background: rgba(255, 255, 255, 0.03);
  }

  /* Main Workspace */
  .workspace {
    flex: 1;
    display: grid;
    grid-template-columns: 330px 1fr;
    gap: 20px;
    padding: 24px 28px;
    min-height: 0;
  }

  .col-left {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .col-right {
    display: grid;
    grid-template-rows: auto 1fr 145px;
    gap: 16px;
    min-height: 0;
  }

  /* Spotlight Card System */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s ease;
  }

  .card::before {
    content: '';
    position: absolute;
    inset: 0;
    background: radial-gradient(
      500px circle at var(--mouse-x, -500px) var(--mouse-y, -500px),
      rgba(255, 255, 255, 0.045),
      transparent 40%
    );
    pointer-events: none;
  }

  .card:hover {
    border-color: rgba(255, 255, 255, 0.14);
  }

  .section-label {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    margin-bottom: 14px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  /* Telemetry Readouts */
  .metric-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .metric-item {
    background: rgba(255, 255, 255, 0.015);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .metric-title {
    font-size: 0.7rem;
    color: var(--text-secondary);
    display: flex;
    justify-content: space-between;
  }

  .metric-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.98rem;
    font-weight: 600;
    color: var(--text-primary);
    letter-spacing: 0.02em;
  }

  /* Form Controls */
  label {
    display: block;
    font-size: 0.74rem;
    font-weight: 500;
    color: var(--text-secondary);
    margin-bottom: 6px;
  }

  select, input[type="text"] {
    width: 100%;
    background: #070709;
    border: 1px solid var(--border);
    color: var(--text-primary);
    padding: 9px 12px;
    border-radius: 6px;
    font-family: 'Inter', sans-serif;
    font-size: 0.8rem;
    outline: none;
    transition: border-color 0.15s ease;
  }

  input[type="text"].mono {
    font-family: 'JetBrains Mono', monospace;
  }

  select:focus, input[type="text"]:focus {
    border-color: rgba(255, 255, 255, 0.35);
  }

  /* Buttons */
  .btn-primary {
    background: var(--text-primary);
    color: var(--bg);
    border: 1px solid var(--text-primary);
    padding: 11px 18px;
    border-radius: 7px;
    font-family: 'Inter', sans-serif;
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s cubic-bezier(0.16, 1, 0.3, 1);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }

  .btn-primary:hover {
    background: #e4e4e7;
    transform: translateY(-1px);
    box-shadow: 0 4px 20px rgba(255, 255, 255, 0.12);
  }

  .btn-primary:active {
    transform: translateY(0);
  }

  .btn-secondary {
    background: rgba(255, 255, 255, 0.03);
    color: var(--text-primary);
    border: 1px solid var(--border);
    padding: 9px 14px;
    border-radius: 6px;
    font-family: 'Inter', sans-serif;
    font-size: 0.78rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }

  .btn-secondary:hover {
    background: rgba(255, 255, 255, 0.07);
    border-color: var(--border-strong);
  }

  .btn-secondary:active {
    transform: scale(0.985);
  }

  /* Hero Action Banner */
  .hero-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
  }

  .hero-info h2 {
    font-size: 1.05rem;
    font-weight: 600;
    margin-bottom: 4px;
    letter-spacing: -0.01em;
  }

  .hero-info p {
    font-size: 0.78rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  /* Modules Grid */
  .modules-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .control-row {
    display: flex;
    gap: 8px;
    margin-top: 8px;
  }

  .segmented-dns {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 6px;
    margin-top: 6px;
  }

  /* Activity Console */
  .console-card {
    display: flex;
    flex-direction: column;
    padding: 14px 18px;
    background: #070709;
  }

  #console-output {
    flex: 1;
    overflow-y: auto;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.74rem;
    line-height: 1.65;
    color: var(--text-secondary);
  }

  .log-entry {
    display: flex;
    gap: 12px;
  }
  .log-ts {
    color: var(--text-muted);
    flex-shrink: 0;
  }
  .log-msg.white { color: var(--text-primary); }
  .log-msg.muted { color: var(--text-secondary); }

  /* Subtle Progress Bar */
  .progress-line {
    position: fixed;
    top: 56px;
    left: 0;
    height: 1px;
    background: var(--text-primary);
    width: 0%;
    transition: width 0.3s ease, opacity 0.3s ease;
    opacity: 0;
    z-index: 20;
  }
  .progress-line.active {
    width: 100%;
    opacity: 1;
  }
</style>
</head>
<body>

<div class="progress-line" id="loader-bar"></div>

<!-- TOPBAR -->
<header class="topbar">
  <div class="brand">
    <div class="brand-mark"></div>
    <span class="brand-name">MONOLITH</span>
    <span class="version-pill">v5.0 // KERNEL</span>
  </div>
  <div class="topbar-right">
    <div class="status-indicator">
      <span class="dot"></span>
      <span id="state-text">ELEVATED SESSION</span>
    </div>
    <button class="icon-btn" id="audio-toggle" onclick="toggleSound()">Sound: On</button>
    <button class="icon-btn" onclick="refreshStatus()">Refresh</button>
  </div>
</header>

<!-- WORKSPACE -->
<main class="workspace">

  <!-- LEFT: SYSTEM TELEMETRY -->
  <div class="col-left">
    <div class="card" style="flex: 1; display: flex; flex-direction: column; gap: 16px;">
      <div class="section-label">
        <span>Hardware Interface</span>
        <span>NIC</span>
      </div>

      <div>
        <label>Target Network Adapter</label>
        <select id="adapter-select" onchange="onAdapterSelect()"></select>
      </div>

      <div class="section-label" style="margin-top: 6px; margin-bottom: 0;">
        <span>Active Identity</span>
      </div>

      <div class="metric-list">
        <div class="metric-item">
          <div class="metric-title">
            <span>MAC Address</span>
            <span id="mac-badge" style="color: var(--text-muted);">ACTIVE</span>
          </div>
          <div class="metric-value" id="val-mac">—</div>
        </div>

        <div class="metric-item">
          <div class="metric-title">
            <span>Device Hostname (DHCP)</span>
            <span style="color: var(--text-muted);">OPT-12</span>
          </div>
          <div class="metric-value" id="val-host">—</div>
        </div>

        <div class="metric-item">
          <div class="metric-title">
            <span>Local IPv4</span>
            <span style="color: var(--text-muted);">SUBNET</span>
          </div>
          <div class="metric-value" id="val-ip">—</div>
        </div>

        <div class="metric-item">
          <div class="metric-title">
            <span>Default Gateway</span>
            <span style="color: var(--text-muted);">ROUTER</span>
          </div>
          <div class="metric-value" id="val-gw">—</div>
        </div>
      </div>

      <div style="margin-top: auto; display: flex; flex-direction: column; gap: 8px;">
        <button class="btn-secondary" style="width: 100%;" onclick="restoreHardwareMac()">
          Reset to Factory Hardware MAC
        </button>
      </div>
    </div>
  </div>

  <!-- RIGHT: SPOOFING CONTROLS & LOG -->
  <div class="col-right">

    <!-- ONE-CLICK IDENTITY ROTATION -->
    <div class="card hero-banner">
      <div class="hero-info">
        <h2>New Router Identity</h2>
        <p>Rotates MAC address, assigns a fresh device hostname, clears ARP/DNS caches, and forces a new DHCP lease from the router.</p>
      </div>
      <button class="btn-primary" style="flex-shrink: 0; padding: 13px 22px;" onclick="oneClickSpoof()">
        Rotate Full Identity
      </button>
    </div>

    <!-- GRANULAR MODULES -->
    <div class="modules-grid">

      <!-- MAC MODULE -->
      <div class="card" style="display: flex; flex-direction: column; justify-content: space-between;">
        <div>
          <div class="section-label">
            <span>MAC Spoofer</span>
            <span>LAYER-2</span>
          </div>
          <label>Vendor Prefix (OUI)</label>
          <select id="mac-vendor" style="margin-bottom: 10px;">
            <option value="random">Unicast Random (02:XX:XX)</option>
            <option value="apple">Apple Inc. (02:1B:63)</option>
            <option value="intel">Intel Corp. (02:15:17)</option>
            <option value="samsung">Samsung Elec. (02:16:32)</option>
            <option value="realtek">Realtek Semi. (02:E0:4C)</option>
          </select>

          <label>Custom Hex MAC (Optional)</label>
          <input type="text" id="custom-mac" class="mono" placeholder="02:A1:B2:C3:D4:E5" maxlength="17">
        </div>

        <div class="control-row" style="margin-top: 14px;">
          <button class="btn-secondary" style="flex: 1;" onclick="spoofMacOnly()">Apply MAC</button>
        </div>
      </div>

      <!-- HOSTNAME & STACK MODULE -->
      <div class="card" style="display: flex; flex-direction: column; justify-content: space-between;">
        <div>
          <div class="section-label">
            <span>Hostname & Stack</span>
            <span>LAYER-3 / DHCP</span>
          </div>
          <label>Device Disguise Preset</label>
          <select id="host-profile" style="margin-bottom: 10px;">
            <option value="desktop">Windows Desktop (DESKTOP-XXXX)</option>
            <option value="iphone">Apple iPhone (iPhone-XXX)</option>
            <option value="galaxy">Samsung Galaxy (Galaxy-S25-XXX)</option>
            <option value="macbook">MacBook Pro (MacBook-Pro-XXXX)</option>
            <option value="arch">Arch Linux (arch-xxxxx)</option>
          </select>

          <label>Custom Device Name (Optional)</label>
          <input type="text" id="custom-host" class="mono" placeholder="z.B. iPad-Pro" maxlength="15">
        </div>

        <div class="control-row" style="margin-top: 14px;">
          <button class="btn-secondary" style="flex: 1;" onclick="spoofHostOnly()">Apply Hostname</button>
        </div>
      </div>

      <!-- NETWORK HYGIENE & DNS (FULL WIDTH BOTTOM ROW OF MODULES) -->
      <div class="card" style="grid-column: span 2; display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 16px 20px;">
        <div style="flex: 1;">
          <label style="margin-bottom: 4px;">Encrypted DNS Resolver</label>
          <div class="segmented-dns">
            <button class="btn-secondary" onclick="setDns('cloudflare')">Cloudflare</button>
            <button class="btn-secondary" onclick="setDns('mullvad')">Mullvad</button>
            <button class="btn-secondary" onclick="setDns('quad9')">Quad9</button>
            <button class="btn-secondary" onclick="setDns('dhcp')">Router Default</button>
          </div>
        </div>

        <div style="width: 1px; height: 42px; background: var(--border);"></div>

        <div style="display: flex; gap: 10px;">
          <button class="btn-secondary" onclick="flushStack()">Flush ARP / DNS / DHCP</button>
          <button class="btn-secondary" onclick="wipeHistory()">Wipe Win-Network History</button>
        </div>
      </div>

    </div>

    <!-- CONSOLE STREAM -->
    <div class="card console-card">
      <div class="section-label" style="margin-bottom: 8px;">
        <span>Kernel Log</span>
        <span id="clock" style="font-family: 'JetBrains Mono', monospace;">00:00:00</span>
      </div>
      <div id="console-output"></div>
    </div>

  </div>

</main>

<script>
// =====================================================================
// SUBTLE MECHANICAL TACTILE SOUND ENGINE (NO CHEESY SCI-FI LASERS)
// =====================================================================
const TactileAudio = {
  ctx: null,
  enabled: true,
  init() {
    if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (this.ctx.state === 'suspended') this.ctx.resume();
  },
  // Ultra-subtle mechanical switch thock
  click(freq = 160) {
    if (!this.enabled) return;
    this.init();
    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(freq, now);
    osc.frequency.exponentialRampToValueAtTime(42, now + 0.035);
    gain.gain.setValueAtTime(0.08, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.04);
    osc.connect(gain);
    gain.connect(this.ctx.destination);
    osc.start(now);
    osc.stop(now + 0.045);
  },
  // Soft low-profile hover tick
  tick() {
    if (!this.enabled) return;
    this.init();
    const now = this.ctx.currentTime;
    const osc = this.ctx.createOscillator();
    const gain = this.ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(120, now);
    osc.frequency.exponentialRampToValueAtTime(60, now + 0.015);
    gain.gain.setValueAtTime(0.025, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.018);
    osc.connect(gain);
    gain.connect(this.ctx.destination);
    osc.start(now);
    osc.stop(now + 0.02);
  },
  // Clean two-note confirmation chime
  done() {
    if (!this.enabled) return;
    this.init();
    [320, 480].forEach((f, i) => {
      const now = this.ctx.currentTime + i * 0.07;
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(f, now);
      gain.gain.setValueAtTime(0.05, now);
      gain.gain.exponentialRampToValueAtTime(0.0005, now + 0.18);
      osc.connect(gain);
      gain.connect(this.ctx.destination);
      osc.start(now);
      osc.stop(now + 0.2);
    });
  }
};

function toggleSound() {
  TactileAudio.enabled = !TactileAudio.enabled;
  document.getElementById('audio-toggle').innerText = `Sound: ${TactileAudio.enabled ? 'On' : 'Off'}`;
}

// Mouse Spotlight Effect on Cards
document.addEventListener('mousemove', (e) => {
  document.querySelectorAll('.card').forEach(card => {
    const rect = card.getBoundingClientRect();
    card.style.setProperty('--mouse-x', `${e.clientX - rect.left}px`);
    card.style.setProperty('--mouse-y', `${e.clientY - rect.top}px`);
  });
});

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('button, select').forEach(el => {
    el.addEventListener('mouseenter', () => TactileAudio.tick());
    el.addEventListener('click', () => TactileAudio.click());
  });
});

// =====================================================================
// CLEAN TEXT TRANSITION & LOGGING
// =====================================================================
function animateValue(id, target) {
  const el = document.getElementById(id);
  const pool = "0123456789ABCDEF";
  let step = 0;
  const timer = setInterval(() => {
    el.innerText = target.split("").map((ch, idx) => {
      if (ch === ":" || ch === "-" || ch === ".") return ch;
      if (idx < step) return target[idx];
      return pool[Math.floor(Math.random() * pool.length)];
    }).join("");
    if (step >= target.length) clearInterval(timer);
    step += 1;
  }, 18);
}

function log(msg, highlight = false) {
  const out = document.getElementById('console-output');
  const ts = new Date().toLocaleTimeString('de-DE');
  const row = document.createElement('div');
  row.className = 'log-entry';
  row.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg ${highlight ? 'white' : 'muted'}">${msg}</span>`;
  out.appendChild(row);
  out.scrollTop = out.scrollHeight;
}

setInterval(() => {
  document.getElementById('clock').innerText = new Date().toLocaleTimeString('de-DE');
}, 1000);

function setLoading(active, statusText = "ELEVATED SESSION") {
  const bar = document.getElementById('loader-bar');
  document.getElementById('state-text').innerText = statusText;
  if (active) bar.classList.add('active');
  else bar.classList.remove('active');
}

// =====================================================================
// API CALLS
// =====================================================================
let adapters = [];

function formatMac(mac) {
  if (!mac || mac.length !== 12) return mac;
  return mac.match(/.{1,2}/g).join(':');
}

async function api(path, body = {}) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  return await res.json();
}

async function refreshStatus() {
  const data = await api('/api/status');
  adapters = data.adapters;
  const sel = document.getElementById('adapter-select');
  const prev = sel.value;
  sel.innerHTML = '';
  adapters.forEach((a, idx) => {
    const opt = document.createElement('option');
    opt.value = idx;
    opt.innerText = a.name;
    sel.appendChild(opt);
  });
  if (prev && adapters[prev]) sel.value = prev;

  const active = adapters[sel.value || 0] || { mac: 'N/A', name: 'No Adapter' };
  animateValue('val-mac', formatMac(active.mac));
  animateValue('val-host', data.hostname);
  animateValue('val-ip', data.ip);
  animateValue('val-gw', data.gateway);
  document.getElementById('mac-badge').innerText = active.mac === 'HARDWARE' ? 'FACTORY' : 'SPOOFED';
}

function onAdapterSelect() {
  const a = adapters[document.getElementById('adapter-select').value];
  if (a) {
    animateValue('val-mac', formatMac(a.mac));
    log(`Switched active interface to ${a.name}`);
  }
}

function currentAdapter() {
  return adapters[document.getElementById('adapter-select').value || 0];
}

async function oneClickSpoof() {
  const a = currentAdapter();
  if (!a) return;
  const vendor = document.getElementById('mac-vendor').value;
  const profile = document.getElementById('host-profile').value;
  const customMac = document.getElementById('custom-mac').value.trim();
  const customHost = document.getElementById('custom-host').value.trim();

  setLoading(true, "ROTATING IDENTITY...");
  log("Executing full identity rotation (MAC + Hostname + DHCP/ARP Flush)...", true);

  const res = await api('/api/rotate', {
    subkey: a.id,
    desc: a.name,
    vendor,
    profile,
    custom_mac: customMac,
    custom_host: customHost
  });

  setLoading(false);
  TactileAudio.done();
  log(`Identity rotated -> Host: ${res.hostname} | MAC: ${formatMac(res.mac)}`, true);
  await refreshStatus();
}

async function spoofMacOnly() {
  const a = currentAdapter();
  if (!a) return;
  const vendor = document.getElementById('mac-vendor').value;
  const customMac = document.getElementById('custom-mac').value.trim();

  setLoading(true, "APPLYING MAC...");
  log(`Writing new Layer-2 MAC to registry (${a.name})...`);
  const res = await api('/api/mac', { subkey: a.id, desc: a.name, vendor, custom_mac: customMac });
  setLoading(false);
  TactileAudio.done();
  log(`Active MAC updated to ${formatMac(res.mac)}`, true);
  await refreshStatus();
}

async function spoofHostOnly() {
  const profile = document.getElementById('host-profile').value;
  const customHost = document.getElementById('custom-host').value.trim();

  setLoading(true, "UPDATING HOSTNAME...");
  log("Updating Windows TCP/IP & ComputerName registry keys...");
  const res = await api('/api/hostname', { profile, custom_host: customHost });
  setLoading(false);
  TactileAudio.done();
  log(`Device hostname set to ${res.hostname}`, true);
  await refreshStatus();
}

async function restoreHardwareMac() {
  const a = currentAdapter();
  if (!a) return;
  setLoading(true, "RESTORING FACTORY MAC...");
  log("Removing custom NetworkAddress override from registry...");
  await api('/api/restore', { subkey: a.id, desc: a.name });
  setLoading(false);
  TactileAudio.done();
  log("Restored physical hardware MAC address.", true);
  await refreshStatus();
}

async function flushStack() {
  setLoading(true, "FLUSHING NETWORK STACK...");
  log("Releasing DHCP lease, clearing ARP table, NetBIOS & DNS cache...");
  await api('/api/flush');
  setLoading(false);
  TactileAudio.done();
  log("Network stack flushed and DHCP lease renewed.", true);
  await refreshStatus();
}

async function wipeHistory() {
  setLoading(true, "CLEANING REGISTRY PROFILES...");
  log("Purging Windows NetworkList registry signatures & resetting Winsock...");
  const res = await api('/api/wipe_history');
  setLoading(false);
  TactileAudio.done();
  log(`Removed ${res.removed} cached network profiles & reset Winsock catalog.`, true);
}

async function setDns(provider) {
  const a = currentAdapter();
  if (!a) return;
  setLoading(true, "UPDATING DNS...");
  log(`Configuring DNS resolver (${provider.toUpperCase()}) on ${a.name}...`);
  await api('/api/dns', { desc: a.name, provider });
  setLoading(false);
  TactileAudio.done();
  log(`DNS resolver switched to ${provider.toUpperCase()}.`, true);
}

window.addEventListener('load', async () => {
  log("Monolith Kernel initialized. Ready.");
  await refreshStatus();
});
</script>
</body>
</html>
"""

# =====================================================================
# LOCAL HTTP BRIDGE & APP WINDOW LAUNCHER
# =====================================================================

class MonolithHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = HTML_UI.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}

        if self.path == "/api/status":
            self._send_json({
                "adapters": get_network_adapters(),
                "hostname": get_current_hostname(),
                "ip": get_local_ip(),
                "gateway": get_gateway_ip()
            })

        elif self.path == "/api/rotate":
            custom_mac = payload.get("custom_mac", "").replace(":", "").replace("-", "").strip()
            new_mac = custom_mac if len(custom_mac) == 12 else generate_mac(payload.get("vendor", "random"))
            custom_host = payload.get("custom_host", "").strip()
            new_host = custom_host if custom_host else generate_hostname(payload.get("profile", "desktop"))

            apply_hostname(new_host)
            apply_mac(payload["subkey"], payload["desc"], new_mac)
            flush_network()
            self._send_json({"status": "ok", "mac": new_mac, "hostname": new_host})

        elif self.path == "/api/mac":
            custom_mac = payload.get("custom_mac", "").replace(":", "").replace("-", "").strip()
            new_mac = custom_mac if len(custom_mac) == 12 else generate_mac(payload.get("vendor", "random"))
            apply_mac(payload["subkey"], payload["desc"], new_mac)
            run_cmd("ipconfig /renew")
            self._send_json({"status": "ok", "mac": new_mac})

        elif self.path == "/api/hostname":
            custom_host = payload.get("custom_host", "").strip()
            new_host = custom_host if custom_host else generate_hostname(payload.get("profile", "desktop"))
            apply_hostname(new_host)
            self._send_json({"status": "ok", "hostname": new_host})

        elif self.path == "/api/restore":
            apply_mac(payload["subkey"], payload["desc"], None)
            flush_network()
            self._send_json({"status": "ok"})

        elif self.path == "/api/flush":
            flush_network()
            self._send_json({"status": "ok"})

        elif self.path == "/api/wipe_history":
            removed = clean_network_profiles()
            self._send_json({"status": "ok", "removed": removed})

        elif self.path == "/api/dns":
            apply_dns(payload["desc"], payload.get("provider", "cloudflare"))
            self._send_json({"status": "ok"})

def launch_window(url):
    profile_dir = os.path.join(tempfile.gettempdir(), "Monolith_UI_Profile")
    browsers = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    ]
    for b in browsers:
        if os.path.exists(b):
            subprocess.Popen([
                b,
                f"--app={url}",
                "--window-size=1100,720",
                f"--user-data-dir={profile_dir}",
                "--disable-extensions",
                "--no-first-run"
            ])
            return
    os.system(f'start msedge --app="{url}" --window-size=1100,720')

def main():
    ensure_admin()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MonolithHandler)
    url = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=launch_window, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()

if __name__ == "__main__":
    main()