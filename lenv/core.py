import subprocess
import os
import sys
import json
from pathlib import Path
from datetime import datetime
import urllib.request
import hashlib
import time

# ─── Network isolation constants ───────────────────────────────────────────────
LENV_BRIDGE   = "lenv-br0"
LENV_GATEWAY  = "10.100.0.1"
LENV_SUBNET   = "10.100.0.0/16"
# ───────────────────────────────────────────────────────────────────────────────


class LENV:
    def __init__(self, project_path=None, distro_set=None):
        self.project_path = project_path or os.getcwd()
        self.project_name = os.path.basename(self.project_path)

        self._path_hash = hashlib.md5(
            str(Path(self.project_path).absolute()).encode()
        ).hexdigest()[:8]

        self.instance_name = f"lenv-{self.project_name}-{self._path_hash}"
        self.config_dir  = Path(self.project_path) / ".lenv"
        self.config_file = self.config_dir / "config.json"

        self.lenv_home = Path.home() / ".lenv"
        self.lenv_home.mkdir(exist_ok=True)

        self.rootfs_cache = self.lenv_home / "rootfs"
        self.rootfs_cache.mkdir(exist_ok=True)

        self.distro_set = distro_set
        self.instance_ip = None          # filled after network setup

    # ── Config ─────────────────────────────────────────────────────────────────

    def _load_config(self):
        if self.config_file.exists():
            with open(self.config_file) as f:
                config = json.load(f)
            self.instance_name = config.get("instance_name", self.instance_name)
            self.distro_set    = config.get("distro",         self.distro_set)
            self.instance_ip   = config.get("ip",             self.instance_ip)

    # ── WSL helpers ────────────────────────────────────────────────────────────

    def _check_wsl2_installed(self):
        try:
            result = subprocess.run(
                ["wsl", "--status"], capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _check_wsl2_version(self):
        try:
            result = subprocess.run(
                ["wsl", "--status"], capture_output=True, text=True
            )
            return "WSL 2" in result.stdout or "version: 2" in result.stdout
        except Exception:
            return False

    def _install_wsl2(self):
        print(" WSL2 is not installed on your system.")
        print("\n Installing WSL2...")
        print("\nOption 1: Automatic Installation (Recommended)")
        print("Run this command in PowerShell as Administrator:")
        print("  wsl --install --no-distribution")
        print("\nOption 2: Manual Installation")
        print("Follow: https://docs.microsoft.com/en-us/windows/wsl/install")

        response = input("\nDo you want LENV to attempt automatic installation? (y/n): ")

        if response.lower() == "y":
            print("\n Attempting to install WSL2...")
            print("Note: This requires Administrator privileges.")
            try:
                subprocess.run(
                    ["powershell", "-Command", "Start-Process", "wsl",
                     "-ArgumentList '--install --no-distribution'", "-Verb", "RunAs"],
                    capture_output=True, text=True,
                )
                print("\n WSL2 installation initiated.")
                print("  You may need to restart your computer.")
                print("After restart, run 'lenv init' again.")
                sys.exit(0)
            except Exception as e:
                print(f"\n Failed to auto-install: {e}")
                print("Please install WSL2 manually using PowerShell as admin:")
                print("  wsl --install --no-distribution")
                sys.exit(1)
        else:
            print("\nPlease install WSL2 and run 'lenv init' again.")
            sys.exit(1)

    # ── Distro choice ──────────────────────────────────────────────────────────

    def _distro_choice(self):
        print("\n Choose your Linux distribution:")
        print("\n1. Alpine Linux (Recommended)")
        print("   - Lightweight (~3MB)")
        print("   - Fast startup")
        print("   - Minimal resource usage")
        print("\n2. Ubuntu 22.04")
        print("   - Full-featured (~50MB)")
        print("   - More packages available")
        print("   - Familiar environment")
        print("\n3. For Custom Distro Choice")

        while True:
            choice = input("\nEnter your choice (1 or 2 or 3): ").strip()
            if choice == "1":
                return "alpine"
            elif choice == "2":
                return "ubuntu"
            elif choice == "3":
                return "custom"
            else:
                print("Invalid choice. Please enter 1, 2 or 3.")

    # ── Rootfs download ────────────────────────────────────────────────────────

    def _download_rootfs(self):
        """Download minimal Linux rootfs"""
        rootfs_urls = {
            "alpine": {
                "url": "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-minirootfs-3.19.0-x86_64.tar.gz",
                "filename": "alpine-minirootfs-3.19.0-x86_64.tar.gz",
                "size_mb": 3,
            },
            "ubuntu": {
                "url": "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz",
                "filename": "ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz",
                "size_mb": 50,
            },
        }

        self.distro_set = self._distro_choice()

        if self.distro_set not in rootfs_urls:
            raise ValueError(f"Unknown distro: {self.distro_set}")

        info = rootfs_urls[self.distro_set]
        rootfs_path = self.rootfs_cache / info["filename"]

        if rootfs_path.exists():
            print(f"Using cached {self.distro_set} rootfs")
            return str(rootfs_path)

        print(f"Downloading {self.distro_set} rootfs (~{info['size_mb']}MB)...")
        print(f"   From: {info['url']}")

        try:
            def reporthook(count, block_size, total_size):
                percent = int(count * block_size * 100 / total_size)
                sys.stdout.write(f"\r   Progress: {percent}%")
                sys.stdout.flush()

            urllib.request.urlretrieve(info["url"], rootfs_path, reporthook=reporthook)
            print("\n Download complete")
            return str(rootfs_path)

        except Exception as e:
            print(f"\n Download failed: {e}")
            print(f"Please download manually from: {info['url']}")
            print(f"Save to: {rootfs_path}")
            sys.exit(1)

    # ── Network isolation ──────────────────────────────────────────────────────

    def _assign_ip(self):
        """
        Derive a stable, unique IP (10.100.X.Y) from the instance name hash.
        Uses _path_hash (8 hex chars = 32 bits) to fill the last two octets.
        Avoids .0.1 (gateway) and .255.255 (broadcast).
        """
        h = int(self._path_hash, 16)          # 0 – 4 294 967 295
        third  = (h >> 8) & 0xFF              # bits 8-15  → 0-255
        fourth = h & 0xFF                     # bits 0-7   → 0-255

        # Avoid reserved addresses
        if third == 0 and fourth <= 1:
            fourth = 2
        if third == 255 and fourth == 255:
            fourth = 254

        return f"10.100.{third}.{fourth}"

    def _veth_name(self):
        """Interface names are capped at 15 chars in Linux."""
        return f"vlenv-{self._path_hash[:6]}"      # 12 chars

    def _setup_network(self):
        """
        Create a veth pair for this instance and attach it to the lenv bridge.
        All lenv instances share the same WSL2 kernel network namespace, so the
        bridge is visible to every instance — each just gets its own veth + IP.

        Topology:
            Windows
              └── WSL2 kernel namespace
                    ├── lenv-br0  (10.100.0.1)   ← shared bridge
                    │     ├── vlenv-XXXXXX-br    ← bridge end of veth pair A
                    │     └── vlenv-YYYYYY-br    ← bridge end of veth pair B
                    ├── vlenv-XXXXXX  (10.100.x.y)  ← instance A's interface
                    └── vlenv-YYYYYY  (10.100.a.b)  ← instance B's interface
        """
        print(" Setting up network isolation...")

        self.instance_ip = self._assign_ip()
        veth      = self._veth_name()          # vlenv-abc123
        veth_br   = f"{veth}-br"              # vlenv-abc123-br  (14 chars ✓)

        # Full setup script — runs inside the WSL instance as root
        # NOTE: No 'set -e' — every step is independent and idempotent
        script = f"""
# ── 1. Install networking tools if missing ──────────────────────────────
if ! command -v ip > /dev/null 2>&1; then
    if command -v apk > /dev/null 2>&1; then
        apk add --quiet iproute2 iptables 2>/dev/null
    elif command -v apt-get > /dev/null 2>&1; then
        apt-get install -y -qq iproute2 iptables 2>/dev/null
    fi
fi

# ── 2. Create the shared bridge (once, idempotent) ──────────────────────
if ! ip link show {LENV_BRIDGE} > /dev/null 2>&1; then
    ip link add {LENV_BRIDGE} type bridge 2>/dev/null
    ip addr add {LENV_GATEWAY}/16 dev {LENV_BRIDGE} 2>/dev/null
    ip link set {LENV_BRIDGE} up 2>/dev/null
fi

# ── 3. Create veth pair for THIS instance (idempotent) ──────────────────
# Both ends of the pair are created in ONE command, then configured separately
if ! ip link show {veth} > /dev/null 2>&1; then
    ip link add {veth} type veth peer name {veth_br} 2>/dev/null
fi

# Configure bridge end (may already exist if veth was re-created)
ip link show {veth_br} > /dev/null 2>&1 && ip link set {veth_br} master {LENV_BRIDGE} 2>/dev/null
ip link show {veth_br} > /dev/null 2>&1 && ip link set {veth_br} up 2>/dev/null

# Configure instance end
ip link show {veth} > /dev/null 2>&1 && ip link set {veth} up 2>/dev/null
ip link show {veth} > /dev/null 2>&1 && ip addr replace {self.instance_ip}/16 dev {veth} 2>/dev/null

# ── 4. Routing & NAT (so instances can reach the internet) ──────────────
echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null
iptables -t nat -C POSTROUTING -s {LENV_SUBNET} -j MASQUERADE 2>/dev/null \
    || iptables -t nat -A POSTROUTING -s {LENV_SUBNET} -j MASQUERADE 2>/dev/null

# ── 5. Persist config so network re-applies on WSL restart ──────────────
mkdir -p /etc/profile.d
PROFILE=/etc/profile.d/lenv-net.sh
printf '%s\n' \
  '# lenv network isolation - re-apply on shell start' \
  '_lenv_net() {' \
  "  ip link show {LENV_BRIDGE} > /dev/null 2>&1 || ip link add {LENV_BRIDGE} type bridge 2>/dev/null" \
  "  ip link show {LENV_BRIDGE} > /dev/null 2>&1 && ip addr replace {LENV_GATEWAY}/16 dev {LENV_BRIDGE} 2>/dev/null" \
  "  ip link show {LENV_BRIDGE} > /dev/null 2>&1 && ip link set {LENV_BRIDGE} up 2>/dev/null" \
  "  ip link show {veth} > /dev/null 2>&1 || ip link add {veth} type veth peer name {veth_br} 2>/dev/null" \
  "  ip link show {veth_br} > /dev/null 2>&1 && ip link set {veth_br} master {LENV_BRIDGE} 2>/dev/null" \
  "  ip link show {veth_br} > /dev/null 2>&1 && ip link set {veth_br} up 2>/dev/null" \
  "  ip link show {veth} > /dev/null 2>&1 && ip link set {veth} up 2>/dev/null" \
  "  ip link show {veth} > /dev/null 2>&1 && ip addr replace {self.instance_ip}/16 dev {veth} 2>/dev/null" \
  "  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null" \
  '}' \
  '_lenv_net' \
  > "$PROFILE"
chmod +x "$PROFILE"
"""

        shell = "bash" if self.distro_set == "ubuntu" else "ash"
        result = subprocess.run(
            ["wsl", "-d", self.instance_name, "--", shell, "-c", script],
            capture_output=True, text=True,
        )

        if result.returncode != 0:
            print(f"  Warning: Network setup had errors:\n  {result.stderr.strip()}")
        else:
            print(f" Network ready — instance IP: {self.instance_ip}")

    def _teardown_network(self):
        """Remove the veth pair for this instance from the bridge."""
        veth    = self._veth_name()
        veth_br = f"{veth}-br"

        script = f"""
ip link del {veth}    2>/dev/null || true
ip link del {veth_br} 2>/dev/null || true
"""
        # Use any running lenv or fall back to the default WSL distro
        subprocess.run(
            ["wsl", "-d", self.instance_name, "--", "sh", "-c", script],
            capture_output=True, text=True,
        )

    # ── Instance creation & configuration ─────────────────────────────────────

    def _create_wsl_instance(self):
        if not self._check_wsl2_installed():
            self._install_wsl2()
            return

        if not self._check_wsl2_version():
            print(" WSL is installed but may be version 1.")
            print("Setting WSL 2 as default...")
            subprocess.run(["wsl", "--set-default-version", "2"])

        rootfs_tar   = self._download_rootfs()
        install_path = str(self.lenv_home / "instances" / self.instance_name)
        Path(install_path).mkdir(parents=True, exist_ok=True)

        print(f"Creating WSL instance '{self.instance_name}'...")

        result = subprocess.run(
            ["wsl", "--import", self.instance_name, install_path, rootfs_tar],
            capture_output=True, text=True,
        )

        if result.returncode != 0:
            if "already exists" in result.stderr.lower():
                print(f"Instance '{self.instance_name}' already exists")
            else:
                raise Exception(f"Failed to create WSL instance: {result.stderr}")

        self._configure_instance()
        print("WSL instance created successfully")

    def _configure_instance(self):
        """Install base packages and configure networking."""
        print(" Installing Python and essential tools...")
        self._load_config()

        if self.distro_set == "alpine":
            shell_rcd = "ash"
            commands  = ["apk update"]
        elif self.distro_set == "ubuntu":
            shell_rcd = "bash"
            commands  = ["apt-get update"]
        else:
            shell_rcd = "ash"
            print(f"  Unknown distro: {self.distro_set}, skipping configuration")
            return

        for cmd in commands:
            result = subprocess.run(
                ["wsl", "-d", self.instance_name, "--", shell_rcd, "-c", cmd],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"  Warning: Command failed: {cmd}")
                print(f"   {result.stderr}")

        print("Configuration complete")

        # ── Network isolation ──
        self._setup_network()

    # ── Public commands ────────────────────────────────────────────────────────

    def init(self):
        self.config_dir.mkdir(exist_ok=True)

        if self.distro_set is None:
            self.distro_set = self._distro_choice()

        self._create_wsl_instance()

        config = {
            "instance_name": self.instance_name,
            "distro":        self.distro_set,
            "ip":            self.instance_ip,        # ← persisted IP
            "created_at":    datetime.utcnow().isoformat(),
        }

        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)

        # BUG FIX: project_path is a str, must wrap in Path() before using /
        gitignore = Path(self.project_path) / ".gitignore"
        with open(gitignore, "a") as f:
            f.write(".lenv\n")

        print(f"LENV instance initialised: {self.instance_name}")
        if self.instance_ip:
            print(f"Instance IP: {self.instance_ip}")
        print(f"\nNext steps:")
        print(f"  lenv activate    # Enter Linux environment")

    def _windows_to_wsl_path(self, windows_path):
        path  = Path(windows_path).absolute()
        drive = path.drive.lower().replace(":", "")
        rest  = str(path).replace(path.drive, "").replace("\\", "/")
        return f"/mnt/{drive}{rest}"

    def activate(self):
        if not self.config_file.exists():
            print("No LENV environment found. Run 'lenv init' first.")
            return
        self._load_config()

        wsl_path  = self._windows_to_wsl_path(self.project_path)
        shell_rcd = "bash" if self.distro_set == "ubuntu" else "ash"

        print(f"Entering Linux environment '{self.instance_name}'...")
        if self.instance_ip:
            print(f"Instance IP: {self.instance_ip}")
        print("Type 'exit' to return to Windows")

        subprocess.run([
            "wsl", "-d", self.instance_name,
            "--cd", wsl_path, "--", shell_rcd,
        ])

        print("Exited Linux environment")

    def run(self, command):
        self._load_config()
        wsl_path  = self._windows_to_wsl_path(self.project_path)
        shell_rcd = "bash" if self.distro_set == "ubuntu" else "ash"

        result = subprocess.run(
            ["wsl", "-d", self.instance_name,
             "--cd", wsl_path, "--", shell_rcd, "-c", command],
            capture_output=True, text=True,
        )

        print(result.stdout)
        if result.stderr:
            print(result.stderr)

        return result.returncode

    def destroy(self):
        self._load_config()
        if not self.config_file.exists():
            print("No LENV environment found")
            return

        with open(self.config_file) as f:
            config = json.load(f)

        instance_name = config.get("instance_name", self.instance_name)

        # ── Tear down veth before unregistering ──
        self._teardown_network()

        subprocess.run(
            ["wsl", "--terminate", instance_name],
            capture_output=True, timeout=10,
        )
        time.sleep(2)

        subprocess.run(
            ["wsl", "--unregister", self.instance_name],
            capture_output=True, text=True, timeout=20,
        )

        import shutil
        if self.config_dir.exists():
            shutil.rmtree(self.config_dir)

        print(f"Destroyed environment: {self.instance_name}")

    def status(self):
        self._load_config()
        print(f"Project:  {self.project_name}")
        print(f"Path:     {self.project_path}")

        if not self.config_file.exists():
            print("Status:   Not initialized")
            return

        print("Status:   Initialized")

        if self.instance_ip:
            print(f"IP:       {self.instance_ip}")

        result = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True, text=True,
        )

        if self.instance_name in result.stdout:
            print(f"WSL Instance:  {self.instance_name}")

            result = subprocess.run(
                ["wsl", "--list", "--running"],
                capture_output=True, text=True,
            )
            if self.instance_name in result.stdout:
                print("State:    Running")
            else:
                print("State:    Stopped")
        else:
            print("WSL Instance:  Not found")
