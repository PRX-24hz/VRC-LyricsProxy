import asyncio
import os
import sys
import json
import re
import time
import threading
import ctypes
import requests
from pythonosc import osc_server, dispatcher, udp_client
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus
)
import pystray
from PIL import Image, ImageDraw
import customtkinter as ctk

# ----------------- 单实例互斥体检测 -----------------
def check_single_instance(app_mutex_name="Global\\VRC_LyricsProxy_Mutex"):
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, app_mutex_name)
    last_error = ctypes.windll.kernel32.GetLastError()
    if last_error == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            0,
            "【VRC-LyricsProxy】已经在后台运行中了！\n请检查右下角任务栏托盘图标，无需重复启动。",
            "提示",
            0x40 | 0x0
        )
        sys.exit(0)
    return mutex

# ----------------- 配置文件管理 -----------------
CONFIG_FILE = "config.json"
ICON_FILE = "logo.ico"

DEFAULT_CONFIG = {
    "listen_ip": "127.0.0.1",
    "listen_port": 9005,
    "vrc_ip": "127.0.0.1",
    "vrc_port": 9000,
    "lyric_mode": "smart",      # smart / orig / trans / dual
    "status_timeout": 15.0      # 提升至 15 秒，避免 MCB 刷新间隔导致文本忽隐忽现
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    except Exception:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"[配置保存失败] {e}")

config = load_config()

# ----------------- 全局运行时变量 -----------------
ALLOWED_APP_IDS = ["cloudmusic", "netease"]
MAX_CHATBOX_LEN = 144

client = udp_client.SimpleUDPClient(config["vrc_ip"], int(config["vrc_port"]))
server_instance = None

lock = threading.Lock()
latest_status_text = ""
last_status_time = 0.0
current_lyric_line = ""
is_running = True

# ----------------- 字符截断与合并 -----------------
def build_combined_chatbox_message(status_text: str, lyric_text: str) -> str:
    status_text = status_text.strip() if status_text else ""
    lyric_text = lyric_text.strip() if lyric_text else ""

    if not status_text and lyric_text:
        return lyric_text[: MAX_CHATBOX_LEN - 3] + "..." if len(lyric_text) > MAX_CHATBOX_LEN else lyric_text

    if status_text and not lyric_text:
        return status_text[: MAX_CHATBOX_LEN - 3] + "..." if len(status_text) > MAX_CHATBOX_LEN else status_text

    if not status_text and not lyric_text:
        return ""

    raw_combined = f"{status_text}\n{lyric_text}"
    if len(raw_combined) <= MAX_CHATBOX_LEN:
        return raw_combined

    # 预算配比：给状态信息最多留 60 字符，剩余留给歌词
    max_status_budget = 60
    status_safe = status_text[: max_status_budget - 3] + "..." if len(status_text) > max_status_budget else status_text
    remaining_budget = MAX_CHATBOX_LEN - len(status_safe) - 1

    if remaining_budget > 6:
        lyric_safe = lyric_text[: remaining_budget - 3] + "..."
        return f"{status_safe}\n{lyric_safe}"
    else:
        return status_safe[:MAX_CHATBOX_LEN]

# ----------------- 歌词解析模块 -----------------
def parse_lrc_content(text):
    result = {}
    if not text:
        return result
    for line in text.splitlines():
        line = line.strip()
        if re.match(r'^\[(ar|ti|al|by|offset|length):', line, re.I):
            continue
        if "纯音乐" in line and "欣赏" in line:
            continue

        matches = re.findall(r'\[(\d{1,2}):(\d{1,2}(?:\.\d+)?)\]', line)
        if matches:
            content = re.sub(r'\[\d{1,2}:\d{1,2}(?:\.\d+)?\]', '', line).strip()
            for m, s in matches:
                try:
                    sec = int(m) * 60 + float(s)
                    result[sec] = content
                except ValueError:
                    continue
    return result

def is_chinese_dominant(text):
    if not text:
        return False
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    return (chinese_chars / max(1, len(text))) > 0.3

class LyricManager:
    def __init__(self):
        self.original_lrc = {}
        self.trans_lrc = {}
        self.is_foreign = False

    def fetch_lyrics(self, title, artist):
        self.original_lrc.clear()
        self.trans_lrc.clear()
        self.is_foreign = False

        clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
        query = f"{clean_title} {artist}".strip()

        try:
            res = requests.get(
                f"https://music.163.com/api/search/get/web?s={query}&type=1&limit=1",
                timeout=4
            ).json()
            songs = res.get("result", {}).get("songs", [])
            if not songs:
                return
            song_id = songs[0]["id"]

            lrc_res = requests.get(
                f"https://music.163.com/api/song/lyric?os=pc&id={song_id}&lv=-1&kv=-1&tv=-1",
                timeout=4
            ).json()

            raw_orig = lrc_res.get("lrc", {}).get("lyric", "")
            raw_trans = lrc_res.get("tlyric", {}).get("lyric", "")

            self.original_lrc = parse_lrc_content(raw_orig)
            self.trans_lrc = parse_lrc_content(raw_trans)

            sample_text = "".join(list(self.original_lrc.values())[:5])
            self.is_foreign = not is_chinese_dominant(sample_text)
        except Exception:
            pass

    def get_current_line(self, current_time, mode="smart"):
        orig_line = ""
        trans_line = ""

        for t in sorted(self.original_lrc.keys()):
            if t <= current_time:
                orig_line = self.original_lrc[t]
            else:
                break

        for t in sorted(self.trans_lrc.keys()):
            if t <= current_time:
                trans_line = self.trans_lrc[t]
            else:
                break

        if mode == "orig":
            return f"♪ {orig_line}" if orig_line else ""
        elif mode == "trans":
            target = trans_line if trans_line else orig_line
            return f"♪ {target}" if target else ""
        elif mode == "dual":
            lines = []
            if orig_line: lines.append(orig_line)
            if trans_line and trans_line != orig_line: lines.append(trans_line)
            return "♪ " + "\n  ".join(lines) if lines else ""
        else:  # smart
            if self.is_foreign and trans_line:
                return f"♪ {trans_line}"
            return f"♪ {orig_line}" if orig_line else ""

    def has_lyrics(self):
        return bool(self.original_lrc)

    def get_max_time(self):
        return max(self.original_lrc.keys()) if self.original_lrc else 0

lyric_mgr = LyricManager()

# ----------------- OSC 监听与动态热更新 -----------------
def osc_handler(address, *args):
    global latest_status_text, last_status_time
    if args and is_running:
        text = str(args[0]).strip()
        # 仅接收非空内容，丢弃纯空刷新包，防止误清空状态
        if text:
            with lock:
                latest_status_text = text
                last_status_time = time.time()

def start_osc_server():
    global server_instance
    disp = dispatcher.Dispatcher()
    disp.map("/chatbox/input", osc_handler)
    try:
        server_instance = osc_server.ThreadingOSCUDPServer(
            (config["listen_ip"], int(config["listen_port"])), disp
        )
        server_instance.serve_forever()
    except Exception as e:
        print(f"[OSC 监听启动失败] 端口可能已被占用: {e}")

def update_client_target():
    global client
    client = udp_client.SimpleUDPClient(config["vrc_ip"], int(config["vrc_port"]))

# ----------------- 防闪烁推送核心逻辑 -----------------
def push_loop():
    last_sent = ""
    last_send_time = 0.0

    while True:
        if is_running:
            now = time.time()
            with lock:
                timeout = float(config.get("status_timeout", 15.0))
                if now - last_status_time > timeout:
                    status = ""
                else:
                    status = latest_status_text
                lyric = current_lyric_line

            combined = build_combined_chatbox_message(status, lyric)

            # 发送触发条件：
            # 1. 文本内容发生了变化（无论歌词翻页还是心率/时间更新）
            # 2. 或者内容虽未变化，但距离上次发送已过去 8.0 秒（低频心跳，避免 VRChat 气泡消失）
            need_push = False
            if combined != last_sent:
                need_push = True
            elif combined and (now - last_send_time >= 8.0):
                need_push = True

            if need_push and combined:
                # 仅传 [text, True]，避免携带第三参数导致打字状态反复重绘闪烁
                client.send_message("/chatbox/input", [combined, True])
                last_sent = combined
                last_send_time = now
            elif need_push and not combined and last_sent:
                # 状态和歌词均为空，清空 Chatbox
                client.send_message("/chatbox/input", ["", True])
                last_sent = ""
                last_send_time = now

        time.sleep(0.5)

# ----------------- 异步媒体监听与时间轴 -----------------
async def monitor_media():
    global current_lyric_line
    manager = await MediaManager.request_async()
    last_title = ""
    current_offset = 0.0
    song_duration = 0.0
    last_tick = time.time()

    while True:
        try:
            if is_running:
                target_session = None
                sessions = manager.get_sessions()
                for s in sessions:
                    app_id = (s.source_app_user_model_id or "").lower()
                    if any(kw in app_id for kw in ALLOWED_APP_IDS):
                        target_session = s
                        break

                if not target_session:
                    with lock:
                        current_lyric_line = ""
                    last_title = ""
                    await asyncio.sleep(0.5)
                    continue

                session = target_session
                now = time.time()
                dt = now - last_tick
                last_tick = now

                props = await session.try_get_media_properties_async()
                playback = session.get_playback_info()
                timeline = session.get_timeline_properties()
                is_playing = (playback.playback_status == PlaybackStatus.PLAYING) if playback else True

                if timeline and timeline.end_time:
                    song_duration = timeline.end_time.total_seconds()

                if props and props.title and props.title != last_title:
                    last_title = props.title
                    current_offset = 0.0
                    await asyncio.to_thread(lyric_mgr.fetch_lyrics, props.title, props.artist or "")

                max_time = lyric_mgr.get_max_time()
                if song_duration > 0 and current_offset >= song_duration:
                    current_offset = 0.0
                elif song_duration == 0 and max_time > 0 and current_offset > (max_time + 8.0):
                    current_offset = 0.0

                if is_playing:
                    current_offset += dt

                if is_playing and lyric_mgr.has_lyrics():
                    line = lyric_mgr.get_current_line(current_offset, mode=config.get("lyric_mode", "smart"))
                    with lock:
                        current_lyric_line = line
                elif not is_playing:
                    with lock:
                        current_lyric_line = ""
        except Exception:
            pass
        await asyncio.sleep(0.3)

def run_async_loop():
    asyncio.run(monitor_media())

# ----------------- GUI 设置弹窗界面 -----------------
settings_window = None

def open_settings_ui():
    global settings_window
    if settings_window is not None and settings_window.winfo_exists():
        settings_window.lift()
        settings_window.focus_force()
        return

    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

    settings_window = ctk.CTk()
    settings_window.title("VRChat 歌词代理设置")
    settings_window.geometry("420x460")
    settings_window.resizable(False, False)

    # 尝试加载窗口图标
    if os.path.exists(ICON_FILE):
        try:
            settings_window.iconbitmap(ICON_FILE)
        except Exception:
            pass

    title_label = ctk.CTkLabel(settings_window, text="代理参数配置", font=ctk.CTkFont(size=18, weight="bold"))
    title_label.pack(pady=(15, 10))

    frame = ctk.CTkFrame(settings_window)
    frame.pack(padx=20, pady=10, fill="both", expand=True)

    lbl1 = ctk.CTkLabel(frame, text="监听 IP (通常为 127.0.0.1):")
    lbl1.grid(row=0, column=0, padx=10, pady=5, sticky="w")
    entry_listen_ip = ctk.CTkEntry(frame, width=150)
    entry_listen_ip.insert(0, str(config["listen_ip"]))
    entry_listen_ip.grid(row=0, column=1, padx=10, pady=5)

    lbl2 = ctk.CTkLabel(frame, text="接收端口 (MagicChatbox):")
    lbl2.grid(row=1, column=0, padx=10, pady=5, sticky="w")
    entry_listen_port = ctk.CTkEntry(frame, width=150)
    entry_listen_port.insert(0, str(config["listen_port"]))
    entry_listen_port.grid(row=1, column=1, padx=10, pady=5)

    lbl3 = ctk.CTkLabel(frame, text="VRChat 目标 IP:")
    lbl3.grid(row=2, column=0, padx=10, pady=5, sticky="w")
    entry_vrc_ip = ctk.CTkEntry(frame, width=150)
    entry_vrc_ip.insert(0, str(config["vrc_ip"]))
    entry_vrc_ip.grid(row=2, column=1, padx=10, pady=5)

    lbl4 = ctk.CTkLabel(frame, text="VRChat 端口 (默认 9000):")
    lbl4.grid(row=3, column=0, padx=10, pady=5, sticky="w")
    entry_vrc_port = ctk.CTkEntry(frame, width=150)
    entry_vrc_port.insert(0, str(config["vrc_port"]))
    entry_vrc_port.grid(row=3, column=1, padx=10, pady=5)

    mode_map = {
        "智能模式 (外文优先翻译)": "smart",
        "纯原文模式": "orig",
        "纯翻译模式": "trans",
        "双语对照 (可能超长)": "dual"
    }
    rev_mode_map = {v: k for k, v in mode_map.items()}

    lbl5 = ctk.CTkLabel(frame, text="歌词显示模式:")
    lbl5.grid(row=4, column=0, padx=10, pady=5, sticky="w")
    combo_mode = ctk.CTkComboBox(frame, values=list(mode_map.keys()), width=150)
    combo_mode.set(rev_mode_map.get(config.get("lyric_mode", "smart"), "智能模式 (外文优先翻译)"))
    combo_mode.grid(row=4, column=1, padx=10, pady=5)

    warn_label = ctk.CTkLabel(
        frame, 
        text="⚠️ 提示：若使用「双语对照」加心率显示，\n可能超过 Chatbox 144 字符上限导致截断。", 
        font=ctk.CTkFont(size=11), 
        text_color="gray"
    )
    warn_label.grid(row=5, column=0, columnspan=2, padx=10, pady=(10, 5))

    def on_save():
        try:
            config["listen_ip"] = entry_listen_ip.get().strip()
            config["listen_port"] = int(entry_listen_port.get().strip())
            config["vrc_ip"] = entry_vrc_ip.get().strip()
            config["vrc_port"] = int(entry_vrc_port.get().strip())
            config["lyric_mode"] = mode_map[combo_mode.get()]

            save_config(config)
            update_client_target()
            warn_label.configure(text="✅ 设置已保存！若修改了接收端口，建议重启程序生效。", text_color="green")
        except Exception as err:
            warn_label.configure(text=f"❌ 保存失败: {err}", text_color="red")

    btn_save = ctk.CTkButton(settings_window, text="保存设置", command=on_save, width=120)
    btn_save.pack(pady=(5, 15))

    settings_window.mainloop()

# ----------------- 系统托盘组件 -----------------
def get_tray_image(color="green"):
    if os.path.exists(ICON_FILE):
        try:
            return Image.open(ICON_FILE)
        except Exception:
            pass
    # 回退：动态绘制小圆点
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=color)
    return img

def toggle_state(icon, item):
    global is_running
    is_running = not is_running
    status_str = "已开启" if is_running else "已暂停"
    icon.icon = get_tray_image("green" if is_running else "gray")
    icon.title = f"VRC 歌词代理 [{status_str}]"

def open_settings_from_tray(icon, item):
    threading.Thread(target=open_settings_ui, daemon=True).start()

def exit_app(icon, item):
    icon.stop()
    os._exit(0)

# ----------------- 程序启动入口 -----------------
if __name__ == "__main__":
    _app_mutex = check_single_instance()
    update_client_target()
    
    # 启动后台守护工作线程
    threading.Thread(target=start_osc_server, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()
    threading.Thread(target=run_async_loop, daemon=True).start()

    # 系统托盘菜单
    menu = pystray.Menu(
        pystray.MenuItem("开启/暂停歌词代理", toggle_state, default=True),
        pystray.MenuItem("设置...", open_settings_from_tray),
        pystray.MenuItem("退出程序", exit_app)
    )
    tray_icon = pystray.Icon("VRC_Lyrics", get_tray_image("green"), "VRC 歌词代理 [运行中]", menu)
    tray_icon.run()