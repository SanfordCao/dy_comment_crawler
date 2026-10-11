"""Windows 基础版单文件包：直接启动工作台，不包含授权模块或密钥。"""
from pathlib import Path
import sys
import runpy

root = Path(SPECPATH)
if sys.platform != 'win32':
    raise SystemExit('此配置仅用于 Windows EXE，请在 Windows Python 3.12 环境打包。')
# 构建时由同一个绘制函数生成多尺寸 ICO，无需手动维护两套图标。
icon_path = root / 'build' / 'workbench.ico'
icon_path.parent.mkdir(parents=True, exist_ok=True)
create_icon = runpy.run_path(str(root / 'src' / 'app_icon.py'))['create_app_icon']
create_icon().save(icon_path, format='ICO', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
datas = [
    (str(root / 'src' / 'web_console.html'), '.'),
]
# 只打包持久化配置，不把运行队列或以后误放的密钥带给用户。
for name in ('web_console_settings.json', 'capture_settings.json', 'capture_presets.json'):
    config = root / 'src' / 'config' / name
    if not config.is_file():
        raise SystemExit(f'缺少默认配置：{config}')
    datas.append((str(config), 'config'))
# 显式排除预留的授权代码，避免开发环境安装的可选依赖进入基础发行包。
a = Analysis([str(root / 'src' / 'web_console.py')], pathex=[str(root / 'src')],
             binaries=[], datas=datas, hiddenimports=['pystray._win32'], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['activation_window', 'offline_license', 'license_issuer',
                       'customtkinter', 'cryptography'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='抖音评论工作台',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(icon_path))
