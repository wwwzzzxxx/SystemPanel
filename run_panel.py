"""PyInstaller 打包入口（`python -m panel` 的相对导入不适合直接打包）。"""
from panel.app_tk import main

if __name__ == "__main__":
    main()
