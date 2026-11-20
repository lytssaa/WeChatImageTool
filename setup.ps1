# setup.ps1

$ErrorActionPreference = "Stop"

# 2. 创建虚拟环境
Write-Host "Setting up development environment..."
python -m venv .venv
& .\.venv\Scripts\Activate.ps1

# 3. 安装依赖
Write-Host "Installing dependencies..."
pip install -r requirements.txt


Write-Host "Setup complete! Activate with: .venv\Scripts\activate"
