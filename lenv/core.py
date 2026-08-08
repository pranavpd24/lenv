import subprocess
import os
import sys
import re
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import time

# NOTE: heavy modules (urllib.request, tarfile, tempfile) are imported lazily
# inside the functions that need them. They pull in http.client/email.* and cost
# ~60-70ms of interpreter startup — most of the gap between `lenv run` and a
# compiled CLI — but are only used on the download/validate paths.

# ─── Network isolation constants ───────────────────────────────────────────────
LENV_BRIDGE   = "lenv-br0"
LENV_GATEWAY  = "10.100.0.1"
LENV_SUBNET   = "10.100.0.0/16"
# ───────────────────────────────────────────────────────────────────────────────

# Instance names created by lenv always look like: lenv-<folder>-<8 hex chars>.
# Anything loaded from .lenv/config.json that does NOT match this pattern is
# rejected, so a tampered config can never point lenv at an unrelated distro.
_INSTANCE_NAME_RE = re.compile(r"^lenv-[A-Za-z0-9._-]+-[0-9a-f]{8}$")

# Package names from build files are interpolated into shell commands, so they
# must be validated before use.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$")


class LENV:
    def __init__(self, project_path=None, distro_set=None, rootfs_path=None, build=None):
        self.project_path = project_path or os.getcwd()

        # The folder name becomes part of the WSL instance name — restrict it
        # to characters that are safe for WSL distro names and shell usage.
        raw_name = os.path.basename(self.project_path) or "project"
        self.project_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-.") or "project"

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
        self.rootfs_path = rootfs_path
        self.build = build                 # optional build name (lenv/builds/<name>.yaml)
        self.instance_ip = None            # filled after network setup

    # ── Config ─────────────────────────────────────────────────────────────────

    def _load_config(self):
        if self.config_file.exists():
            with open(self.config_file) as f:
                config = json.load(f)
            stored_name = config.get("instance_name")
            # Only trust a stored instance name that matches the lenv naming
            # scheme; otherwise fall back to the name computed from the path.
            if stored_name and _INSTANCE_NAME_RE.fullmatch(stored_name):
                self.instance_name = stored_name
            self.distro_set  = config.get("distro", self.distro_set)
            self.instance_ip = config.get("ip",     self.instance_ip)
            self.build       = config.get("build",  self.build)

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
        # wsl --status emits UTF-16 on Windows — decode via _wsl_output so the
        # version check actually sees the text instead of garbage bytes.
        try:
            output = self._wsl_output(["--status"]).lower()
            return "wsl 2" in output or "version: 2" in output or "version 2" in output
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

    def _wsl_output(self, args):
        result = subprocess.run(["wsl"]+ args, capture_output=True,timeout=10)
        raw = result.stdout
        if b"\x00" in raw:
            return raw.decode("utf-16-le", errors="replace")
        return raw.decode("utf-8", errors="replace")


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

    def _format_size(self, size_bytes):
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024 or unit == 'TB':
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024

    def _sha256_file(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def _fetch_expected_sha256(self, info):
        """
        Fetch the upstream SHA-256 checksum for a rootfs.
        Handles both Alpine-style '<hash>  <filename>' .sha256 files and
        Ubuntu-style SHA256SUMS lists ('<hash> *<filename>').
        Returns the lowercase hex digest, or None if it cannot be determined.
        """
        import urllib.request   # lazy: pulls in http.client/email.*, ~60ms startup
        try:
            with urllib.request.urlopen(info["sha256_url"], timeout=15) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return None

        for line in text.splitlines():
            parts = line.replace("*", " ").split()
            if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                if parts[1] == info["filename"]:
                    return parts[0].lower()
            elif len(parts) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                return parts[0].lower()
        return None

    def _validate_custom_rootfs(self, path):
        """Validate a user-supplied rootfs tarball (extension + real tar check)."""
        import tarfile   # lazy: only needed on this path, costs startup time
        if not path.exists():
            print(f"Provided rootfs path does not exist: {path}")
            sys.exit(1)
        if path.suffixes[-2:] not in (['.tar', '.gz'], ['.tar', '.xz']) and path.suffix != '.tar':
            print(f"Provided rootfs path is not a valid tarball: {path}")
            sys.exit(1)
        if not tarfile.is_tarfile(path):
            print(f"Provided rootfs is not a readable tar archive: {path}")
            sys.exit(1)
        print(f"Using custom rootfs ({self._format_size(path.stat().st_size)})")
        return str(path)

    def _download_rootfs(self):
        """Download minimal Linux rootfs, with SHA-256 integrity verification."""
        if self.rootfs_path:
            return self._validate_custom_rootfs(Path(self.rootfs_path))

        rootfs_urls = {
            "alpine": {
                "url": "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-minirootfs-3.19.0-x86_64.tar.gz",
                "filename": "alpine-minirootfs-3.19.0-x86_64.tar.gz",
                "size_mb": 3,
                "sha256_url": "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-minirootfs-3.19.0-x86_64.tar.gz.sha256",
            },
            "ubuntu": {
                "url": "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz",
                "filename": "ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz",
                "size_mb": 50,
                "sha256_url": "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/SHA256SUMS",
            },
        }

        if self.distro_set is None:
            self.distro_set = self._distro_choice() 

        if self.distro_set not in rootfs_urls:
            distro_path = Path(input("Enter the path to your custom rootfs tarball: ").strip())
            return self._validate_custom_rootfs(distro_path)


        info = rootfs_urls[self.distro_set]
        rootfs_path = self.rootfs_cache / info["filename"]
        expected = self._fetch_expected_sha256(info)

        if rootfs_path.exists():
            if expected:
                if self._sha256_file(rootfs_path) == expected:
                    print(f"Using cached {self.distro_set} rootfs (checksum verified)")
                    return str(rootfs_path)
                print("  Cached rootfs failed checksum verification - re-downloading.")
                rootfs_path.unlink()
            else:
                print("  Warning: checksum unavailable, using cached rootfs unverified.")
                return str(rootfs_path)

        if expected is None:
            # Fail closed: never install a fresh download we cannot verify.
            print("Could not fetch the official SHA-256 checksum - refusing to")
            print("download an unverified rootfs. Check your connection and retry.")
            sys.exit(1)

        print(f"Downloading {self.distro_set} rootfs (~{info['size_mb']}MB)...")
        print(f"   From: {info['url']}")

        def reporthook(count, block_size, total_size):
            if total_size and total_size > 0:
                percent = min(100, int(count * block_size * 100 / total_size))
                sys.stdout.write(f"\r   Progress: {percent}%")
            else:
                sys.stdout.write(f"\r   Downloaded: {self._format_size(count * block_size)}")
            sys.stdout.flush()

        # Download to a temp file first so a failed/partial download can never
        # be mistaken for a valid cached rootfs on the next run.
        # Lazy imports: both are only needed on the download path.
        import tempfile
        import urllib.request
        fd, tmp_name = tempfile.mkstemp(dir=self.rootfs_cache, prefix=".dl-", suffix=".part")
        os.close(fd)
        tmp_path = Path(tmp_name)

        try:
            urllib.request.urlretrieve(info["url"], tmp_name, reporthook=reporthook)
            print("\n Download complete")
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            print(f"\n Download failed: {e}")
            print(f"Please download manually from: {info['url']}")
            print(f"Save to: {rootfs_path}")
            sys.exit(1)

        if self._sha256_file(tmp_path) != expected:
            tmp_path.unlink(missing_ok=True)
            print(" Downloaded rootfs FAILED SHA-256 verification - file deleted.")
            print("The upstream mirror may be corrupted; try again later.")
            sys.exit(1)

        print(" SHA-256 checksum verified")
        tmp_path.replace(rootfs_path)   # atomic move within the same directory
        return str(rootfs_path)

    # ── Builds (bundled package sets) ───────────────────────────────────────────

    def _load_build(self, name):
        """
        Load a bundled build definition from lenv/builds/<name>.yaml.
        Supports the simple subset of YAML used by the bundled files:

            name: minimal
            description: ...
            distro: alpine
            packages:
              - ca-certificates
        """
        if not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            print(f"Invalid build name: {name!r}")
            sys.exit(1)

        path = Path(__file__).resolve().parent / "builds" / f"{name}.yaml"
        if not path.exists():
            available = ", ".join(sorted(p.stem for p in path.parent.glob("*.yaml"))) or "none"
            print(f"Unknown build '{name}'. Available builds: {available}")
            sys.exit(1)

        build = {"name": name, "description": "", "distro": None, "packages": []}
        in_packages = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                if in_packages:
                    pkg = stripped[2:].strip()
                    if not _PACKAGE_NAME_RE.fullmatch(pkg):
                        print(f"Unsafe package name in {path.name}: {pkg!r}")
                        sys.exit(1)
                    build["packages"].append(pkg)
                continue
            in_packages = False
            key, sep, value = stripped.partition(":")
            if not sep:
                continue
            key, value = key.strip(), value.strip()
            if key == "packages":
                in_packages = True
            elif key in ("description", "distro"):
                build[key] = value or None

        return build

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
        veth_br   = f"{veth}-br"              # vlenv-abc123-br  (14 chars)

        # Full setup script — runs inside the WSL instance as root
        # NOTE: No 'set -e' — every step is independent and idempotent
        # All interpolated values are lenv-generated (hex hashes, computed IPs),
        # never raw user input.
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
# NOTE: no shell variables here — wsl.exe expands every $VAR in the command
# line against the distro environment before ash ever runs (even inside
# single quotes), so $PROFILE would arrive pre-expanded (and empty).
mkdir -p /etc/profile.d
printf '%s\n' \
  '# lenv network isolation - re-apply on shell start' \
  '_lenv_net() {{' \
  "  ip link show {LENV_BRIDGE} > /dev/null 2>&1 || ip link add {LENV_BRIDGE} type bridge 2>/dev/null" \
  "  ip link show {LENV_BRIDGE} > /dev/null 2>&1 && ip addr replace {LENV_GATEWAY}/16 dev {LENV_BRIDGE} 2>/dev/null" \
  "  ip link show {LENV_BRIDGE} > /dev/null 2>&1 && ip link set {LENV_BRIDGE} up 2>/dev/null" \
  "  ip link show {veth} > /dev/null 2>&1 || ip link add {veth} type veth peer name {veth_br} 2>/dev/null" \
  "  ip link show {veth_br} > /dev/null 2>&1 && ip link set {veth_br} master {LENV_BRIDGE} 2>/dev/null" \
  "  ip link show {veth_br} > /dev/null 2>&1 && ip link set {veth_br} up 2>/dev/null" \
  "  ip link show {veth} > /dev/null 2>&1 && ip link set {veth} up 2>/dev/null" \
  "  ip link show {veth} > /dev/null 2>&1 && ip addr replace {self.instance_ip}/16 dev {veth} 2>/dev/null" \
  "  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null" \
  '}}' \
  '_lenv_net' \
  > /etc/profile.d/lenv-net.sh
chmod +x /etc/profile.d/lenv-net.sh
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

        # wsl.exe writes status/errors as UTF-16 — decode like _wsl_output,
        # otherwise the "already exists" check never matches and the error
        # message prints as garbage.
        result = subprocess.run(
            ["wsl", "--import", self.instance_name, install_path, rootfs_tar],
            capture_output=True,
        )
        stderr = result.stderr
        stderr = (stderr.decode("utf-16-le", errors="replace") if b"\x00" in stderr
                  else stderr.decode("utf-8", errors="replace"))

        if result.returncode != 0:
            if "already exists" in stderr.lower():
                print(f"Instance '{self.instance_name}' already exists")
            else:
                raise Exception(f"Failed to create WSL instance: {stderr}")

        self._configure_instance()
        print("WSL instance created successfully")

    def _configure_instance(self):
        """Install base packages (plus optional build packages) and networking."""
        print(" Installing essential packages and configuring instance...")
        self._load_config()

        if self.distro_set == "alpine":
            shell_rcd = "ash"
            commands  = ["apk update"]
        elif self.distro_set == "ubuntu":
            shell_rcd = "bash"
            commands  = ["apt-get update"]
        else:
            shell_rcd = "ash"
            commands  = []
            print(f"  Unknown distro: {self.distro_set}, skipping package setup")

        # Optional build: install the bundled package set for this distro.
        if self.build and commands:
            build = self._load_build(self.build)
            if build["distro"] and build["distro"] != self.distro_set:
                print(f"  Build '{self.build}' targets {build['distro']}, but this "
                      f"environment is {self.distro_set} - skipping its packages.")
            elif build["packages"]:
                pkgs = " ".join(build["packages"])
                print(f" Installing build '{self.build}' packages: {pkgs}")
                if self.distro_set == "alpine":
                    commands.append(f"apk add --no-cache {pkgs}")
                else:
                    commands.append(
                        f"DEBIAN_FRONTEND=noninteractive apt-get install -y "
                        f"--no-install-recommends {pkgs}"
                    )

        for cmd in commands:
            result = subprocess.run(
                ["wsl", "-d", self.instance_name, "--", shell_rcd, "-c", cmd],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"  Warning: Command failed: {cmd}")
                print(f"   {result.stderr}")

        print("Configuration complete")

        # ── Network isolation (applies to every distro, including custom) ──
        self._setup_network()

    # ── Public commands ────────────────────────────────────────────────────────

    def init(self):
        self.config_dir.mkdir(exist_ok=True)

        # Validate the build early (bad name/unknown build fails before any
        # download), and let the build pick the distro when the user didn't.
        if self.build:
            build = self._load_build(self.build)
            if self.distro_set is None and build["distro"]:
                self.distro_set = build["distro"]

        if self.distro_set is None:
            self.distro_set = self._distro_choice()

        self._create_wsl_instance()

        config = {
            "instance_name": self.instance_name,
            "distro":        self.distro_set,
            "ip":            self.instance_ip,        # ← persisted IP
            "build":         self.build,
            "created_at":    datetime.now(timezone.utc).isoformat(),
        }

        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)

        # BUG FIX: project_path is a str, must wrap in Path() before using /
        gitignore = Path(self.project_path) / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""

        if ".lenv" not in existing.splitlines():
            with open(gitignore, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
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
        if not self.config_file.exists():
            print("No LENV environment found. Run 'lenv init' first.")
            return 1
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

    def destroy(self, assume_yes=False):
        if not self.config_file.exists():
            print("No LENV environment found")
            return
        self._load_config()

        with open(self.config_file) as f:
            config = json.load(f)

        instance_name = config.get("instance_name", self.instance_name)

        # Never unregister anything that is not a lenv-managed instance name.
        # A tampered .lenv/config.json must not be able to wipe an unrelated
        # WSL distro (e.g. the user's main Ubuntu install).
        if not _INSTANCE_NAME_RE.fullmatch(instance_name):
            print(f"Refusing to destroy: '{instance_name}' is not a lenv-managed instance.")
            return

        if not assume_yes:
            print(f"This will permanently delete the WSL instance '{instance_name}'")
            print("and everything inside it.")
            try:
                answer = input("Continue? (Y/n): ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                print("Aborted.")
                return

        # ── Tear down veth before unregistering ──
        self._teardown_network()

        subprocess.run(
            ["wsl", "--terminate", instance_name],
            capture_output=True, timeout=10,
        )
        time.sleep(2)

        subprocess.run(
            ["wsl", "--unregister", instance_name],
            capture_output=True, text=True, timeout=20,
        )

        if self.config_dir.exists():
            shutil.rmtree(self.config_dir)

        # Also drop the install directory (~/.lenv/instances/<name>) left behind
        install_dir = self.lenv_home / "instances" / instance_name
        if install_dir.exists():
            shutil.rmtree(install_dir, ignore_errors=True)

        print(f"Destroyed environment: {instance_name}")


    @staticmethod
    def _vhdx_size(path):
        """On-disk (allocated) size of a VHDX file — sparse-aware, unlike
        os.path.getsize which reports the virtual (apparent) size."""
        import ctypes
        gcf = ctypes.windll.kernel32.GetCompressedFileSizeW
        gcf.restype = ctypes.c_ulong
        gcf.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        hi = ctypes.c_ulong(0)
        lo = gcf(str(path), ctypes.byref(hi))
        return (hi.value << 32) | lo

    def compact(self, assume_yes=False):
        """
        Reclaim disk space from an environment's virtual disk.
        WSL2 VHDX files grow as the instance writes data but never shrink on
        their own. Tries WSL's sparse-VHD reclaim first; if the WSL build has
        that disabled (current builds do, over corruption concerns), rebuilds
        the disk via export/re-import, which needs no elevation.
        """
        if not self.config_file.exists():
            print("No LENV environment found")
            return
        self._load_config()

        vhdx = self.lenv_home / "instances" / self.instance_name / "ext4.vhdx"
        if not vhdx.exists():
            print(f"No virtual disk found at {vhdx}")
            return

        before_apparent = vhdx.stat().st_size
        before_disk = self._vhdx_size(vhdx)

        # The VHDX must not be attached while it is modified
        subprocess.run(["wsl", "--terminate", self.instance_name],
                       capture_output=True, timeout=10)
        time.sleep(1)

        result = subprocess.run(
            ["wsl", "--manage", self.instance_name, "--set-sparse", "true"],
            capture_output=True,
        )
        # wsl.exe prints status/errors to stdout, not stderr
        raw = result.stdout + result.stderr
        msg = (raw.decode("utf-16-le", "replace") if b"\x00" in raw
               else raw.decode("utf-8", "replace")).strip()

        if result.returncode == 0:
            after_disk = self._vhdx_size(vhdx)
            print(f"VHDX marked sparse: {vhdx}")
            print(f"  virtual size: {self._format_size(before_apparent)}")
            print(f"  on disk: {self._format_size(before_disk)} -> {self._format_size(after_disk)}")
            print("  Deleted files are now returned to Windows automatically")
            print("  (on delete/fstrim inside the environment).")
            return

        if "allow-unsafe" in msg:
            # Current WSL builds gate sparse VHDs behind --allow-unsafe due to
            # potential data corruption. Do NOT use it. Host-side compact tools
            # (diskpart/Optimize-VHD) can't help either: blocks freed inside
            # ext4 still contain stale data, and Windows can't read ext4's
            # allocation bitmap. Rebuilding the disk from an export contains
            # only live files, so it always reclaims everything.
            print("This WSL version has sparse VHDs disabled (data-corruption risk),")
            print("and diskpart cannot reclaim ext4-deleted blocks. Rebuilding the")
            print("disk via export/re-import instead — files are preserved.")
        else:
            print(f"wsl --manage failed ({msg or 'unknown error'}).")
            print("Rebuilding the disk via export/re-import instead — files are preserved.")

        print(f"  virtual size: {self._format_size(before_apparent)}")
        print(f"  on disk now:  {self._format_size(before_disk)}")

        if not assume_yes:
            try:
                answer = input("Rebuild now? (Y/n): ").strip().lower()
            except EOFError:
                answer = ""
            if answer != "y":
                print("Aborted.")
                return

        import tempfile
        fd, tar_name = tempfile.mkstemp(dir=self.lenv_home, prefix=".compact-",
                                        suffix=".tar")
        os.close(fd)
        tar = Path(tar_name)
        try:
            # 1. Export first — the environment is only touched after a
            #    complete, non-trivial archive exists.
            result = subprocess.run(
                ["wsl", "--export", self.instance_name, str(tar)],
                capture_output=True, timeout=3600,
            )
            if result.returncode != 0 or tar.stat().st_size < 1024 * 1024:
                print("  Export failed — environment left untouched.")
                return

            # 2. Rebuild the disk from the archive.
            subprocess.run(["wsl", "--terminate", self.instance_name],
                           capture_output=True, timeout=10)
            time.sleep(1)
            subprocess.run(["wsl", "--unregister", self.instance_name],
                           capture_output=True, timeout=30)
            install_path = str(self.lenv_home / "instances" / self.instance_name)
            result = subprocess.run(
                ["wsl", "--import", self.instance_name, install_path, str(tar)],
                capture_output=True, timeout=3600,
            )
            if result.returncode != 0:
                print("  Re-import FAILED. Your data is safe in this archive:")
                print(f"    {tar}")
                print(f"  Restore with: wsl --import {self.instance_name} "
                      f"\"{install_path}\" \"{tar}\"")
                return
        finally:
            if tar.exists() and self.instance_name in self._wsl_output(["--list", "--quiet"]):
                tar.unlink(missing_ok=True)

        after_disk = self._vhdx_size(vhdx)
        print(f"  on disk: {self._format_size(before_disk)} -> {self._format_size(after_disk)}")


    def list_instances(self):
        result = self._wsl_output(["--list", "--verbose"])
        rows = []
        for line in result.splitlines():
            line = line.strip().lstrip("*").strip()
            if not line.startswith("lenv-"):
                continue

            parts = line.split()
            name = parts[0]
            state = parts[1] if len(parts)>1 else "Unknown"
            rows.append((name, state))

        if not rows:
            print("No lenv environments found on this machine.")
            return

        print(f"{'INSTANCE': <35} {'STATE': <10}")
        print("#"*45)
        for name, state in rows:
            print(f"{name: <35} {state: <10}")


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

        if self.build:
            print(f"Build:    {self.build}")


        if self.instance_name in self._wsl_output(["--list", "--quiet"]):
            print(f"WSL Instance:  {self.instance_name}")

            if self.instance_name in self._wsl_output(["--list", "--running"]):
                print("State:    Running")
            else:
                print("State:    Stopped")
        else:
            print("WSL Instance:  Not found")
