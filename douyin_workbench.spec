"""Windows 授权版单文件包：只包含验签公钥，绝不收集管理员私钥。"""
from pathlib import Path
import sys
import runpy
from PyInstaller.utils.hooks import collect_data_files
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

root = Path(SPECPATH)
if sys.platform != 'win32':
    raise SystemExit('此配置仅用于 Windows EXE，请在 Windows Python 3.12 环境打包。')
public_key = root / 'src' / 'licensing' / 'license_public_key.pem'
if not public_key.is_file():
    raise SystemExit('缺少发行公钥：src/licensing/license_public_key.pem。请使用管理员签发工具生成密钥，只复制公钥，禁止复制私钥。')
try:
    if not isinstance(serialization.load_pem_public_key(public_key.read_bytes()), Ed25519PublicKey):
        raise ValueError('公钥必须为 Ed25519')
except ValueError as error:
    raise SystemExit(f'发行公钥无效：{error}')
# 构建时由同一个绘制函数生成多尺寸 ICO，无需手动维护两套图标。
icon_path = root / 'build' / 'workbench.ico'
icon_path.parent.mkdir(parents=True, exist_ok=True)
create_icon = runpy.run_path(str(root / 'src' / 'app_icon.py'))['create_app_icon']
create_icon().save(icon_path, format='ICO', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
datas = [
    (str(root / 'src' / 'web_console.html'), '.'),
    # 四个 Tab、共享组件、样式和脚本由服务端组合成一个受 CSP 保护的页面。
    (str(root / 'src' / 'web_console_parts'), 'web_console_parts'),
    (str(public_key), 'licensing'),
] + collect_data_files('customtkinter')
# 只打包持久化配置，不把运行队列或以后误放的密钥带给用户。
for name in ('web_console_settings.json', 'capture_settings.json', 'capture_presets.json',
             'comment_reply_xpath.json', 'reply_presets.json'):
    config = root / 'src' / 'config' / name
    if not config.is_file():
        raise SystemExit(f'缺少默认配置：{config}')
    datas.append((str(config), 'config'))
# 仅列入公钥资源，不收集 licensing 目录中的其他文件或管理员签发工具。
a = Analysis([str(root / 'src' / 'web_console.py')], pathex=[str(root / 'src')],
             binaries=[], datas=datas, hiddenimports=['pystray._win32'], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['license_issuer'], noarchive=False)
# 检查最终收集清单，包括依赖钩子或资源符号链接，防止管理员私钥目录意外进入发行包。
admin_key_directories = [(root / 'tools' / 'admin_keys').resolve(), (root / 'admin_keys').resolve()]
for _, source, _ in [*a.datas, *a.binaries]:
    if any(Path(source).resolve().is_relative_to(directory) for directory in admin_key_directories):
        raise SystemExit('禁止打包管理员私钥目录，请检查资源收集配置。')
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='抖音评论工作台',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(icon_path))
