#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ruijie Switch Configuration Collector
======================================
Automatically logs into Ruijie switches via their web management interface,
extracts the device hostname from the Web CLI response, and saves the
running-config to local files.

Author  : ruijie-config-collector contributors
License : MIT
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
import urllib3
import yaml

# ---------------------------------------------------------------------------
# Suppress SSL certificate warnings (switches often use self-signed certs)
# ---------------------------------------------------------------------------
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(message)s"
logging.basicConfig(format=LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
logger = logging.getLogger(__name__)


# ===========================================================================
# Auth helpers
# ===========================================================================

def generate_auth_token(username: str, password: str, rounds: int = 9) -> str:
    """
    Encode credentials using Ruijie's multi-layer Base64 scheme.

    Ruijie web management encodes ``username:password`` with successive rounds
    of standard Base64.  The default number of rounds is **9**, which matches
    the firmware versions observed in the field.

    Parameters
    ----------
    username : str
    password : str
    rounds   : int  (default 9)

    Returns
    -------
    str  – the value to place in the ``auth`` POST field of ``/login.do``
    """
    token: str = f"{username}:{password}"
    for _ in range(rounds):
        token = base64.b64encode(token.encode("utf-8")).decode("ascii")
    return token


def decode_auth_token(token: str) -> tuple[str, str] | None:
    """
    Attempt to reverse a Ruijie auth token.

    Iteratively Base64-decodes until the result looks like ``user:pass``.
    Returns ``(username, password)`` or ``None`` when decoding fails.
    """
    current = token
    for _ in range(30):
        try:
            decoded = base64.b64decode(current.encode("ascii")).decode("utf-8")
        except Exception:
            break
        # A plain-text credential contains exactly one ":" and no Base64 chars
        if ":" in decoded and not re.search(r"[A-Za-z0-9+/]{20,}", decoded):
            user, _, pwd = decoded.partition(":")
            return user, pwd
        current = decoded
    return None


# ===========================================================================
# Switch client
# ===========================================================================

class RuijieSwitch:
    """
    HTTP client for a single Ruijie switch.

    Workflow
    --------
    1. ``login()``          – POST to ``/login.do``, obtain session cookie
    2. ``web_cli(cmd)``     – POST to ``/web_cli.do``, parse XML response
    3. ``collect()``        – convenience method that chains the above
    """

    #: Headers that mimic a real browser session
    _BASE_HEADERS: dict[str, str] = {
        "Accept":           "text/plain, */*; q=0.01",
        "Accept-Language":  "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection":       "keep-alive",
        "Content-Type":     "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    def __init__(
        self,
        ip: str,
        auth_token: str,
        *,
        timeout: int = 10,
        verify_ssl: bool = False,
    ) -> None:
        self.ip = ip
        self.auth_token = auth_token
        self.timeout = timeout
        self.base_url = f"http://{ip}"

        self.session = requests.Session()
        self.session.verify = verify_ssl
        # Pre-set the language cookie that the switch UI expects
        self.session.cookies.set("LOCAL_LANG_COOKIE", "zh")
        self.session.cookies.set("UI_LOCAL_COOKIE",   "zh")

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """
        POST credentials to ``/login.do``.

        Returns ``True`` when the server sets ``login=1`` in the response
        cookies, which indicates a successful authentication.
        """
        url = f"{self.base_url}/login.do"
        headers = {
            **self._BASE_HEADERS,
            "Origin":  self.base_url,
            "Referer": f"{self.base_url}/index.htm",
        }
        try:
            resp = self.session.post(
                url,
                headers=headers,
                data={"auth": self.auth_token},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.exceptions.ConnectionError:
            logger.debug("[%s] Connection refused or host unreachable", self.ip)
            return False
        except requests.exceptions.Timeout:
            logger.debug("[%s] Connection timed out", self.ip)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] Login error: %s", self.ip, exc)
            return False

        cookies = self.session.cookies.get_dict()
        # Primary success indicator: server sets login=1
        if cookies.get("login") == "1" and "SID" in cookies:
            logger.debug(
                "[%s] Login OK  SID=%s", self.ip, cookies["SID"]
            )
            # Inject the Web-CLI navigation cookies that the JS normally sets
            self.session.cookies.set("module",      "system")
            self.session.cookies.set("subModule",   "console")
            self.session.cookies.set("threeModule", "console_setting")
            return True

        # Fallback: some firmware versions only set SID without login=1
        if "SID" in cookies and cookies.get("SID", "deleted") != "deleted":
            logger.debug("[%s] Login OK (fallback)  SID=%s", self.ip, cookies["SID"])
            self.session.cookies.set("module",      "system")
            self.session.cookies.set("subModule",   "console")
            self.session.cookies.set("threeModule", "console_setting")
            return True

        logger.debug("[%s] Login failed. HTTP %d  cookies=%s", self.ip, resp.status_code, cookies)
        return False

    # ------------------------------------------------------------------
    # Web CLI
    # ------------------------------------------------------------------

    def web_cli(self, command: str) -> dict | None:
        """
        Execute *command* via the Web CLI (``/web_cli.do``).

        Returns a dict with keys ``mode_tip``, ``content``, ``return_code``
        extracted from the XML response, or ``None`` on any error.
        """
        url = f"{self.base_url}/web_cli.do"
        headers = {
            **self._BASE_HEADERS,
            "Origin":  self.base_url,
            "Referer": f"{self.base_url}/common/webcli.htm",
        }
        data = {
            "command":     command,
            "mode_url":    "exec",
            "mode_intidx":  "0",
            "mode_intidx1": "0",
            "mode_intidx2": "0",
            "mode_prompt":  "",
            "mode_stridx":  "",
        }
        try:
            resp = self.session.post(
                url,
                headers=headers,
                data=data,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[%s] web_cli error: %s", self.ip, exc)
            return None

        if resp.status_code != 200:
            logger.debug("[%s] web_cli HTTP %d", self.ip, resp.status_code)
            return None

        return self._parse_xml(resp.text)

    @staticmethod
    def _parse_xml(xml_text: str) -> dict | None:
        """
        Parse the ``<webcli-print>`` XML envelope.

        The important fields are:

        * ``<mode-tip>`` – contains ``<HOSTNAME>#`` (e.g. ``DXL-5007#``)
        * ``<content>``  – the command output (running-config text)
        * ``<return-code>`` – ``0`` means success
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.debug("XML parse error: %s", exc)
            return None

        def _text(tag: str) -> str:
            el = root.find(tag)
            return (el.text or "").strip() if el is not None else ""

        return {
            "mode_tip":   _text("mode-tip"),
            "content":    _text("content"),
            "return_code": _text("return-code"),
        }

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def _hostname_from_tip(self, mode_tip: str) -> str | None:
        """
        Extract the hostname from a ``<mode-tip>`` value.

        Examples
        --------
        ``DXL-5007#``  →  ``DXL-5007``
        ``Switch>``    →  ``Switch``
        """
        m = re.match(r"^(.+?)\s*[#>]\s*$", mode_tip)
        return m.group(1).strip() if m else None

    def get_running_config(self) -> tuple[str | None, str | None]:
        """
        Run ``show running-config`` and return ``(hostname, config_text)``.

        Both values can be ``None`` when the command fails.
        """
        data = self.web_cli("show running-config")
        if not data:
            return None, None

        hostname = self._hostname_from_tip(data.get("mode_tip", ""))

        # Fallback: parse 'hostname <name>' from the config body
        if not hostname:
            m = re.search(r"^hostname\s+(\S+)", data.get("content", ""), re.MULTILINE)
            hostname = m.group(1) if m else None

        content = data.get("content") or None
        return hostname, content

    # ------------------------------------------------------------------
    # Collect (main entry point)
    # ------------------------------------------------------------------

    def collect(self) -> dict:
        """
        Full collection workflow for this switch.

        Returns
        -------
        dict with keys:
            ip, hostname, config, success, error
        """
        result: dict = {
            "ip":       self.ip,
            "hostname": None,
            "config":   None,
            "success":  False,
            "error":    None,
        }

        if not self.login():
            result["error"] = "Login failed"
            return result

        hostname, config = self.get_running_config()

        if not hostname:
            result["error"] = "Could not determine hostname"
            return result

        if not config:
            result["error"] = "Running-config is empty"
            return result

        result["hostname"] = hostname
        result["config"]   = config
        result["success"]  = True
        return result


# ===========================================================================
# Config loading
# ===========================================================================

def load_config(path: str) -> dict:
    """Load and validate ``config.yml``."""
    p = Path(path)
    if not p.exists():
        logger.error("Config file not found: %s", path)
        sys.exit(1)

    with p.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    if not isinstance(cfg, dict):
        logger.error("config.yml is empty or invalid")
        sys.exit(1)

    auth = cfg.get("auth", {})
    has_token = bool(auth.get("token"))
    has_creds  = bool(auth.get("username")) and bool(auth.get("password"))

    if not has_token and not has_creds:
        logger.error(
            "config.yml must define either 'auth.token' or "
            "both 'auth.username' and 'auth.password'"
        )
        sys.exit(1)

    return cfg


def resolve_auth_token(auth: dict) -> str:
    """Return the auth token, generating it from credentials when needed."""
    if auth.get("token"):
        return auth["token"]
    username = auth["username"]
    password = auth["password"]
    rounds   = int(auth.get("encoding_rounds", 9))
    token    = generate_auth_token(username, password, rounds)
    logger.debug("Generated auth token for user '%s' (%d rounds)", username, rounds)
    return token


# ===========================================================================
# IP target expansion
# ===========================================================================

def expand_targets(targets: dict) -> list[str]:
    """
    Convert the *targets* section of config.yml into a sorted list of IPs.

    Supported formats
    -----------------
    * ``ips``     – explicit list of IP strings
    * ``subnets`` – CIDR notation (e.g. ``192.168.1.0/24``)
    * ``ranges``  – dash notation  (e.g. ``192.168.1.1-254``)
    """
    ips: set[str] = set()

    for raw in targets.get("ips", []):
        try:
            ipaddress.ip_address(raw)
            ips.add(raw)
        except ValueError:
            logger.warning("Skipping invalid IP: %s", raw)

    for cidr in targets.get("subnets", []):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            hosts = list(net.hosts())
            for h in hosts:
                ips.add(str(h))
            logger.info("Subnet %s → %d host addresses", cidr, len(hosts))
        except ValueError:
            logger.warning("Skipping invalid subnet: %s", cidr)

    for rng in targets.get("ranges", []):
        # Expected: "A.B.C.START-END"  e.g. "10.0.0.1-254"
        m = re.match(r"^(\d+\.\d+\.\d+)\.(\d+)-(\d+)$", rng.strip())
        if not m:
            logger.warning("Skipping invalid range: %s", rng)
            continue
        prefix, start, end = m.group(1), int(m.group(2)), int(m.group(3))
        for i in range(start, end + 1):
            ips.add(f"{prefix}.{i}")
        logger.info("Range %s → %d addresses", rng, end - start + 1)

    return sorted(ips, key=lambda x: ipaddress.ip_address(x))


# ===========================================================================
# Output helpers
# ===========================================================================

def save_results(
    hostname: str,
    ip: str,
    config: str,
    output_dir: Path,
) -> tuple[Path, Path]:
    """
    Persist a hostname↔IP mapping JSON and the running-config text.

    Files created
    -------------
    * ``<output_dir>/<Hostname>_<IP>.json``
    * ``<output_dir>/configs/<Hostname>_<IP>.text``
    """
    # Sanitize the hostname so it is safe as a filename
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", hostname)
    base      = f"{safe_name}_{ip}"

    # ---- JSON mapping ----
    mapping = {
        "hostname":     hostname,
        "ip":           ip,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json_path = output_dir / f"{base}.json"
    json_path.write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("[%s] Mapping saved → %s", ip, json_path)

    # ---- Running-config text ----
    configs_dir = output_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    config_path = configs_dir / f"{base}.text"
    config_path.write_text(config, encoding="utf-8")
    logger.info("[%s] Config saved  → %s", ip, config_path)

    return json_path, config_path


# ===========================================================================
# Per-switch worker (runs in thread pool)
# ===========================================================================

def process_switch(
    ip: str,
    auth_token: str,
    request_cfg: dict,
    output_dir: Path,
) -> dict:
    """Log into *ip*, collect data, save files, return result dict."""
    timeout    = int(request_cfg.get("timeout",    10))
    verify_ssl = bool(request_cfg.get("verify_ssl", False))

    switch = RuijieSwitch(ip, auth_token, timeout=timeout, verify_ssl=verify_ssl)
    result = switch.collect()

    if result["success"]:
        json_path, config_path = save_results(
            result["hostname"], ip, result["config"], output_dir
        )
        result["json_path"]   = str(json_path)
        result["config_path"] = str(config_path)
        logger.info("[%s] ✓  Hostname: %s", ip, result["hostname"])
    else:
        logger.warning("[%s] ✗  %s", ip, result["error"])

    return result


# ===========================================================================
# CLI entry-point
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ruijie-collector",
        description=(
            "Collect running-configs from Ruijie switches via the web management API."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # use config.yml in current directory
  python main.py -c /etc/collector.yml    # custom config path
  python main.py -o /tmp/backups          # custom output directory
  python main.py --dry-run                # list targets without connecting
  python main.py -v                       # verbose / debug output
        """,
    )
    p.add_argument(
        "-c", "--config",
        default="config.yml",
        metavar="FILE",
        help="Path to YAML configuration file (default: config.yml)",
    )
    p.add_argument(
        "-o", "--output",
        default=".",
        metavar="DIR",
        help="Output directory for JSON and config files (default: current dir)",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print target IPs and exit without connecting",
    )
    p.add_argument(
        "--decode-token",
        metavar="TOKEN",
        help="Decode a Ruijie auth token and print credentials",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ---- Utility: decode an existing token ----
    if args.decode_token:
        creds = decode_auth_token(args.decode_token)
        if creds:
            print(f"Username : {creds[0]}")
            print(f"Password : {creds[1]}")
        else:
            print("Could not decode token (unknown encoding or too many rounds).")
        return

    # ---- Normal collection run ----
    cfg         = load_config(args.config)
    auth_token  = resolve_auth_token(cfg.get("auth", {}))
    targets_cfg = cfg.get("targets", {})
    request_cfg = cfg.get("request", {})

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    ips = expand_targets(targets_cfg)
    if not ips:
        logger.error("No valid target IPs found. Check the 'targets' section in config.yml.")
        sys.exit(1)

    logger.info("Targets: %d IP(s)", len(ips))

    if args.dry_run:
        print(f"\nDry-run — {len(ips)} target(s):")
        for ip in ips:
            print(f"  {ip}")
        print()
        return

    # ---- Concurrent collection ----
    max_workers = int(request_cfg.get("concurrent", 5))
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(process_switch, ip, auth_token, request_cfg, output_dir): ip
            for ip in ips
        }
        for future in as_completed(future_map):
            ip = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] Unhandled exception: %s", ip, exc)
                results.append({"ip": ip, "success": False, "error": str(exc)})

    # ---- Summary ----
    ok   = [r for r in results if r["success"]]
    fail = [r for r in results if not r["success"]]

    bar = "=" * 56
    print(f"\n{bar}")
    print("  Collection Summary")
    print(bar)
    print(f"  Total     : {len(results)}")
    print(f"  Success   : {len(ok)}")
    print(f"  Failed    : {len(fail)}")

    if ok:
        print(f"\n  {'IP Address':<22}  Hostname")
        print(f"  {'-'*22}  {'-'*20}")
        for r in sorted(ok, key=lambda x: ipaddress.ip_address(x["ip"])):
            print(f"  {r['ip']:<22}  {r['hostname']}")

    if fail:
        print(f"\n  {'IP Address':<22}  Error")
        print(f"  {'-'*22}  {'-'*30}")
        for r in sorted(fail, key=lambda x: x["ip"]):
            print(f"  {r['ip']:<22}  {r.get('error', 'unknown')}")

    print(f"{bar}\n")


if __name__ == "__main__":
    main()
