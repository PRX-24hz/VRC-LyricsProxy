import sys
import os
import socket
import asyncio
import requests
import json
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager
)

LOG_FILE = "diagnosis.log"

def log(msg, status="INFO"):
    symbol = {"INFO": "[*]", "OK": "[+]", "WARN": "[!]", "ERR": "[-]"}.get(status, "[*]")
    line = f"{symbol} {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# 1. 检查端口占用状态
def check_port_available(ip, port):
    log(f"正在检测端口占用情况: {ip}:{port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, port))
        sock.close()
        log(f"端口 {port} 空闲，代理可正常绑定监听！", "OK")
        return True
    except OSError as e:
        log(f"端口 {port} 绑定失败！可能已被占用或未释放（错误码: {e.errno}）", "ERR")
        return False

# 2. 检查网易云 API 连通性
def check_ncm_api():
    log("正在测试网易云在线歌词接口连通性...")
    try:
        res = requests.get(
            "https://music.163.com/api/search/get/web?s=海阔天空&type=1&limit=1",
            timeout=5
        )
        if res.status_code == 200:
            log("网易云 API 请求畅通！", "OK")
            return True
        else:
            log(f"网易云 API 状态码异常: {res.status_code}", "WARN")
            return False
    except Exception as e:
        log(f"无法访问网易云 API，请检查本机代理软件或网络环境: {e}", "ERR")
        return False

# 3. 检查 Windows 媒体监听权限与网易云广播状态
async def check_media_session():
    log("正在检测 Windows SMTC 媒体总线广播状态...")
    try:
        manager = await MediaManager.request_async()
        sessions = manager.get_sessions()
        log(f"检测到当前系统活跃的媒体会话数量: {len(sessions)}")
        
        found_ncm = False
        for s in sessions:
            app_id = (s.source_app_user_model_id or "").lower()
            log(f"  -> 捕获到媒体源: {app_id}")
            if any(kw in app_id for kw in ["cloudmusic", "netease"]):
                found_ncm = True
                props = await s.try_get_media_properties_async()
                if props:
                    log(f"已成功捕获网易云曲目: 《{props.title}》 - {props.artist}", "OK")

        if not found_ncm:
            log("未找到网易云音乐会话！请确认网易云客户端已启动并正在播放歌曲。", "WARN")
        return found_ncm
    except Exception as e:
        log(f"Windows SMTC 权限获取异常: {e}", "ERR")
        return False

# 4. 检查配置文件状态
def check_config():
    log("正在检测本地配置文件 config.json...")
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
                log(f"读取到配置文件内容: {cfg}", "OK")
                return cfg
        except Exception as e:
            log(f"配置文件损坏，无法正常解析 JSON: {e}", "ERR")
            return None
    else:
        log("未检测到本地 config.json，主程序启动时将使用默认配置自动生成。", "INFO")
        return None

async def main():
    if os.path.exists(LOG_FILE):
        try:
            os.remove(LOG_FILE)
        except Exception:
            pass

    print("=" * 55)
    print("      VRChat 歌词/心率中继代理 - 环境诊断工具")
    print("=" * 55)
    
    cfg = check_config()
    listen_port = cfg.get("listen_port", 9005) if cfg else 9005
    check_port_available("127.0.0.1", listen_port)
    check_ncm_api()
    await check_media_session()
    
    print("=" * 55)
    log("诊断完成！结果已同步保存至同目录下的 diagnosis.log", "INFO")
    print("=" * 55)
    input("\n按回车键（Enter）退出诊断程序...")

if __name__ == "__main__":
    asyncio.run(main())