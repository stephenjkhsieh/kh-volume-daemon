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
from pathlib import Path
from pid import PidFile
from typing import List, Dict, Any, Optional, Set, Tuple

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class VolumeDaemon:
    def __init__(self, config: Dict[str, Any]):
        self.config: Dict[str, Any] = config
        self.created_sink_module_id: Optional[str] = None
        self.last_state: Dict[str, Any] = {"vol": 0, "mute": False}
        self.sink_name: str = self.config["sink_name"]
        self.sink_index: Optional[str] = None
        self.sink_node_info: Dict[str, str] = {}
        self.real_sink_name: str = self.config["real_sink_name"]
        self.real_sink_node_info: Dict[str, str] = {}
        self.links_created: Set[Tuple[str, str]] = set()
        self.redirected_sink_inputs: Set[str] = set()

    def _run_command(self, cmd: List[str], **kwargs) -> Optional[str]:
        """執行外部指令的輔助方法"""
        logging.debug(f"Running command: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, **kwargs)
            output = result.stdout.strip()
            logging.debug(f"Command output: \n{output}")
            return output
        except FileNotFoundError:
            logging.error(f"Command not found: {cmd[0]}")
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            if e.returncode != 0:
                logging.debug(f"Command failed: {' '.join(cmd)}\nStderr: {e.stderr.strip()}")
            return e.stdout.strip()
        return None

    def _get_node_info_from_pactl_name(self, pactl_name: str) -> Optional[Dict[str, str]]:
        """從 pactl 名稱獲取 PipeWire Node ID 和 Name"""
        output = self._run_command(["pw-cli", "list-objects", "Node"])
        if not output: return None

        objects = re.split(r'(\n?\s*id \d+, type )', output)
        for i in range(1, len(objects), 2):
            header = objects[i]
            body = objects[i+1]

            if 'media.class = "Audio/Sink"' in body and pactl_name in body:
                id_match = re.search(r'id (\d+)', header)
                name_match = re.search(r'node.name = "([^"]+)"', body)
                if id_match and name_match:
                    return {"id": id_match.group(1), "name": name_match.group(1)}
        return None

    def _get_node_info_from_sink_input_block(self, block_content: str) -> Optional[Dict[str, str]]:
        """從 sink-input 的文字區塊中獲取 Node ID 和 Name"""
        id_match = re.search(r'object.id = "(\d+)"', block_content)
        name_match = re.search(r'node.name = "([^"]+)"', block_content)
        if id_match and name_match:
            return {"id": id_match.group(1), "name": name_match.group(1)}
        return None

    def _get_sink_index(self) -> Optional[str]:
        """取得 sink 的數字 index"""
        output = self._run_command(["pactl", "list", "short", "sinks"])
        if output is None: return None
        for line in output.splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[1] == self.sink_name:
                return parts[0]
        return None

    def _get_vol_pct(self) -> Optional[int]:
        """使用 sink name 取得音量"""
        output = self._run_command(["pactl", "get-sink-volume", self.sink_name])
        if output is None: return None
        match = re.search(r'(\d+)%', output)
        return int(match.group(1)) if match else None

    def _get_mute(self) -> Optional[bool]:
        """使用 sink name 取得靜音狀態"""
        output = self._run_command(["pactl", "get-sink-mute", self.sink_name])
        return "yes" in output.lower() if output else None

    def _pct_to_db(self, pct: int) -> float:
        pct = max(0, pct); return max(0, min(self.config["db_min"] + (pct * (self.config["db_max"] - self.config["db_min"]) / 100), 120))

    def _run_khtool(self, args: List[str]):
        cmd = [sys.executable, self.config["khtool_py"], *args]; self._run_command(cmd, cwd=self.config["khtool_workdir"])

    def _apply_mute(self):
        logging.info("Applying mute..."); self._run_khtool(["-i", self.config["iface"], "--mute"])

    def _apply_level_unmute(self, db: float):
        logging.info(f"Applying level {db:.1f} dB and unmuting..."); self._run_khtool(["-i", self.config["iface"], "--level", f"{db:.1f}", "--unmute"])

    def cleanup(self):
        """卸載模組並拆除手動建立的鏈接"""
        logging.info("Caught signal or exiting, cleaning up...")
        if self.links_created:
            logging.info("Unlinking all manually created links...")
            for output_port, input_port in self.links_created:
                logging.info(f"Unlinking: {output_port} -> {input_port}")
                self._run_command(["pw-link", "-d", output_port, input_port])
        if self.created_sink_module_id:
            logging.info(f"Unloading created sink module: {self.created_sink_module_id}")
            self._run_command(["pactl", "unload-module", self.created_sink_module_id])
        logging.info("Exiting.")

    def _reconcile_streams_state(self):
        """全面掃描並同步所有音訊流的狀態 (狀態同步引擎)"""
        if not self.config["enable_redirect"]: return

        logging.info("Reconciling streams state...")
        if not self.sink_index: return

        sink_inputs_output = self._run_command(["pactl", "list", "sink-inputs"])
        if not sink_inputs_output: return

        # 使用 finditer 來遍歷所有 sink-input 區塊
        regex = re.compile(r"Sink Input #(\d+)(.*?)(?=Sink Input #|\Z)", re.DOTALL)
        for match in regex.finditer(sink_inputs_output):
            sink_input_index = match.group(1)
            block_content = match.group(2)

            # 如果這個流已經轉接過，就跳過
            if sink_input_index in self.redirected_sink_inputs: continue

            # 檢查這個流是否在我們的虛擬 sink 上
            sink_index_match = re.search(r'Sink: (\d+)', block_content)
            if sink_index_match and sink_index_match.group(1) == self.sink_index:
                app_node_info = self._get_node_info_from_sink_input_block(block_content)
                if app_node_info:
                    logging.info(f"Redirecting stream from '{app_node_info['name']}' (ID: {app_node_info['id']})...")
                    app_node_id = app_node_info['id']
                    vsink_node_id = self.sink_node_info['id']
                    realsink_node_id = self.real_sink_node_info['id']

                    # self._run_command(["pw-link", "-d", app_node_id, vsink_node_id])
                    self._run_command(["pw-link", app_node_id, realsink_node_id])
                    self.links_created.add((app_node_id, realsink_node_id))


                    # app_node_name = app_node_info['name']
                    # vsink_node_name = self.sink_node_info['name']
                    # realsink_node_name = self.real_sink_node_info['name']

                    # app_ports = [f"{app_node_name}:output_FL", f"{app_node_name}:output_FR"]
                    # vsink_ports = [f"{vsink_node_name}:playback_FL", f"{vsink_node_name}:playback_FR"]
                    # realsink_ports = [f"{realsink_node_name}:send_CH1", f"{realsink_node_name}:send_CH2"]
                    # for i in range(2):
                        # self._run_command(["pw-link", "-d", app_ports[i], vsink_ports[i]])
                        # self._run_command(["pw-link", app_ports[i], realsink_ports[i]])
                        # self.links_created.add((app_ports[i], realsink_ports[i]))


                    self.redirected_sink_inputs.add(sink_input_index)

    def run(self):
        """主執行函式"""
        # ---- 建立/取得虛擬 sink ----
        sinks_output = self._run_command(["pactl", "list", "short", "sinks"])
        if sinks_output is None or self.sink_name not in sinks_output:
            logging.info(f"Sink '{self.sink_name}' not found, creating it...")
            module_id_str = self._run_command(["pactl", "load-module", "module-null-sink", f"sink_name={self.sink_name}", f"sink_properties=device.description='{self.config['sink_desc']}'"])
            if not module_id_str or not module_id_str.isdigit():
                logging.error("Failed to load module-null-sink"); sys.exit(1)
            self.created_sink_module_id = module_id_str
            logging.info(f"Sink '{self.sink_name}' created with module ID {self.created_sink_module_id}")
        else:
            logging.info(f"Found existing sink '{self.sink_name}'")

        # ---- 取得 Node Info (ID 和 Name) ----
        self.sink_node_info = self._get_node_info_from_pactl_name(self.sink_name)
        if not self.sink_node_info:
            logging.error(f"Could not find node info for virtual sink '{self.sink_name}'"); sys.exit(1)
        if self.config["enable_redirect"]:
            self.real_sink_node_info = self._get_node_info_from_pactl_name(self.real_sink_name)
            if not self.real_sink_node_info:
                logging.error(f"Could not find node info for real sink '{self.real_sink_name}'"); sys.exit(1)

        logging.info(f"Virtual sink: '{self.sink_node_info['name']}' (ID: {self.sink_node_info['id']})")
        if self.real_sink_node_info:
            logging.info(f"Real sink: '{self.real_sink_node_info['name']}' (ID: {self.real_sink_node_info['id']})")

        # ---- 設定虛擬 sink 初始狀態 & 初始同步 ----
        self.sink_index = self._get_sink_index()
        if not self.sink_index:
            logging.error("Could not get sink index after creation."); sys.exit(1)
        self._run_command(["pactl", "set-sink-volume", self.sink_index, f"{self.config['init_vol_pct']}%"])
        mute_flag = "1" if self.config["init_mute"] else "0"
        self._run_command(["pactl", "set-sink-mute", self.sink_index, mute_flag])

        self.last_state["vol"] = self.config["init_vol_pct"]
        self.last_state["mute"] = self.config["init_mute"]
        logging.info(f"Initializing speaker to {self.last_state['vol']}% volume, Mute: {self.last_state['mute']}")
        if self.last_state["mute"]: self._apply_mute()
        else: self._apply_level_unmute(self._pct_to_db(self.last_state["vol"]))

        # 在進入主迴圈前，執行一次全面的狀態同步
        self._reconcile_streams_state()

        # ---- 事件監聽 ----
        logging.info("Initialization complete. Listening for events...")
        process = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, text=True, bufsize=1)

        for line in process.stdout:
            logging.debug(f"Event received: {line.strip()}")

            # 將 'new' 和 'server change' 事件統一導向狀態同步引擎
            if self.config["enable_redirect"] and ("'new' on sink-input" in line or "'change' on server" in line):# or "'change' on sink-input" in line):
                logging.info(f"Routing event detected ('{line.strip()}'), reconciling streams state...")
                # 稍微延遲，確保 PipeWire 已更新其內部狀態
                # import time; time.sleep(0.05)
                self._reconcile_streams_state()

            elif "'change' on sink #" in line:
                match = re.search(r'#(\d+)', line)
                if not match: continue

                event_sink_index = match.group(1)

                if event_sink_index == self.sink_index:
                    current_vol = self._get_vol_pct()
                    current_mute = self._get_mute()
                    if current_vol is None or current_mute is None: continue

                    vol_changed = current_vol != self.last_state["vol"]
                    mute_changed = current_mute != self.last_state["mute"]

                    if mute_changed:
                        logging.info(f"Mute changed from {self.last_state['mute']} to {current_mute}")
                        if current_mute: self._apply_mute()
                        else: self._apply_level_unmute(self._pct_to_db(current_vol))
                        self.last_state["mute"] = current_mute
                        self.last_state["vol"] = current_vol
                    elif not current_mute and vol_changed:
                        logging.info(f"Volume changed from {self.last_state['vol']}% to {current_vol}%")
                        self._apply_level_unmute(self._pct_to_db(current_vol))
                        self.last_state["vol"] = current_vol

            elif "'remove' on sink-input #" in line:
                match = re.search(r'#(\d+)', line)
                if match:
                    sink_input_index = match.group(1)
                    if sink_input_index in self.redirected_sink_inputs:
                        logging.info(f"Stream #{sink_input_index} removed. Clearing state.")
                        self.redirected_sink_inputs.remove(sink_input_index)

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
    # 1. 定義程式碼中的預設設定
    DEFAULT_CONFIG = {
        "sink_name": "kh150_sink",
        "sink_desc": "KH150\ Control",
        "enable_redirect": False,
        "real_sink_name": "",
        "khtool_repo": "~/Codespace/khtool/khtool.py",
        "khtool_workdir": "~/.local/state/kh-volume",
        "iface": "lo",
        "db_min": 40,
        "db_max": 90,
        "init_vol_pct": 20,
        "init_mute": False,
    }

    # 2. 讀取設定檔
    # config_path = Path(__file__).parent / "config.yaml"
    config_path = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "kh-volume/config.yaml"
    file_config = load_config_from_file(config_path)

    # 3. 解析命令列參數
    parser = argparse.ArgumentParser(description="A daemon to control Neumann speaker volume via a virtual sink.")
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
        logging.debug("Debug mode enabled.")

    # 4. 組合最終設定 (優先級：命令列 > 設定檔 > 預設值)
    final_config = DEFAULT_CONFIG.copy()
    final_config.update(file_config)
    cli_args_dict = {k: v for k, v in vars(args).items() if v is not None}
    final_config.update(cli_args_dict)

    # 5. [簡化] 對組合後的設定進行最終處理
    # 大部分的類型轉換已由 PyYAML 自動完成！我們只需要處理特殊項目。
    # (確保從 .ini 轉移過來的 yes/no 字串也能被 CLI 覆寫的布林值正確處理)
    final_config["init_mute"] = str(final_config["init_mute"]).lower() in ['true', 'yes', '1']
    final_config["enable_redirect"] = str(final_config["enable_redirect"]).lower() in ['true', 'yes', '1']

    # 處理路徑擴展
    final_config["khtool_py"] = str(Path(final_config["khtool_repo"]).expanduser())
    final_config["khtool_workdir"] = str(Path(final_config["khtool_workdir"]).expanduser())
    Path(final_config["khtool_workdir"]).mkdir(parents=True, exist_ok=True)

    # 6. 執行主程式
    try:
        pid_dir = Path.home() / ".local/state/kh-volume"
        pid_dir.mkdir(parents=True, exist_ok=True)
        with PidFile("kh-volume-daemon", piddir=pid_dir):
            daemon = VolumeDaemon(final_config)
            atexit.register(daemon.cleanup)
            def signal_handler(signum, frame): sys.exit(0)
            signal.signal(signal.SIGTERM, signal_handler)
            signal.signal(signal.SIGINT, signal_handler)
            daemon.run()
    except (SystemExit, KeyboardInterrupt):
        logging.info("Script interrupted by user.")
    except Exception as e:
        logging.error(f"An error occurred: {e}", exc_info=True)
        sys.exit(1)