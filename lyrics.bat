@echo off
title VRChat Music & Chatbox Launcher

:: 1. 启动 MagicChatbox（替换为你的实际路径）
start "" "D:\download\MagicChatbox-0.9.226.zip"

:: 2. 等待 2 秒确保端口准备就绪
timeout /t 2 >nul

:: 3. 切换到当前目录并静默/后台启动 Python 代理脚本
cd /d "%~dp0"
start "VRC_Lyrics_Proxy" python gey_lyrics.py

exit