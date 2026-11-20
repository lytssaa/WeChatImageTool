# -*- coding: utf-8 -*-
"""纯文件操作：目录确保 / 清空 / 图片列表"""

import os
import shutil
import stat
from config import IMAGE_EXTS


def ensure_writable(path):
    """清除文件的只读属性。微信复制来的文件常带只读，会导致覆盖保存失败。"""
    try:
        if os.path.exists(path):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def ensure_folder(path):
    """确保目录存在，不存在则创建"""
    os.makedirs(path, exist_ok=True)
    return path


def clear_folder(folder):
    """递归删除文件夹内所有图片文件，返回删除数量；顺手清理空子目录"""
    if not os.path.isdir(folder):
        return 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(folder, topdown=False):
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.path.isfile(full):
                    ensure_writable(full)
                    os.remove(full)
                    count += 1
            except Exception:
                # 跳过无法删除的文件
                pass
        if os.path.abspath(dirpath) != os.path.abspath(folder):
            try:
                os.rmdir(dirpath)  # 仅当目录已空时才会成功
            except OSError:
                pass
    return count


def list_images_sorted(folder):
    """返回文件夹内图片按文件名排序的完整路径列表"""
    if not os.path.isdir(folder):
        return []
    files = []
    for name in os.listdir(folder):
        ext = os.path.splitext(name)[1].lower()
        if ext in IMAGE_EXTS:
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                files.append(full)
    # 数字前缀优先按数值排，否则字典序
    def sort_key(p):
        base = os.path.splitext(os.path.basename(p))[0]
        if base.isdigit():
            return (0, int(base), "")
        return (1, 0, base)
    files.sort(key=sort_key)
    return files


def next_index(folder, start=1001):
    """返回文件夹内下一个可用序号（避免覆盖已有文件）"""
    existing = {os.path.splitext(os.path.basename(p))[0] for p in list_images_sorted(folder)}
    i = start
    while f"{i:04d}" in existing:
        i += 1
    return i
