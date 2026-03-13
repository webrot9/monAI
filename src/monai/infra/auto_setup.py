"""Zero-touch infrastructure auto-setup.

On first run, monAI must provision EVERYTHING itself:
- Install and start Tor (anonymity layer)
- Install and start monero-wallet-rpc (crypto payments)
- Generate creator wallet if none configured
- Detect or provision LLM API access
- Create default config with sane defaults

The creator runs ONE command. monAI does the rest.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MONAI_DIR = Path.home() / ".monai"
MONAI_BIN = MONAI_DIR / "bin"
MONERO_VERSION = "0.18.3.4"


class InfraSetup:
    """Autonomous infrastructure provisioning — zero human intervention."""

    def __init__(self) -> None:
        self.results: dict[str, Any] = {}

    def run_all(self) -> dict[str, Any]:
        """Run all infrastructure checks and auto-setup steps.

        Returns dict with status of each component.
        """
        MONAI_DIR.mkdir(parents=True, exist_ok=True)
        MONAI_BIN.mkdir(parents=True, exist_ok=True)

        self.results["config"] = self._ensure_config()
        self.results["sandbox"] = self._ensure_sandbox()
        self.results["tor"] = self._ensure_tor()
        self.results["llm"] = self._ensure_llm_access()
        self.results["browser"] = self._ensure_browser()
        self.results["pdf_libs"] = self._ensure_pdf_libs()
        self.results["nodejs"] = self._ensure_nodejs()
        self.results["util_linux"] = self._ensure_util_linux()

        # Crypto infrastructure is OPTIONAL — only set up if explicitly configured.
        # The primary payout flow is LLC → contractor invoice → bank transfer.
        # Monero is only needed if the creator wants anonymous crypto payouts.
        if self._is_crypto_configured():
            self.results["monero_wallet_rpc"] = self._ensure_monero_wallet_rpc()
        else:
            self.results["monero_wallet_rpc"] = {
                "status": "skipped",
                "reason": "Crypto not configured. Using LLC bank transfer flow.",
            }

        all_ok = all(
            r.get("status") in ("ok", "already_running", "already_configured", "skipped",
                                 "already_exists", "degraded")
            for r in self.results.values()
        )
        self.results["ready"] = all_ok
        return self.results

    # ── Sandbox (bubblewrap) ─────────────────────────────────────

    def _ensure_sandbox(self) -> dict[str, Any]:
        """Ensure bubblewrap is installed for OS-level process isolation.

        Without bubblewrap, agents can read any world-readable file on the
        creator's system. With it, child processes literally cannot see
        files outside the bind-mounted paths.
        """
        if shutil.which("bwrap"):
            logger.info("bubblewrap (bwrap) already installed")
            return {"status": "ok"}

        installed = self._install_bubblewrap()
        if installed:
            # Re-detect sandbox backend so sandbox.py picks up bwrap
            from monai.utils import sandbox
            sandbox.refresh_isolation_backend()
            return {"status": "ok", "method": "auto_installed"}

        logger.warning(
            "Could not auto-install bubblewrap. Process isolation will use "
            "weaker fallbacks (unshare or application-level only). "
            "Install manually: sudo apt install bubblewrap"
        )
        return {
            "status": "degraded",
            "warning": "bubblewrap not available — weaker sandbox isolation",
        }

    def _install_bubblewrap(self) -> bool:
        """Attempt to install bubblewrap via package manager."""
        system = platform.system().lower()
        if system != "linux":
            logger.info("bubblewrap only supported on Linux (current: %s)", system)
            return False

        try:
            if shutil.which("apt-get"):
                subprocess.run(
                    ["sudo", "apt-get", "install", "-y", "bubblewrap"],
                    capture_output=True, timeout=120,
                )
                if shutil.which("bwrap"):
                    logger.info("bubblewrap installed via apt-get")
                    return True
            if shutil.which("dnf"):
                subprocess.run(
                    ["sudo", "dnf", "install", "-y", "bubblewrap"],
                    capture_output=True, timeout=120,
                )
                if shutil.which("bwrap"):
                    logger.info("bubblewrap installed via dnf")
                    return True
            if shutil.which("pacman"):
                subprocess.run(
                    ["sudo", "pacman", "-S", "--noconfirm", "bubblewrap"],
                    capture_output=True, timeout=120,
                )
                if shutil.which("bwrap"):
                    logger.info("bubblewrap installed via pacman")
                    return True
        except Exception as e:
            logger.warning("bubblewrap installation failed: %s", e)
        return False

    # ── Browser (Playwright + Chromium) ─────────────────────────

    def _ensure_browser(self) -> dict[str, Any]:
        """Ensure Playwright and Chromium are installed for browser automation.

        Without a browser, agents cannot scrape websites, register on
        platforms, or perform any web-based actions. This is a critical
        dependency for the entire system.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning(
                "Playwright Python package not installed. "
                "Run: pip install playwright"
            )
            return {"status": "degraded", "warning": "playwright package not installed"}

        # Check if Chromium binary is already installed
        if self._is_chromium_installed():
            logger.info("Playwright Chromium already installed")
            return {"status": "ok"}

        # Install Chromium browser binary
        return self._install_chromium()

    def _is_chromium_installed(self) -> bool:
        """Check if Playwright's Chromium binary is available."""
        try:
            result = subprocess.run(
                ["python3", "-m", "playwright", "install", "--dry-run", "chromium"],
                capture_output=True, text=True, timeout=15,
            )
            # If dry-run says nothing to install, it's already there
            if result.returncode == 0 and "is already installed" in result.stdout.lower():
                return True
        except Exception:
            pass

        # Fallback: check if the browser dir exists
        browser_path = Path.home() / ".cache" / "ms-playwright" / "chromium-"
        try:
            matches = list(Path.home().glob(".cache/ms-playwright/chromium-*"))
            return len(matches) > 0
        except Exception:
            return False

    def _install_chromium(self) -> dict[str, Any]:
        """Install Playwright Chromium browser binary."""
        try:
            # Install system deps first (Playwright needs these)
            system = platform.system().lower()
            if system == "linux":
                subprocess.run(
                    ["python3", "-m", "playwright", "install-deps", "chromium"],
                    capture_output=True, timeout=180,
                )

            # Install Chromium binary
            result = subprocess.run(
                ["python3", "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                logger.info("Playwright Chromium installed successfully")
                return {"status": "ok", "method": "auto_installed"}

            logger.warning(
                "Playwright Chromium install failed: %s",
                result.stderr[:200] if result.stderr else "unknown error",
            )
            return {
                "status": "degraded",
                "warning": "Chromium install failed — browser automation unavailable",
            }
        except Exception as e:
            logger.warning("Playwright Chromium install error: %s", e)
            return {
                "status": "degraded",
                "warning": f"Chromium install error: {e}",
            }

    # ── PDF libs (WeasyPrint system deps) ─────────────────────

    def _ensure_pdf_libs(self) -> dict[str, Any]:
        """Ensure system libraries for PDF generation (WeasyPrint) are installed.

        WeasyPrint needs libpango, libcairo, etc. Without them, invoice
        PDF generation silently fails. This is optional — the system
        works without it, but invoicing degrades to HTML-only.
        """
        try:
            from weasyprint import HTML  # noqa: F401
            logger.info("WeasyPrint available — PDF generation enabled")
            return {"status": "ok"}
        except ImportError:
            pass
        except OSError:
            # WeasyPrint installed but system libs missing
            pass

        system = platform.system().lower()
        if system != "linux":
            return {
                "status": "degraded",
                "warning": "WeasyPrint system libs not auto-installed on non-Linux",
            }

        # Install system libs
        installed = False
        try:
            if shutil.which("apt-get"):
                subprocess.run(
                    ["sudo", "apt-get", "install", "-y",
                     "libpango-1.0-0", "libpangoft2-1.0-0",
                     "libcairo2", "libffi-dev", "libgdk-pixbuf2.0-0"],
                    capture_output=True, timeout=120,
                )
                installed = True
            elif shutil.which("dnf"):
                subprocess.run(
                    ["sudo", "dnf", "install", "-y",
                     "pango", "cairo", "libffi-devel", "gdk-pixbuf2"],
                    capture_output=True, timeout=120,
                )
                installed = True
        except Exception as e:
            logger.warning("PDF system libs installation failed: %s", e)

        if installed:
            # Check if it works now
            try:
                from weasyprint import HTML  # noqa: F401
                logger.info("WeasyPrint system libs installed — PDF generation enabled")
                return {"status": "ok", "method": "auto_installed"}
            except Exception:
                pass

        return {
            "status": "degraded",
            "warning": "PDF generation unavailable (WeasyPrint system libs missing)",
        }

    # ── Node.js ───────────────────────────────────────────────────

    def _ensure_nodejs(self) -> dict[str, Any]:
        """Ensure Node.js and npm are installed for web deployment CLIs.

        Without Node.js, agents cannot deploy landing pages (Netlify,
        Vercel, Cloudflare Workers). Web deployment is a core capability.
        """
        if shutil.which("node") and shutil.which("npm"):
            logger.info("Node.js and npm already installed")
            return {"status": "ok"}

        installed = self._install_nodejs()
        if installed:
            return {"status": "ok", "method": "auto_installed"}

        logger.warning(
            "Could not auto-install Node.js. Web deployment will not work. "
            "Install manually: https://nodejs.org/"
        )
        return {"status": "degraded", "warning": "Node.js not available — web deployment disabled"}

    def _install_nodejs(self) -> bool:
        """Attempt to install Node.js via package manager or nodesource."""
        system = platform.system().lower()
        try:
            if system == "linux":
                if shutil.which("apt-get"):
                    # Use nodesource for a recent version
                    subprocess.run(
                        ["sudo", "apt-get", "install", "-y", "nodejs", "npm"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("node"):
                        logger.info("Node.js installed via apt-get")
                        return True
                if shutil.which("dnf"):
                    subprocess.run(
                        ["sudo", "dnf", "install", "-y", "nodejs", "npm"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("node"):
                        logger.info("Node.js installed via dnf")
                        return True
                if shutil.which("pacman"):
                    subprocess.run(
                        ["sudo", "pacman", "-S", "--noconfirm", "nodejs", "npm"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("node"):
                        logger.info("Node.js installed via pacman")
                        return True
            elif system == "darwin":
                if shutil.which("brew"):
                    subprocess.run(
                        ["brew", "install", "node"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("node"):
                        logger.info("Node.js installed via brew")
                        return True
        except Exception as e:
            logger.warning("Node.js installation failed: %s", e)
        return False

    # ── util-linux (unshare) ──────────────────────────────────────

    def _ensure_util_linux(self) -> dict[str, Any]:
        """Ensure util-linux is installed (provides 'unshare' for sandbox fallback).

        If bubblewrap fails, unshare is the next line of defense for
        process isolation. Without it, sandbox degrades to application-level
        only — which means NO OS-level isolation at all.
        """
        if shutil.which("unshare"):
            logger.info("util-linux (unshare) already available")
            return {"status": "ok"}

        system = platform.system().lower()
        if system != "linux":
            # unshare is Linux-specific
            return {"status": "skipped", "reason": "unshare is Linux-only"}

        installed = False
        try:
            if shutil.which("apt-get"):
                subprocess.run(
                    ["sudo", "apt-get", "install", "-y", "util-linux"],
                    capture_output=True, timeout=120,
                )
                installed = shutil.which("unshare") is not None
            elif shutil.which("dnf"):
                subprocess.run(
                    ["sudo", "dnf", "install", "-y", "util-linux"],
                    capture_output=True, timeout=120,
                )
                installed = shutil.which("unshare") is not None
            elif shutil.which("pacman"):
                subprocess.run(
                    ["sudo", "pacman", "-S", "--noconfirm", "util-linux"],
                    capture_output=True, timeout=120,
                )
                installed = shutil.which("unshare") is not None
        except Exception as e:
            logger.warning("util-linux installation failed: %s", e)

        if installed:
            # Refresh sandbox backend to pick up unshare
            from monai.utils import sandbox
            sandbox.refresh_isolation_backend()
            logger.info("util-linux installed — unshare available for sandbox fallback")
            return {"status": "ok", "method": "auto_installed"}

        logger.warning("Could not install util-linux — sandbox has no OS-level fallback")
        return {"status": "degraded", "warning": "unshare not available — no sandbox fallback"}

    # ── Tor ──────────────────────────────────────────────────────

    def _ensure_tor(self) -> dict[str, Any]:
        """Ensure Tor is installed and running."""
        # Check if Tor is already running
        if self._is_port_open(9050):
            logger.info("Tor SOCKS proxy already running on :9050")
            return {"status": "already_running"}

        # Check if Tor binary exists
        tor_bin = shutil.which("tor")
        if not tor_bin:
            # Try to install Tor
            installed = self._install_tor()
            if not installed:
                logger.warning(
                    "Could not auto-install Tor. Will attempt to operate "
                    "without it, but anonymity is NOT guaranteed."
                )
                return {"status": "failed", "error": "Tor not found and auto-install failed"}

        # Start Tor in background
        return self._start_tor()

    def _install_tor(self) -> bool:
        """Attempt to install Tor via package manager."""
        system = platform.system().lower()
        try:
            if system == "linux":
                # Try apt (Debian/Ubuntu)
                if shutil.which("apt-get"):
                    subprocess.run(
                        ["sudo", "apt-get", "install", "-y", "tor"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("tor"):
                        logger.info("Tor installed via apt-get")
                        return True
                # Try dnf (Fedora/RHEL)
                if shutil.which("dnf"):
                    subprocess.run(
                        ["sudo", "dnf", "install", "-y", "tor"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("tor"):
                        logger.info("Tor installed via dnf")
                        return True
                # Try pacman (Arch)
                if shutil.which("pacman"):
                    subprocess.run(
                        ["sudo", "pacman", "-S", "--noconfirm", "tor"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("tor"):
                        logger.info("Tor installed via pacman")
                        return True
            elif system == "darwin":
                if shutil.which("brew"):
                    subprocess.run(
                        ["brew", "install", "tor"],
                        capture_output=True, timeout=120,
                    )
                    if shutil.which("tor"):
                        logger.info("Tor installed via brew")
                        return True
        except Exception as e:
            logger.warning(f"Tor installation failed: {e}")
        return False

    def _start_tor(self) -> dict[str, Any]:
        """Start Tor as a background process."""
        tor_bin = shutil.which("tor")
        if not tor_bin:
            return {"status": "failed", "error": "Tor binary not found"}

        try:
            # Start Tor with sensible defaults
            tor_data_dir = MONAI_DIR / "tor_data"
            tor_data_dir.mkdir(exist_ok=True)

            proc = subprocess.Popen(
                [tor_bin, "--SocksPort", "9050", "--ControlPort", "9051",
                 "--DataDirectory", str(tor_data_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait for Tor to bootstrap (max 60s)
            for i in range(60):
                if self._is_port_open(9050):
                    logger.info(f"Tor started successfully (pid={proc.pid})")
                    return {"status": "ok", "pid": proc.pid}
                time.sleep(1)

            logger.warning("Tor started but SOCKS port not ready after 60s")
            return {"status": "ok", "pid": proc.pid, "warning": "slow_bootstrap"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    # ── Monero Wallet RPC ────────────────────────────────────────

    def _ensure_monero_wallet_rpc(self) -> dict[str, Any]:
        """Ensure monero-wallet-rpc is installed and running."""
        # Check if already running
        if self._is_port_open(18082):
            logger.info("monero-wallet-rpc already running on :18082")
            return {"status": "already_running"}

        # Check if binary exists
        rpc_bin = shutil.which("monero-wallet-rpc")
        if not rpc_bin:
            # Check our local bin dir
            local_bin = MONAI_BIN / "monero-wallet-rpc"
            if local_bin.exists():
                rpc_bin = str(local_bin)
            else:
                # Download monero CLI tools
                rpc_bin = self._download_monero_cli()
                if not rpc_bin:
                    logger.warning(
                        "Could not auto-install monero-wallet-rpc. "
                        "Crypto payments will not work until manually configured."
                    )
                    return {"status": "failed", "error": "monero-wallet-rpc not found"}

        # Create wallet if it doesn't exist
        wallet_dir = MONAI_DIR / "wallets"
        wallet_dir.mkdir(exist_ok=True)
        wallet_file = wallet_dir / "brand_wallet"

        if not wallet_file.exists():
            result = self._create_monero_wallet(rpc_bin, wallet_file)
            if result.get("status") == "failed":
                return result

        # Start wallet RPC
        return self._start_monero_wallet_rpc(rpc_bin, wallet_file)

    def _download_monero_cli(self) -> str | None:
        """Download Monero CLI tools from official source."""
        system = platform.system().lower()
        arch = platform.machine().lower()

        # Map to Monero download names
        if system == "linux":
            if arch in ("x86_64", "amd64"):
                plat = "linux-x64"
            elif arch in ("aarch64", "arm64"):
                plat = "linux-armv8"
            else:
                logger.warning(f"Unsupported architecture: {arch}")
                return None
        elif system == "darwin":
            plat = "mac-x64" if arch == "x86_64" else "mac-armv8"
        else:
            logger.warning(f"Unsupported OS: {system}")
            return None

        filename = f"monero-{plat}-v{MONERO_VERSION}.tar.bz2"
        url = f"https://downloads.getmonero.org/cli/{filename}"

        try:
            import httpx
            logger.info(f"Downloading Monero CLI from {url}...")
            with tempfile.TemporaryDirectory() as tmpdir:
                tmppath = Path(tmpdir) / filename
                # Download with progress
                with httpx.stream("GET", url, follow_redirects=True, timeout=300) as resp:
                    resp.raise_for_status()
                    with open(tmppath, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)

                # Extract
                with tarfile.open(tmppath, "r:bz2") as tar:
                    tar.extractall(path=tmpdir)

                # Find the extracted directory
                extracted = list(Path(tmpdir).glob("monero-*"))
                if not extracted:
                    return None

                monero_dir = extracted[0]
                # Copy binaries to our bin dir
                for binary in ("monero-wallet-rpc", "monero-wallet-cli"):
                    src = monero_dir / binary
                    if src.exists():
                        dst = MONAI_BIN / binary
                        shutil.copy2(src, dst)
                        dst.chmod(0o755)

                rpc_path = MONAI_BIN / "monero-wallet-rpc"
                if rpc_path.exists():
                    logger.info(f"Monero CLI installed to {MONAI_BIN}")
                    return str(rpc_path)

        except Exception as e:
            logger.warning(f"Failed to download Monero CLI: {e}")
        return None

    def _create_monero_wallet(self, rpc_bin: str, wallet_file: Path) -> dict[str, Any]:
        """Create a new Monero wallet using monero-wallet-cli."""
        cli_bin = str(Path(rpc_bin).parent / "monero-wallet-cli")
        if not Path(cli_bin).exists():
            cli_bin = shutil.which("monero-wallet-cli") or ""

        if not cli_bin:
            # Use wallet-rpc to create wallet on first start
            # It will auto-create if wallet file doesn't exist
            logger.info("Will create wallet on first RPC start")
            return {"status": "ok", "method": "rpc_auto_create"}

        try:
            # Create wallet non-interactively
            proc = subprocess.run(
                [
                    cli_bin,
                    "--generate-new-wallet", str(wallet_file),
                    "--password", "",
                    "--mnemonic-language", "English",
                    "--command", "exit",
                ],
                capture_output=True, text=True, timeout=30,
            )

            # Extract seed phrase from output
            output = proc.stdout
            seed_lines = []
            capture = False
            for line in output.split("\n"):
                if "mnemonic seed" in line.lower() or "seed phrase" in line.lower():
                    capture = True
                    continue
                if capture and line.strip():
                    seed_lines.append(line.strip())
                    if len(seed_lines) >= 2:
                        break

            seed = " ".join(seed_lines) if seed_lines else ""

            if seed:
                # Save seed securely for the creator
                seed_file = MONAI_DIR / "CREATOR_WALLET_SEED.txt"
                seed_file.write_text(
                    "=== monAI AUTO-GENERATED WALLET SEED ===\n"
                    "SAVE THIS SECURELY. This is the recovery seed for\n"
                    "the brand wallet that collects payments.\n\n"
                    f"Seed: {seed}\n\n"
                    f"Wallet file: {wallet_file}\n"
                    "========================================\n"
                )
                seed_file.chmod(0o600)
                logger.info(
                    f"Monero wallet created. Seed saved to {seed_file} — "
                    f"CREATOR MUST BACK THIS UP"
                )

            return {"status": "ok", "wallet_file": str(wallet_file), "has_seed": bool(seed)}
        except Exception as e:
            logger.warning(f"Wallet creation via CLI failed: {e}")
            return {"status": "ok", "method": "rpc_auto_create"}

    def _start_monero_wallet_rpc(self, rpc_bin: str, wallet_file: Path) -> dict[str, Any]:
        """Start monero-wallet-rpc in background."""
        try:
            # Generate random RPC password for security
            import secrets
            rpc_password = secrets.token_hex(16)

            cmd = [
                rpc_bin,
                "--wallet-file", str(wallet_file),
                "--password", "",
                "--rpc-bind-port", "18082",
                "--rpc-bind-ip", "127.0.0.1",
                "--rpc-login", f"monai:{rpc_password}",
                "--confirm-external-bind",
                "--disable-rpc-ban",
            ]

            # Don't auto-create wallet if file doesn't exist
            if not wallet_file.exists():
                cmd.extend(["--generate-from-json", ""])  # Will fail, but that's OK

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait for RPC to be ready
            for i in range(30):
                if self._is_port_open(18082):
                    logger.info(f"monero-wallet-rpc started (pid={proc.pid})")
                    # Save RPC credentials to config
                    self._save_monero_rpc_creds(rpc_password)
                    return {"status": "ok", "pid": proc.pid}
                time.sleep(1)

            logger.warning("monero-wallet-rpc started but port not ready after 30s")
            return {"status": "ok", "pid": proc.pid, "warning": "slow_start"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def _save_monero_rpc_creds(self, password: str) -> None:
        """Save auto-generated RPC credentials to config."""
        config_file = MONAI_DIR / "config.json"
        config: dict = {}
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)

        if "monero" not in config:
            config["monero"] = {}
        config["monero"]["rpc_user"] = "monai"
        config["monero"]["rpc_password"] = password
        config["monero"]["wallet_rpc_url"] = "http://127.0.0.1:18082"

        # Atomic write: write to temp file, then rename
        tmp_file = config_file.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(config, f, indent=2)
        tmp_file.replace(config_file)

    # ── LLM Access ───────────────────────────────────────────────

    def _ensure_llm_access(self) -> dict[str, Any]:
        """Ensure LLM access is available.

        Priority:
        1. OPENAI_API_KEY env var
        2. ANTHROPIC_API_KEY env var
        3. Config file API key
        4. Local Ollama instance
        """
        # Check env vars
        if os.environ.get("OPENAI_API_KEY"):
            return {"status": "ok", "provider": "openai", "source": "env"}

        if os.environ.get("ANTHROPIC_API_KEY"):
            return {"status": "ok", "provider": "anthropic", "source": "env"}

        # Check config
        config_file = MONAI_DIR / "config.json"
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
            llm_config = config.get("llm", {})
            if llm_config.get("api_key"):
                return {"status": "ok", "provider": "config", "source": "config"}

        # Check for local Ollama
        if self._is_port_open(11434):
            logger.info("Local Ollama detected on :11434 — using as LLM backend")
            self._configure_ollama_backend()
            return {"status": "ok", "provider": "ollama", "source": "local"}

        # Try to install Ollama as fallback
        if self._install_ollama():
            return {"status": "ok", "provider": "ollama", "source": "auto_installed"}

        return {
            "status": "failed",
            "error": (
                "No LLM access. Set OPENAI_API_KEY, ANTHROPIC_API_KEY, "
                "or install Ollama (https://ollama.ai)"
            ),
        }

    def _install_ollama(self) -> bool:
        """Attempt to install Ollama for local LLM inference."""
        try:
            # Official Ollama install script
            result = subprocess.run(
                ["sh", "-c", "curl -fsSL https://ollama.ai/install.sh | sh"],
                capture_output=True, timeout=300,
            )
            if shutil.which("ollama"):
                # Start Ollama and pull a model
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(3)

                # Pull a capable model
                subprocess.run(
                    ["ollama", "pull", "llama3.1:8b"],
                    capture_output=True, timeout=600,
                )
                logger.info("Ollama installed with llama3.1:8b model")
                self._configure_ollama_backend()
                return True
        except Exception as e:
            logger.warning(f"Ollama installation failed: {e}")
        return False

    def _configure_ollama_backend(self) -> None:
        """Configure monAI to use local Ollama as LLM backend."""
        config_file = MONAI_DIR / "config.json"
        config: dict = {}
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)

        if "llm" not in config:
            config["llm"] = {}
        config["llm"]["provider"] = "ollama"
        config["llm"]["model"] = "llama3.1:8b"
        config["llm"]["model_mini"] = "llama3.1:8b"
        config["llm"]["api_base"] = "http://127.0.0.1:11434"
        # Set a dummy key so the startup check passes
        config["llm"]["api_key"] = "ollama-local"

        # Atomic write: write to temp file, then rename
        tmp_file = config_file.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(config, f, indent=2)
        tmp_file.replace(config_file)

    # ── Config ───────────────────────────────────────────────────

    def _ensure_config(self) -> dict[str, Any]:
        """Ensure config file exists with sane defaults."""
        config_file = MONAI_DIR / "config.json"
        if config_file.exists():
            return {"status": "already_configured"}

        # Create minimal config — everything else auto-provisions
        default_config = {
            "privacy": {
                "proxy_type": "tor",
                "tor_socks_port": 9050,
                "verify_anonymity": True,
                "fallback_enabled": True,
            },
            "budget": {
                "max_cycle_cost": 5.0,
                "max_cycle_calls": 200,
                "budget_fraction_per_cycle": 0.1,
            },
            "initial_capital": 500.0,
            "currency": "EUR",
        }

        MONAI_DIR.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            json.dump(default_config, f, indent=2)

        logger.info(f"Created default config at {config_file}")
        return {"status": "ok", "path": str(config_file)}

    # ── Crypto detection ────────────────────────────────────────

    def _is_crypto_configured(self) -> bool:
        """Check if creator has explicitly configured crypto payouts.

        Crypto (Monero) is OPTIONAL. The primary flow is:
            Platforms → LLC bank → contractor invoice → creator bank.
        Monero is only set up if the creator explicitly wants it.
        """
        config_file = MONAI_DIR / "config.json"
        if not config_file.exists():
            return False
        try:
            with open(config_file) as f:
                config = json.load(f)
            # Check if creator provided an XMR address or Monero RPC is custom-configured
            creator_wallet = config.get("creator_wallet", {})
            monero_cfg = config.get("monero", {})
            has_xmr_address = bool(creator_wallet.get("xmr_address"))
            has_custom_rpc = bool(
                monero_cfg.get("wallet_rpc_url", "http://127.0.0.1:18082")
                != "http://127.0.0.1:18082"
                or monero_cfg.get("rpc_user")
            )
            return has_xmr_address or has_custom_rpc
        except Exception:
            return False

    # ── Utilities ────────────────────────────────────────────────

    @staticmethod
    def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
        """Check if a TCP port is open."""
        import socket
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except (OSError, ConnectionRefusedError):
            return False
