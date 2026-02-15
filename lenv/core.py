import subprocess
import os
import sys
import json
from pathlib import Path
from datetime import datetime
import urllib.request
import hashlib
import time

class LENV:
    def __init__(self, project_path = None):
        self.project_path = project_path or os.getcwd()
        self.project_name = os.path.basename(self.project_path)

        path_hash = hashlib.md5(
            str(Path(self.project_path).absolute()).encode()
        ).hexdigest()[:8]

        self.instance_name = f"lenv-{self.project_name}-{path_hash}"
        self.config_dir = Path(self.project_path) / ".lenv"
        self.config_file = self.config_dir / "config.json"

        self.lenv_home = Path.home() / ".lenv"
        self.lenv_home.mkdir(exist_ok=True)

        self.rootfs_cache = self.lenv_home / "rootfs"
        self.rootfs_cache.mkdir(exist_ok=True)

    def _check_wsl2_installed(self):
        try:
            result = subprocess.run([
                "wsl", "--status"
            ], capture_output=True, text=True, timeout=5)
            return result.returncode==0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    
    def _check_wsl2_version(self):
        try:
            result = subprocess.run([
                "wsl", "--status"
            ], capture_output=True, text=True)
            return "WSL 2" in result.stdout or "version: 2" in result.stdout
        except:
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
        
        if response.lower() == 'y':
            print("\n Attempting to install WSL2...")
            print("Note: This requires Administrator privileges.")
            
            # Try to run wsl --install (requires admin)
            try:
                result = subprocess.run(
                    ["powershell", "-Command", "Start-Process", "wsl", 
                     "-ArgumentList '--install --no-distribution'", "-Verb", "RunAs"],
                    capture_output=True,
                    text=True
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

        while(True):
            choice = input("\nEnter your choice (1 or 2): ").strip()
            if choice=='1':
                return 'alpine'
            elif choice=='2':
                return 'ubuntu'
            else:
                ('invalid choice. Please enter 1 or 2.')


    def _download_rootfs(self, distro="alpine"):
        """Download minimal Linux rootfs"""
        
        rootfs_urls = {
            "alpine": {
                "url": "https://dl-cdn.alpinelinux.org/alpine/v3.19/releases/x86_64/alpine-minirootfs-3.19.0-x86_64.tar.gz",
                "filename": "alpine-minirootfs-3.19.0-x86_64.tar.gz",
                "size_mb": 3
            },
            "ubuntu": {
                "url": "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz",
                "filename": "ubuntu-22.04-minimal-cloudimg-amd64-root.tar.xz",
                "size_mb": 50
            }
        }
        
        if distro not in rootfs_urls:
            raise ValueError(f"Unknown distro: {distro}")
        
        info = rootfs_urls[distro]
        rootfs_path = self.rootfs_cache / info["filename"]
        
        # Check if already downloaded
        if rootfs_path.exists():
            print(f"Using cached {distro} rootfs")
            return str(rootfs_path)
        
        # Download
        print(f"Downloading {distro} rootfs (~{info['size_mb']}MB)...")
        print(f"   From: {info['url']}")
        
        try:
            # Download with progress
            def reporthook(count, block_size, total_size):
                percent = int(count * block_size * 100 / total_size)
                sys.stdout.write(f"\r   Progress: {percent}%")
                sys.stdout.flush()
            
            urllib.request.urlretrieve(
                info["url"],
                rootfs_path,
                reporthook=reporthook
            )
            print("\n Download complete")
            return str(rootfs_path)
            
        except Exception as e:
            print(f"\n Download failed: {e}")
            
            # Fallback: suggest manual download
            print("\n  Automatic download failed.")
            print(f"Please download manually from: {info['url']}")
            print(f"Save to: {rootfs_path}")
            sys.exit(1)

    
    
    def _create_wsl_instance(self, distro="alpine"):
        if not self._check_wsl2_installed():
            self._install_wsl2()
            return

        if not self._check_wsl2_version():
            print(" WSL is installed but may be version 1.")
            print("Setting WSL 2 as default...")
            subprocess.run(["wsl", "--set-default-version", "2"])

        

        rootfs_tar = self._download_rootfs(distro)
        install_path = str(self.lenv_home / "instances" / self.instance_name)
        Path(install_path).mkdir(parents=True, exist_ok=True)

        print(f"Creating WSL instance '{self.instance_name}'...")

        result = subprocess.run([
            "wsl", "--import", 
            self.instance_name,
            install_path,
            rootfs_tar 
        ], capture_output=True, text=True)

        if result.returncode!=0:
            if "already exists" in result.stderr.lower():
                print(f"Instance '{self.instance_name}' already exists")

            else:
                raise Exception(f"Failed to create WSL instance: {result.stderr}")

        self._configure_instance()
        print("WSL instance created successfully")
        
    def _configure_instance(self, distro='alpine'):
        """Install Python and essential tools in the instance"""
        print(" Installing Python and essential tools...")
        
        if distro == "alpine":
            commands = [
                # Update package manager
                "apk update",
                # Install Python, pip, and build essentials
                "apk add python3 py3-pip gcc python3-dev musl-dev linux-headers",
                # Create symlinks
                "ln -sf /usr/bin/python3 /usr/bin/python",
                # Upgrade pip
                "python -m pip install --upgrade pip"
            ]
        elif distro == "ubuntu":
            commands = [
                # Update package manager
                "apt-get update",
                # Install Python, pip, and build essentials
                "apt-get install -y python3 python3-pip python3-dev build-essential",
                # Create symlinks
                "ln -sf /usr/bin/python3 /usr/bin/python",
                # Upgrade pip
                "python -m pip install --upgrade pip"
            ]
        else:
            print(f"  Unknown distro: {distro}, skipping configuration")
            return
        
        for cmd in commands:
            result = subprocess.run([
                "wsl", "-d", self.instance_name,
                "--", "sh", "-c", cmd
            ], capture_output=True, text=True)
            
            if result.returncode != 0:
                print(f"  Warning: Command failed: {cmd}")
                print(f"   {result.stderr}")
        
        print("✓ Configuration complete")

    def init(self, distro=None):
        self.config_dir.mkdir(exist_ok=True)
        if distro==None:
            distro = self._distro_choice()
            
        self._create_wsl_instance()

        config = {
            "instance_name":self.instance_name,
            "distro": distro,
            "created_at": datetime.utcnow().isoformat()
        }

        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=2)
        
        

        print(f"LENV instance intialized: {self.instance_name}")
        print(f"\nNext steps:")
        print(f"  lenv activate    # Enter Linux environment")

    def _windows_to_wsl_path(self, windows_path):
        path = Path(windows_path).absolute()
        drive = path.drive.lower().replace(":", "")
        rest = str(path).replace(path.drive, '').replace('\\', '/')
        return f"/mnt/{drive}{rest}"
    
    def activate(self):
        if not self.config_file.exists():
            print("No LENV environment found. Run 'lenv init' first.")
            return
        
        wsl_path = self._windows_to_wsl_path(self.project_path)

        print(f"Entering Linux environment '{self.instance_name}'...")
        print("Type 'exit' to return to Windows")

        subprocess.run([
            "wsl", "-d",
            self.instance_name, "--cd",
            wsl_path, "--", "/bin/sh"
        ])

        print("Exited Linux environment")

    def run(self,command):
        wsl_path = self._windows_to_wsl_path(self.project_path)

        result = subprocess.run([
            "wsl", "-d",
            self.instance_name, "--cd",
            wsl_path, "--", "sh",
            "-c", command
        ], capture_output=True, text=True)

        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        
        return result.returncode

    def destroy(self):
        if not self.config_file.exists():
            print("No LENV enviroment found")
            return
        with open(self.config_file) as f:
            config = json.load(f)

        instance_name = config.get('instance_name', self.instance_name)
        wsl_path = self._windows_to_wsl_path(self.project_path)

        subprocess.run([
            'wsl', '--terminate', instance_name
        ], capture_output=True, timeout=10)

        time.sleep(2)


        subprocess.run([
            "wsl", "--unregister", self.instance_name
        ], capture_output=True, text=True, timeout=20)

        import shutil
        if self.config_dir.exists():
            shutil.rmtree(self.config_dir)

        print(f"Destroyed environment: {self.instance_name}")

    

    def status(self):
        """Show environment status"""
        print(f"Project: {self.project_name}")
        print(f"Path: {self.project_path}")

        if not self.config_file.exists():
            print("Status:  Not initialized")
            return

        print("Status:  Initialized")

        # Check if WSL instance exists
        result = subprocess.run(
            ["wsl", "--list", "--quiet"],
            capture_output=True,
            text=True
        )

        if self.instance_name in result.stdout:
            print(f"WSL Instance:  {self.instance_name}")

            # Check if running
            result = subprocess.run(
                ["wsl", "--list", "--running"],
                capture_output=True,
                text=True
            )

            if self.instance_name in result.stdout:
                print("State:  Running")
            else:
                print("State:  Stopped")
        else:
            print(f"WSL Instance:  Not found")
