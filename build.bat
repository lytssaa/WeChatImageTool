@echo off
setlocal
call .venv\Scripts\activate.bat
python -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name WeChatImageTool ^
    --collect-all customtkinter ^
    --collect-all uiautomation ^
    --collect-all comtypes ^
    --hidden-import win32com ^
    --hidden-import pywin32 ^
    --hidden-import pyautogui ^
    --hidden-import win32clipboard ^
    --hidden-import PIL.ImageGrab ^
    main.py
endlocal
pause
