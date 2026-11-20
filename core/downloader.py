# -*- coding: utf-8 -*-
"""微信图片下载器：聚焦微信查看器，模拟 Ctrl+S 逐张保存并重命名"""

import io
import os
import shutil
import time
import threading

import pyautogui
import win32gui
import win32con
import win32clipboard
from PIL import Image, ImageGrab

import config
from config import IMAGE_EXTS, VIDEO_EXTS
from core import file_ops
from core.naming import NamingScheme, compute_start
from core.wechat_uia import (
    get_chat_info, sanitize_folder_name, is_nav_disabled,
    click_view_original_button,
)


def list_visible_windows(exclude_hwnd=None):
    """返回 [(hwnd, title)]，仅含可见且有标题的顶层窗口，可排除工具自身"""
    out = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd).strip():
            if exclude_hwnd is None or hwnd != exclude_hwnd:
                out.append((hwnd, win32gui.GetWindowText(hwnd)))
        return True

    win32gui.EnumWindows(cb, None)
    return out


class WeChatDownloader:
    """模拟 Ctrl+S 把微信图片查看器中的图片保存到本地

    流程：
      1. 倒计时让用户把微信图片查看器置于前台
      2. 捕获前台窗口句柄
      3. 循环：Ctrl+S -> 监控目录中新文件 -> 重命名 1001.jpg -> 方向键切下一张
    """

    DIRECTION_KEYS = {'向右': 'right', '向左': 'left'}

    def __init__(self, image_folder, count, wechat_save_src=None,
                 direction='向右', naming=None, start_index=1001, log_cb=None, stop_event=None,
                 selected_hwnd=None, mode='ctrl_s', use_chat_folder=True,
                 download_original_video=True):
        self.image_folder = image_folder
        self.base_image_folder = image_folder
        self.count = count
        self.wechat_save_src = wechat_save_src  # 可选：微信默认保存目录
        # 切图方向同时决定按哪个方向键、以及检查哪个导航按钮是否到边界
        self.direction = direction if direction in self.DIRECTION_KEYS else '向右'
        self.next_key = self.DIRECTION_KEYS[self.direction]
        self.nav_dir = 'left' if self.direction == '向左' else 'right'
        self.naming = naming or NamingScheme.from_preset('1001起序')
        self.start_index = start_index
        self.log = log_cb or (lambda msg, level="info": None)
        self.stop_event = stop_event or threading.Event()
        self.selected_hwnd = selected_hwnd  # 用户手动选择的微信窗口句柄
        self.mode = mode  # 'ctrl_s' | 'clipboard'
        self.use_chat_folder = use_chat_folder  # 是否按微信会话名建子文件夹
        self.download_original_video = download_original_video  # 视频优先下载原文件
        self.chat_name = ''
        self.window_title = config.GUI_CONF.get('window_title', '微信图片下载工具')
        self._last_src = None  # 上一张成功保存的剪贴板源路径（用于排除残留）
        self.active_hwnd = None  # 实际用于置顶/聚焦的微信窗口句柄
        self.seq = start_index  # 当前序号（运行期递增）

    def _countdown(self, seconds=3):
        for i in range(seconds, 0, -1):
            if self.stop_event.is_set():
                return False
            self.log(f"倒计时 {i} 秒，请现在用鼠标点击微信图片查看器窗口，使其置顶并保持焦点…", "warning")
            time.sleep(1)
        return True

    def _capture_foreground(self):
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                title = win32gui.GetWindowText(hwnd)
                self.log(f"已捕获前台窗口：{title}", "input")
                return hwnd
        except Exception as e:
            self.log(f"获取前台窗口失败：{e}", "warning")
        return None

    # ------------------------------------------------------------------
    # 处理新版微信 Ctrl+S 弹出的「另存为」对话框
    # ------------------------------------------------------------------
    def _find_save_dialog(self, timeout=4.0):
        """找微信 Ctrl+S 弹出的「保存/另存为」对话框。

        优先按类名 #32770 + 标题关键字匹配；匹配不到时退化为启发式：
        存在 Edit 子控件且包含文字含『保存/Save』按钮的可见窗口
        （兼容部分微信版本的非标准对话框）。
        返回 (dialog_hwnd, edit_hwnd)，找不到返回 (None, None)。
        """
        save_keywords = ('保存', '另存为', 'Save As', 'Save')

        def by_title():
            out = []
            def cb(hwnd, out):
                if win32gui.IsWindowVisible(hwnd):
                    try:
                        cls = win32gui.GetClassName(hwnd)
                        title = win32gui.GetWindowText(hwnd)
                    except Exception:
                        return True
                    if cls == '#32770' and any(k in title for k in save_keywords):
                        out.append(hwnd)
                return True
            win32gui.EnumWindows(cb, out)
            return out

        def by_controls():
            out = []
            def cb(hwnd, out):
                if win32gui.IsWindowVisible(hwnd):
                    try:
                        title = win32gui.GetWindowText(hwnd)
                    except Exception:
                        title = ''
                    if self.window_title and self.window_title in title:
                        return True  # 排除工具自身窗口
                    edit = [None]
                    save_btn = [False]

                    def child_cb(chwnd, _):
                        try:
                            cls = win32gui.GetClassName(chwnd)
                            txt = win32gui.GetWindowText(chwnd)
                        except Exception:
                            return True
                        if cls == 'Edit' and edit[0] is None:
                            edit[0] = chwnd
                        if cls in ('Button', 'NetUIButton') and any(k in txt for k in save_keywords):
                            save_btn[0] = True
                        return True

                    try:
                        win32gui.EnumChildWindows(hwnd, child_cb, None)
                    except Exception:
                        pass
                    if edit[0] is not None and save_btn[0]:
                        out.append(hwnd)
                return True
            win32gui.EnumWindows(cb, out)
            return out

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event.is_set():
                return None, None
            res = by_title()
            if res:
                dlg = res[0]
                return dlg, self._get_dialog_edit(dlg)
            time.sleep(0.2)
        # 超时再用控件启发式兜底一次
        res = by_controls()
        if res:
            dlg = res[0]
            return dlg, self._get_dialog_edit(dlg)
        return None, None

    def _get_dialog_edit(self, dialog_hwnd):
        """在对话框里找第一个 Edit 控件（文件名输入框）"""
        edit_hwnd = [None]

        def cb(hwnd, _):
            if not edit_hwnd[0] and win32gui.GetClassName(hwnd) == 'Edit':
                edit_hwnd[0] = hwnd
            return True

        try:
            win32gui.EnumChildWindows(dialog_hwnd, cb, None)
        except Exception:
            pass
        return edit_hwnd[0]

    def _read_dialog_ext(self, edit_hwnd):
        """读取当前对话框文件名，返回扩展名；读不到默认 .jpg（微信保存图片通常为 jpg）"""
        if not edit_hwnd:
            return '.jpg'
        try:
            text = win32gui.GetWindowText(edit_hwnd).strip()
            ext = os.path.splitext(text)[1].lower()
            return ext if ext in IMAGE_EXTS else '.jpg'
        except Exception:
            return '.jpg'

    def _dismiss_error_dialog(self, parent_hwnd):
        """点掉微信保存对话框弹出的错误提示（如「文件名无效」），返回是否点掉。"""
        clicked = []

        def child_cb(chwnd, _):
            try:
                txt = win32gui.GetWindowText(chwnd)
                cls = win32gui.GetClassName(chwnd)
            except Exception:
                return True
            if cls == 'Button' and txt in ('确定', 'OK', '是(Y)', '是', 'Yes'):
                try:
                    win32gui.PostMessage(chwnd, win32con.BM_CLICK, 0, 0)
                except Exception:
                    pass
            return True

        def cb(hwnd, out):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    cls = win32gui.GetClassName(hwnd)
                    title = win32gui.GetWindowText(hwnd)
                except Exception:
                    return True
                # 只处理依附于保存对话框的错误弹窗：同类名且标题常见
                if cls == '#32770' and title in ('保存', 'Save', '错误', 'Error'):
                    try:
                        win32gui.EnumChildWindows(hwnd, child_cb, None)
                    except Exception:
                        pass
                    out.append(hwnd)
            return True

        win32gui.EnumWindows(cb, clicked)
        if clicked:
            time.sleep(0.2)
        return bool(clicked)

    def _set_clipboard_text(self, text):
        """把 Unicode 字符串写入剪贴板（CF_UNICODETEXT），供 Ctrl+V 粘贴。"""
        for _ in range(6):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
                win32clipboard.CloseClipboard()
                return True
            except Exception:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
                time.sleep(0.12)
        return False

    def _paste_text(self, text):
        """把 text 写入剪贴板，然后发送 Ctrl+A / Ctrl+V。用于对话框地址栏/文件名输入。"""
        if not self._set_clipboard_text(text):
            return False
        time.sleep(0.05)
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(0.05)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.05)
        return True

    def _navigate_dialog_to_folder(self, dialog_hwnd, folder):
        """尝试把文件对话框的当前目录切到 folder。

        不用 pyautogui.typewrite（中文输入法下会把 \ 变成 、、数字错位），
        而是把路径写入剪贴板，Ctrl+L/Alt+D 聚焦地址栏后 Ctrl+V 粘贴回车。
        返回是否执行了切目录动作（不保证成功，后续以实际落点校验为准）。
        """
        for hotkey in (('ctrl', 'l'), ('alt', 'd')):
            try:
                pyautogui.keyDown(hotkey[0])
                pyautogui.keyDown(hotkey[1])
                pyautogui.keyUp(hotkey[1])
                pyautogui.keyUp(hotkey[0])
            except Exception:
                pyautogui.hotkey(*hotkey)
            time.sleep(0.15)
            if self._paste_text(folder):
                pyautogui.press('return')
                self.log(f"已尝试切换目录 -> {folder}", "info")
                time.sleep(0.7)
            # 清掉可能弹出的「找不到路径」之类的错误提示
            self._dismiss_error_dialog(dialog_hwnd)
            if win32gui.IsWindow(dialog_hwnd) and win32gui.IsWindowVisible(dialog_hwnd):
                return True
        return False

    def _fill_save_dialog(self, dialog_hwnd, full_path):
        """激活对话框，把文件存到 full_path 所在目录，文件名为 basename。

        微信/部分程序的保存对话框不接受完整路径作为文件名，因此：
          1) 先用 Ctrl+L/Alt+D 聚焦地址栏，输入目标目录并回车切换；
          2) 再只把文件名填入文件名框；
          3) 用 Alt+S 触发保存，回车兜底。
        """
        folder = os.path.dirname(full_path)
        filename = os.path.basename(full_path)

        try:
            win32gui.ShowWindow(dialog_hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(dialog_hwnd)
        except Exception:
            pass
        time.sleep(0.4)

        # 清掉之前可能残留的错误提示
        self._dismiss_error_dialog(dialog_hwnd)

        # 1) 先切目录
        self._navigate_dialog_to_folder(dialog_hwnd, folder)

        # 2) 再填文件名（只填 basename，不要完整路径；同样用剪贴板粘贴避免中文输入法乱码）
        edit_hwnd = self._get_dialog_edit(dialog_hwnd)
        if edit_hwnd:
            try:
                win32gui.SetFocus(edit_hwnd)
            except Exception:
                pass
            time.sleep(0.1)
            # 优先直接设置文本；若失败或含中文，改用剪贴板粘贴
            try:
                win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, filename)
            except Exception:
                self._paste_text(filename)
        else:
            self._paste_text(filename)

        self.log(f"已填写文件名：{filename}，目录：{folder}", "info")
        time.sleep(0.2)

        # 3) 提交保存：优先 Alt+S，对话框仍开着则回车兜底
        pyautogui.hotkey('alt', 's')
        time.sleep(0.5)
        self._dismiss_error_dialog(dialog_hwnd)
        if win32gui.IsWindow(dialog_hwnd) and win32gui.IsWindowVisible(dialog_hwnd):
            pyautogui.press('return')
            time.sleep(0.4)
            self._dismiss_error_dialog(dialog_hwnd)

    def _wait_window_close(self, hwnd, timeout=5.0):
        """等待指定窗口关闭"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event.is_set():
                return False
            if not win32gui.IsWindow(hwnd):
                return True
            time.sleep(0.2)
        return False

    def _find_saved_target(self, base):
        """在目标文件夹里查找 base 命名（任意扩展名）已存在的图片，返回实际路径；找不到返回 None。
        用于兼容『对话框实际保存的扩展名与预期不同』的情况。"""
        try:
            for f in os.listdir(self.image_folder):
                if f.startswith(base) and os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                    return os.path.join(self.image_folder, f)
        except Exception:
            pass
        return None

    def _rescue_via_monitor(self, base, timeout=8.0):
        """对话框接管彻底失败时，递归扫描微信默认保存目录里『最近修改』的图片，
        把它搬到目标文件夹的 base 命名下，保证最终落点在用户指定的目录。
        返回成功保存的目标路径，或 None。
        """
        roots = [d for d in [self.wechat_save_src] if d and os.path.isdir(d)]
        if not roots:
            return None
        now = time.time()
        deadline = now + timeout
        while time.time() < deadline:
            if self.stop_event.is_set():
                return None
            best = None  # (mtime, path)
            for root in roots:
                try:
                    for dirpath, dirs, files in os.walk(root):
                        # 剪枝：只下钻最近 20 秒内改动过的目录，避免整棵微信目录全扫
                        dirs[:] = [d for d in dirs
                                   if time.time() - os.path.getmtime(os.path.join(dirpath, d)) <= 20]
                        for f in files:
                            ext = os.path.splitext(f)[1].lower()
                            if ext not in IMAGE_EXTS:
                                continue
                            p = os.path.join(dirpath, f)
                            try:
                                m = os.path.getmtime(p)
                            except Exception:
                                continue
                            if time.time() - m <= 15 and (best is None or m > best[0]):
                                best = (m, p)
                except Exception:
                    continue
            if best:
                src = best[1]
                ext = os.path.splitext(src)[1].lower() or '.jpg'
                target = os.path.join(self.image_folder, f"{base}{ext}")
                try:
                    file_ops.ensure_writable(target)
                    if os.path.abspath(os.path.dirname(src)) == os.path.abspath(self.image_folder):
                        os.rename(src, target)
                    else:
                        shutil.move(src, target)
                    file_ops.ensure_writable(target)
                    return target
                except Exception:
                    return None
            time.sleep(0.4)
        return None

    # ------------------------------------------------------------------
    # 剪贴板模式：Ctrl+C 复制图片，从剪贴板读取并保存
    # ------------------------------------------------------------------
    def _clear_clipboard(self):
        """清空剪贴板（带重试，避免被其他进程占用时失败）"""
        for _ in range(6):
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.CloseClipboard()
                return True
            except Exception:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
                time.sleep(0.12)
        return False

    def _read_clipboard_image(self):
        """读取剪贴板中的图片。返回 ('image', Image) / ('files', [paths]) / (None, None)"""
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return (None, None)
        if isinstance(data, Image.Image):
            return ('image', data)
        if isinstance(data, (list, tuple)) and len(data) > 0:
            return ('files', list(data))
        return (None, None)

    def _wait_clipboard_ready(self, timeout=3.0, exclude=None):
        """Ctrl+C 后轮询剪贴板，直到出现图片数据；exclude 用于排除上一张的残留路径"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.stop_event.is_set():
                return (None, None)
            kind, data = self._read_clipboard_image()
            if kind == 'image':
                return (kind, data)
            if kind == 'files':
                src = data[0]
                if src != exclude:
                    return (kind, data)
                # 仍是上一张残留，继续等微信写入新路径
            time.sleep(0.15)
        return (None, None)

    def _get_original_video(self, compressed_src, timeout=12.0):
        """点击「查看原视频」拿到微信落盘的原文件，返回原文件路径或 None。

        Ctrl+C 拿到的视频是微信压缩版（几百 KB）；点「查看原视频」后微信会在
        同目录生成 ``<原名>_raw<扩展名>`` 原文件（体积大很多）。这里点击按钮后
        轮询等待该文件出现且大小稳定，再交给调用方复制。失败时返回 None，
        调用方自动回退到压缩版，不中断下载。
        """
        base, ext = os.path.splitext(compressed_src)
        raw_path = base + '_raw' + ext
        try:
            clicked = click_view_original_button(self.active_hwnd or self.selected_hwnd)
        except Exception:
            clicked = None
        if not clicked:
            return None

        self.log("已点击「查看原视频」，等待原文件落盘…", "info")
        deadline = time.time() + timeout
        last_size = -1
        stable = 0
        while time.time() < deadline:
            if self.stop_event.is_set():
                return None
            try:
                if os.path.exists(raw_path):
                    size = os.path.getsize(raw_path)
                    if size > 0 and size == last_size:
                        stable += 1
                        if stable >= 2:  # 连续两次大小一致，认为写入完成
                            return raw_path
                    else:
                        stable = 0
                    last_size = size
            except Exception:
                pass
            time.sleep(0.3)
        return None

    def _clipboard_copy_image(self, base, max_attempts=4):
        """发送 Ctrl+C（必要时多次重发），从剪贴板读取图片保存为 base（文件名基础名）。
        成功返回 (True, ext, src)；全部失败返回 (False, None, None)。

        内部多次「重发 Ctrl+C + 轮询剪贴板」，专门应对两类偶发竞态：
          1) 切到下一张后图片还没渲染好（大图/慢网），Ctrl+C 当时抓空；
          2) 首次 Ctrl+C 焦点瞬间漂移，微信没接住。
        单张最多等待约 max_attempts×2 秒，比「只在开头按一次」更能救回图片。
        """
        for attempt in range(1, max_attempts + 1):
            if self.stop_event.is_set():
                return False, None, None

            # 每次尝试前重新抢回微信窗口焦点，避免焦点漂移导致 Ctrl+C 落空
            # active_hwnd 可能是自动纠正后的图片查看器，优先于用户手选的窗口
            focus_hwnd = self.active_hwnd or self.selected_hwnd
            if focus_hwnd:
                try:
                    win32gui.ShowWindow(focus_hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(focus_hwnd)
                except Exception:
                    pass

            self._clear_clipboard()
            time.sleep(0.08)
            pyautogui.hotkey('ctrl', 'c')
            if attempt == 1:
                self.log(f"已发送 Ctrl+C（第 {base} 张）", "info")
            else:
                self.log(f"第 {base} 张重发 Ctrl+C（{attempt}/{max_attempts}）", "info")

            kind, data = self._wait_clipboard_ready(timeout=2.0, exclude=self._last_src)
            if kind == 'image':
                target = os.path.join(self.image_folder, f"{base}.png")
                try:
                    file_ops.ensure_writable(target)
                    data.save(target, 'PNG')
                    return True, '.png', None
                except Exception as e:
                    self.log(f"图片保存失败：{e}", "warning")
                    return False, None, None
            if kind == 'files':
                src = data[0]
                ext = os.path.splitext(src)[1].lower() or '.png'
                # 视频：优先拿「查看原视频」的原文件，失败则回退压缩版
                if self.download_original_video and ext in VIDEO_EXTS:
                    original = self._get_original_video(src)
                    if original:
                        src = original
                        ext = os.path.splitext(src)[1].lower() or ext
                        size_mb = os.path.getsize(src) / 1024 / 1024
                        self.log(f"使用原视频文件（{size_mb:.1f} MB）", "success")
                target = os.path.join(self.image_folder, f"{base}{ext}")
                try:
                    # 微信复制来的源文件常带只读属性，copy2 会把它带到目标；
                    # 覆盖已存在的只读文件也会失败，所以前后都清一次。
                    file_ops.ensure_writable(target)
                    shutil.copy2(src, target)
                    file_ops.ensure_writable(target)
                    return True, ext, src
                except Exception as e:
                    self.log(f"复制文件失败：{e}", "warning")
                    return False, None, None

            # 剪贴板未就绪，短暂等待后重发（除非已是最后一次）
            if attempt < max_attempts:
                time.sleep(0.3)

        return False, None, None

    def _apply_chat_folder(self, hwnd):
        """读取当前微信会话名，把保存目录切到同名子文件夹。

        识别失败时保持原目录不变，不影响下载主流程。
        返回最终用于置顶/聚焦的窗口句柄（可能自动纠正为查看器窗口）。
        """
        if not hwnd:
            self.log("没有捕获到微信窗口，无法识别会话，图片保存到原目录", "warning")
            return hwnd
        try:
            info = get_chat_info(hwnd)
        except Exception as e:
            self.log(f"识别微信会话失败：{e}，图片保存到原目录", "warning")
            return hwnd

        if not info:
            self.log("未识别到微信图片查看器会话，图片保存到原目录", "warning")
            return hwnd

        viewer_hwnd = info.get('viewer_hwnd')
        if viewer_hwnd and viewer_hwnd != hwnd:
            try:
                title = win32gui.GetWindowText(viewer_hwnd)
            except Exception:
                title = '图片和视频'
            self.log(f"已自动定位到微信查看器窗口：{title}", "input")
            hwnd = viewer_hwnd
            self.active_hwnd = hwnd

        name = (info.get('name') or '').strip()
        chat_id = info.get('chat_id') or ''
        kind = '群聊' if info.get('is_group') else '联系人'
        if not name:
            self.log(
                f"已识别会话 ID（{kind}）：{chat_id}，但未捕获到名称；"
                "请重新打开该图片查看器后再下载，以便记录群名。图片先保存到原目录",
                "warning",
            )
            return hwnd

        safe = sanitize_folder_name(name)
        if not safe:
            self.log(f"会话名「{name}」不能用作文件夹名，图片保存到原目录", "warning")
            return hwnd

        target = os.path.join(self.base_image_folder, safe)
        if os.path.exists(target) and not os.path.isdir(target):
            self.log(f"子文件夹路径被同名文件占用：{target}，图片保存到原目录", "warning")
            return hwnd
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            self.log(f"创建会话子文件夹失败：{e}，图片保存到原目录", "warning")
            return hwnd

        self.chat_name = name
        self.image_folder = target
        self.log(f"已识别会话：{name}（{kind}），保存到子文件夹「{safe}」", "input")
        return hwnd

    def run(self):
        if not self._countdown(3):
            self.log("下载已取消", "warning")
            return

        if self.selected_hwnd:
            hwnd = self.selected_hwnd
            try:
                self.log(f"使用已选窗口：{win32gui.GetWindowText(hwnd)}", "input")
            except Exception:
                self.log("使用已选窗口", "input")
        else:
            hwnd = self._capture_foreground()
            if hwnd:
                self.log("未手动选择窗口，已自动捕获前台窗口（请确保微信已置顶）", "warning")
        self.active_hwnd = hwnd

        if self.use_chat_folder:
            hwnd = self._apply_chat_folder(hwnd)

        file_ops.ensure_folder(self.image_folder)

        # 根据命名方案计算实际起始序号（避免覆盖文件夹已有同名文件）
        self.seq = compute_start(self.image_folder, self.naming)
        self.log(f"命名方案：{self.naming.preset}（起始序号 {self.seq}，示例：{', '.join(self.naming.preview(3))}）", "info")

        saved = 0
        for i in range(self.count):
            if self.stop_event.is_set():
                self.log("下载已停止", "warning")
                break

            base = self.naming.format(self.seq)
            self.seq += 1  # 无论成功失败序号都顺延（失败留空号，不中断整体）

            # 确保微信查看器有焦点
            if hwnd:
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            time.sleep(0.4)

            if self.mode == 'clipboard':
                # ---------- 剪贴板模式（内部多次重发重试，偶发竞态不中断整体） ----------
                ok, ext, src = self._clipboard_copy_image(base)
                if ok:
                    self._last_src = src  # 记录刚保存的源路径，供下次排除残留
                    saved += 1
                    self.log(f"已保存 {base}{ext} ({saved}/{self.count})", "success")
                else:
                    self.log(f"第 {base} 张多次重试仍失败，跳过并继续下一张", "error")

            else:
                # ---------- Ctrl+S 模式 ----------
                self.log(f"目标保存文件夹：{self.image_folder}", "info")
                pyautogui.hotkey('ctrl', 's')
                self.log(f"已发送 Ctrl+S（第 {i+1}/{self.count} 张，命名 {base}）", "info")
                time.sleep(0.5)  # 给对话框一点弹出时间

                saved_this = False

                # 1) 尝试接管「另存为」对话框，把完整目标路径写进去并用 Alt+S 提交
                dialog_hwnd, edit_hwnd = self._find_save_dialog(timeout=4.0)
                if dialog_hwnd:
                    ext = '.jpg'
                    try:
                        if edit_hwnd:
                            ext = self._read_dialog_ext(edit_hwnd)
                    except Exception:
                        pass
                    target = os.path.join(self.image_folder, f"{base}{ext}")
                    file_ops.ensure_writable(target)
                    self._fill_save_dialog(dialog_hwnd, target)
                    self._wait_window_close(dialog_hwnd, timeout=6.0)
                    time.sleep(0.5)
                    # 优先精确匹配，其次按编号前缀查找（兼容扩展名差异）
                    real = target if os.path.exists(target) else self._find_saved_target(base)
                    if real and os.path.exists(real):
                        saved_this = True
                        self.log(f"已保存 {os.path.basename(real)} ({i+1}/{self.count}) -> {self.image_folder}", "success")
                else:
                    # 2) 找不到对话框：可能检测失败但对话框实际存在，直接 Alt+S 提交
                    #    不盲填完整路径，避免触发「文件名无效」错误。
                    self.log("未找到保存对话框，直接 Alt+S 提交当前对话框…", "warning")
                    pyautogui.hotkey('alt', 's')
                    time.sleep(0.5)
                    pyautogui.press('return')
                    time.sleep(1.0)
                    real = self._find_saved_target(base)
                    if real and os.path.exists(real):
                        saved_this = True
                        self.log(f"已保存 {os.path.basename(real)} ({i+1}/{self.count}) -> {self.image_folder}", "success")

                # 3) 主流程仍失败：递归扫描微信默认保存目录里最新修改的图，搬到你设的文件夹
                if not saved_this:
                    real = self._rescue_via_monitor(base, timeout=8.0)
                    if real and os.path.exists(real):
                        saved_this = True
                        self.log(f"已从微信默认目录搬移 {os.path.basename(real)} ({i+1}/{self.count}) -> {self.image_folder}", "success")

                if saved_this:
                    saved += 1
                else:
                    self.log(f"第 {i+1} 张保存失败：微信未聚焦或默认保存目录不可访问，已停止", "error")
                    break

            # 自动结束：导航按钮被禁用说明已到当前方向的边界，不再切图
            boundary = '第一张' if self.direction == '向左' else '最后一张'
            try:
                if is_nav_disabled(hwnd, self.nav_dir):
                    # Qt 状态可能晚一拍，短间隔再确认一次，避免误停
                    time.sleep(0.2)
                    if is_nav_disabled(hwnd, self.nav_dir):
                        self.log(f"已是{boundary}，自动结束", "success")
                        break
            except Exception:
                pass

            # 切到下一张
            pyautogui.press(self.next_key)
            time.sleep(0.6)

        self.log(f"下载完成，共保存 {saved} 张图片到 {self.image_folder}", "success")
