@echo off
REM 激活 Conda 环境并运行 Python 脚本
call conda activate D:\ComfyUI-0.2.7\cdvenv && cmd.exe /C python main.py --preview-method auto --listen 0.0.0.0 --port 43568
cmd /k
