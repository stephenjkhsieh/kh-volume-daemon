#!/usr/bin/env -S micromamba run -n khtool python

import sys
import os
import subprocess
import re
import yaml
import atexit
import signal
import logging
import argparse
import threading
from pathlib import Path
from pid import PidFile
from typing import List, Dict, Any, Optional, Set, Tuple

# ==============================================================================
# 1. 初始化設定
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# ==============================================================================
# 2. BaseDaemon Class (共享邏輯)
# ==============================================================================
class BaseDaemon:
    """
    所有 Daemon 的基礎類別，包含共享的輔助函式與基礎生命週期管理。
    """
    def __init__(self, config: Dict[str, Any], name: str):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.logger = logging.getLogger(name) # 為每個 daemon 建立獨立的 logger

        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        self.logger.info("Signal received, initiating shutdown.")
        sys.exit(0)

    def _run_command(self, cmd: List[str], **kwargs) -> Optional[str]:
        self.logger.debug(f"Running command: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, **kwargs)
            output = result.stdout.strip()
            self.logger.debug(f"Command output: \n{output}")
            return output
        except FileNotFoundError:
            self.logger.error(f"Command not found: {cmd[0]}")
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            if e.returncode != 0:
                self.logger.debug(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr.strip()}")
            return e.stdout.strip()
        return None

    def _get_node_info_from_pactl_name(self, pactl_name: str) -> Optional[Dict[str, str]]:
        output = self._run_command(["pw-cli", "list-objects", "Node"])
        if not output: return None
        objects = re.split(r'(\n?\s*id \d+, type )', output)
        for i in range(1, len(objects), 2):
            header, body = objects[i], objects[i+1]
            if 'media.class = "Audio/Sink"' in body and pactl_name in body:
                id_match = re.search(r'id (\d+)', header)
                name_match = re.search(r'node.name = "([^"]+)"', body)
                serial_match = re.search(r'object.serial = "([^"]+)"', body)
                if id_match and name_match and serial_match:
                    return {"id": id_match.group(1), "name": name_match.group(1), "serial": serial_match.group(1)}
        self.logger.warning(f"Could not find node info for pactl sink name: {pactl_name}")
        return None

    def cleanup(self):
        self.logger.info("Running base cleanup...")
        if self.process and self.process.poll() is None:
            self.logger.info(f"Terminating subprocess (PID: {self.process.pid})...")
            # 使用 os.killpg 來確保整個程序組被殺死
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=2)
            except (ProcessLookupError, PermissionError):
                pass # 程序可能已經自己結束了
            except subprocess.TimeoutExpired:
                 os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)

# ==============================================================================
# 3. VolumeDaemon Class (音量控制)
# ==============================================================================
class VolumeDaemon(BaseDaemon):
    """
    負責建立虛擬 Sink 並監聽其音量/靜音變化，以控制實體音響。
    """
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config, "VolumeDaemon")
        self.sink_name: str = self.config["sink_name"]
        self.created_sink_module_id: Optional[str] = None
        self.last_state: Dict[str, Any] = {"vol": 0, "mute": False}
        self.sink_index: Optional[str] = None
        self.sink_node_info: Optional[Dict[str, str]] = None

    def setup(self) -> Optional[Dict[str, str]]:
        self.logger.info("Performing setup...")
        sinks_output = self._run_command(["pactl", "list", "short", "sinks"])
        if sinks_output is None or self.sink_name not in sinks_output:
            self.logger.info(f"Sink '{self.sink_name}' not found, creating it...")
            module_id_str = self._run_command(["pactl", "load-module", "module-null-sink", f"sink_name={self.sink_name}", f"sink_properties=device.description='{self.config['sink_desc']}'"])
            if not module_id_str or not module_id_str.isdigit():
                self.logger.error("Failed to load module-null-sink")
                return None
            self.created_sink_module_id = module_id_str
            self.logger.info(f"Sink '{self.sink_name}' created with module ID {self.created_sink_module_id}")
        else:
            self.logger.info(f"Found existing sink '{self.sink_name}'")

        self.sink_node_info = self._get_node_info_from_pactl_name(self.sink_name)
        if not self.sink_node_info:
            self.logger.error(f"Could not find node info for virtual sink '{self.sink_name}'")
            return None
        self.sink_index = self.sink_node_info['serial']
        self.logger.info(f"Virtual sink: '{self.sink_node_info['name']}' (ID: {self.sink_node_info['id']}, Index: {self.sink_index})")

        self.logger.info("Initializing sink state and syncing speaker...")
        self._run_command(["pactl", "set-sink-volume", self.sink_index, f"{self.config['init_vol_pct']}%"])
        self._run_command(["pactl", "set-sink-mute", self.sink_index, "1" if self.config["init_mute"] else "0"])
        self.last_state = {"vol": self.config["init_vol_pct"], "mute": self.config["init_mute"]}
        self.logger.info(f"Speaker initialized to {self.last_state['vol']}% volume, Mute: {self.last_state['mute']}")
        if self.last_state["mute"]: self._apply_mute()
        else: self._apply_level_unmute(self._pct_to_db(self.last_state["vol"]))

        return self.sink_node_info

    def run(self):
        self.logger.info("Starting listener thread.")
        try:
            self.process = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True, bufsize=1, preexec_fn=os.setsid)
            for line in self.process.stdout:
                self.logger.debug(f"Event received: {line.strip()}")
                if "'change' on sink #" not in line: continue
                match = re.search(r'#(\d+)', line)
                if not match or match.group(1) != self.sink_index: continue

                current_vol = self._get_vol_pct()
                current_mute = self._get_mute()
                if current_vol is None or current_mute is None: continue

                vol_changed = current_vol != self.last_state["vol"]
                mute_changed = current_mute != self.last_state["mute"]

                if mute_changed:
                    self.logger.info(f"Mute changed from {self.last_state['mute']} to {current_mute}")
                    if current_mute: self._apply_mute()
                    else: self._apply_level_unmute(self._pct_to_db(current_vol))
                    self.last_state["mute"] = current_mute
                    self.last_state["vol"] = current_vol
                elif not current_mute and vol_changed:
                    self.logger.info(f"Volume changed from {self.last_state['vol']}% to {current_vol}%")
                    self._apply_level_unmute(self._pct_to_db(current_vol))
                    self.last_state["vol"] = current_vol
        except Exception as e:
            self.logger.error(f"Error in listener thread: {e}", exc_info=True)
        self.logger.info("Listener thread finished.")

    def cleanup(self):
        super().cleanup()
        if self.created_sink_module_id:
            self.logger.info(f"Unloading created sink module: {self.created_sink_module_id}")
            self._run_command(["pactl", "unload-module", self.created_sink_module_id])

    # --- Volume-specific helpers ---
    def _get_vol_pct(self) -> Optional[int]:
        output = self._run_command(["pactl", "get-sink-volume", self.sink_name])
        if output is None: return None
        match = re.search(r'(\d+)%', output)
        return int(match.group(1)) if match else None

    def _get_mute(self) -> Optional[bool]:
        output = self._run_command(["pactl", "get-sink-mute", self.sink_name])
        return "yes" in output.lower() if output else None

    def _pct_to_db(self, pct: int) -> float:
        pct = max(0, pct)
        return max(0, min(self.config["db_min"] + (pct * (self.config["db_max"] - self.config["db_min"]) / 100), 120))

    def _run_khtool(self, args: List[str]):
        cmd = [sys.executable, self.config["khtool_py"], *args]
        self._run_command(cmd, cwd=self.config["khtool_workdir"])

    def _apply_mute(self):
        self.logger.info("Applying mute...")
        self._run_khtool(["-i", self.config["iface"], "--mute"])

    def _apply_level_unmute(self, db: float):
        self.logger.info(f"Applying level {db:.1f} dB and unmuting...")
        self._run_khtool(["-i", self.config["iface"], "--level", f"{db:.1f}", "--unmute"])

# ==============================================================================
# 4. RedirectDaemon Class (音訊轉接)
# ==============================================================================
class RedirectDaemon(BaseDaemon):
    """
    負責監聽連接到虛擬 Sink 的音訊流，並將其複製一份到真實的硬體 Sink。
    """
    def __init__(self, config: Dict[str, Any], vsink_node_info: Dict[str, str]):
        super().__init__(config, "RedirectDaemon")
        self.vsink_node_info = vsink_node_info
        self.real_sink_name = self.config["real_sink_name"]
        self.real_sink_node_info: Optional[Dict[str, str]] = None
        self.links_created: Set[Tuple[str, str]] = set()

    def setup(self) -> bool:
        self.logger.info("Performing setup...")
        if not self.real_sink_name:
            self.logger.error("'real_sink_name' is not defined in config. Cannot start.")
            return False
        self.real_sink_node_info = self._get_node_info_from_pactl_name(self.real_sink_name)
        if not self.real_sink_node_info:
            self.logger.error(f"Could not find node info for real sink '{self.real_sink_name}'")
            return False
        self.logger.info(f"Real sink: '{self.real_sink_node_info['name']}' (ID: {self.real_sink_node_info['id']})")
        return True

    def run(self):
        self.logger.info("Starting listener thread.")

        # 1.【修正】使用正確的指令：--links 和 --monitor
        cmd = ["pw-link", "--links", "--monitor"]

        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1, preexec_fn=os.setsid)
            for line in self.process.stdout:
                self.logger.debug(f"Event received: {line.strip()}")

                # 2.【精準匹配】只處理 Link 建立的事件，必須以 '+' 開頭且包含 '->'
                if not (line.startswith('+ ') or line.startswith('- ')) or '->' not in line:
                    continue

                # 3.【解析】解析來源和目標
                #    範例: '+ Firefox:output_FL -> kh150_sink:playback_FL'
                match = re.search(r'([+\-]) (.*?):.*? -> (.*?):.*', line)
                if not match:
                    continue

                event_type, source_node_name, dest_node_name = match.groups()

                # 4.【判斷】檢查目標是否為我們的 vsink，且來源是新的應用程式
                # if dest_node_name == self.vsink_node_info['name'] and source_node_name not in redirected_node_names:
                if dest_node_name == self.vsink_node_info['name']:
                    if event_type == '+':
                        self.logger.info(f"New stream from '{source_node_name}' linked to virtual sink.")

                        self.logger.info(f"Redirecting '{source_node_name}' to real sink {self.real_sink_name}...")

                        # 5.【執行】建立從 App 到 Real Sink 的連結
                        self._run_command(["pw-link", source_node_name, self.real_sink_node_info['name']])
                        self.links_created.add((source_node_name, self.real_sink_node_info['name']))

                        # # 6.【記錄】將此 App 加入已處理清單，避免重複處理其 stereo/surround port
                        # redirected_node_names.add(source_node_name)
                    elif event_type == '-':
                        self.logger.info(f"Stream from '{source_node_name}' unlinked from virtual sink.")

                        self.logger.info(f"Unlinking '{source_node_name}' from real sink {self.real_sink_name}...")

                        self._run_command(["pw-link", "-d", source_node_name, self.real_sink_node_info['name']])
                        self.links_created.discard((source_node_name, self.real_sink_node_info['name']))

        except Exception as e:
            self.logger.error(f"Error in listener thread: {e}", exc_info=True)
        self.logger.info("Listener thread finished.")

    def cleanup(self):
        super().cleanup()
        if self.links_created:
            self.logger.info("Unlinking all manually created links...")
            for app_node_id, realsink_node_id in self.links_created:
                self.logger.info(f"Unlinking: {app_node_id} -> {realsink_node_id}")
                self._run_command(["pw-link", "-d", app_node_id, realsink_node_id])


# ==============================================================================
# 5. 主進入點 (協調者)
# ==============================================================================
def load_config_from_file(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        logging.warning(f"Config file not found at {config_path}, using defaults.")
        return {}

    logging.info(f"Loading config from {config_path}")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return data.get("settings", {})
    except yaml.YAMLError as e:
        logging.error(f"Error parsing YAML config file {config_path}: {e}")
        return {}
    except Exception as e:
        logging.error(f"Error reading config file {config_path}: {e}")
        return {}

if __name__ == "__main__":
    DEFAULT_CONFIG = {
        "sink_name": "kh150_sink",
        "sink_desc": "KH150 Control",
        "enable_redirect": False,
        "real_sink_name": "", # 請務必在設定檔中填寫這個值
        "khtool_py": "~/Codespace/khtool/khtool.py",
        "khtool_workdir": "~/.local/state/kh-volume",
        "iface": "lo",
        "db_min": 40,
        "db_max": 90,
        "init_vol_pct": 20,
        "init_mute": False,
    }

    config_path = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "kh-volume/config.yaml"
    file_config = load_config_from_file(config_path)

    parser = argparse.ArgumentParser(description="A daemon to control Neumann speaker volume and redirect audio.")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug logging level.")
    parser.add_argument("--sink-name", type=str, help="Override the virtual sink's name (sink_name).")
    parser.add_argument("--sink-desc", type=str, help="Override the virtual sink's description (sink_desc).")
    parser.add_argument("--enable-redirect", type=str, choices=['yes', 'no'], help="Override the audio stream redirection feature (enable_redirect).")
    parser.add_argument("--real-sink-name", type=str, help="Override the name of the real hardware sink (real_sink_name).")
    parser.add_argument("--khtool-repo", type=str, help="Override the path to the khtool.py repository.")
    parser.add_argument("--khtool-workdir", type=str, help="Override the working directory path for khtool.py.")
    parser.add_argument("--iface", type=str, help="Override the speaker's network interface (iface).")
    parser.add_argument("--db-min", type=int, help="Override the dB value corresponding to 0% volume (db_min).")
    parser.add_argument("--db-max", type=int, help="Override the dB value corresponding to 100% volume (db_max).")
    parser.add_argument("--init-vol-pct", type=int, help="Override the initial volume percentage (init_vol_pct).")
    parser.add_argument("--init-mute", type=str, choices=['yes', 'no'], help="Override the initial mute state (init_mute).")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    final_config = DEFAULT_CONFIG.copy()
    final_config.update(file_config)
    cli_args_dict = {k: v for k, v in vars(args).items() if v is not None}
    final_config.update(cli_args_dict)

    final_config["enable_redirect"] = str(final_config.get("enable_redirect")).lower() in ['true', 'yes', '1']
    final_config["init_mute"] = str(final_config.get("init_mute")).lower() in ['true', 'yes', '1']
    final_config["khtool_py"] = str(Path(final_config["khtool_py"]).expanduser())
    final_config["khtool_workdir"] = str(Path(final_config["khtool_workdir"]).expanduser())
    Path(final_config["khtool_workdir"]).mkdir(parents=True, exist_ok=True)

    print(final_config["iface"])

    try:
        pid_dir = Path.home() / ".local/state/kh-volume"
        pid_dir.mkdir(parents=True, exist_ok=True)
        with PidFile("kh-volume-daemon", piddir=pid_dir):
            main_logger = logging.getLogger("Main")
            main_logger.info("Starting daemon...")

            volume_daemon = VolumeDaemon(final_config)
            vsink_info = volume_daemon.setup()

            if not vsink_info:
                main_logger.error("Daemon setup failed. Exiting.")
                sys.exit(1)

            threads = []
            volume_thread = threading.Thread(target=volume_daemon.run, daemon=True)
            threads.append(volume_thread)

            if final_config["enable_redirect"]:
                redirect_daemon = RedirectDaemon(final_config, vsink_info)
                if redirect_daemon.setup():
                    redirect_thread = threading.Thread(target=redirect_daemon.run, daemon=True)
                    threads.append(redirect_thread)
                else:
                    main_logger.warning("Redirect daemon setup failed. Will run without redirection.")

            main_logger.info(f"Starting {len(threads)} listener thread(s)...")
            for t in threads:
                t.start()

            # 主執行緒等待所有背景執行緒結束
            for t in threads:
                t.join()

    except SystemExit:
        logging.info("Daemon is shutting down.")
    except Exception as e:
        logging.error(f"An unhandled error occurred in main: {e}", exc_info=True)
        sys.exit(1)