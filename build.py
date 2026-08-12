# -*- coding: UTF-8 -*-
"""
打包脚本：把 siwu-steam-status-monitor 插件打包为 zip。

用法（在项目根目录下执行）：
    python pluginsServer/siwu-steam-status-monitor-1_0/build.py
"""

import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(ROOT, 'plugins', 'siwu-steam-status-monitor-1.2.zip')


def _add_dir(zf: zipfile.ZipFile, src_dir: str):
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for f in files:
            if f.endswith(('.pyc', '.pyo')):
                continue
            full = os.path.join(root, f)
            arcname = os.path.relpath(full, os.path.dirname(src_dir)).replace('\\', '/')
            print(f'  + {arcname}')
            zf.write(full, arcname=arcname)


def build():
    os.makedirs(os.path.dirname(OUTPUT_ZIP), exist_ok=True)

    with zipfile.ZipFile(OUTPUT_ZIP, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(os.listdir(PLUGIN_DIR)):
            if f == os.path.basename(__file__) or f.startswith('__pycache__'):
                continue
            full = os.path.join(PLUGIN_DIR, f)
            if os.path.isfile(full):
                print(f'  + {f}')
                zf.write(full, arcname=f)
            elif os.path.isdir(full) and f in ('fonts',):
                _add_dir(zf, full)

    print(f'\ncreated: {OUTPUT_ZIP} ({os.path.getsize(OUTPUT_ZIP)} bytes)')


if __name__ == '__main__':
    build()
