# -*- coding: utf-8 -*-
"""
屏幕录制器 (ScreenRecorder) —— 独立单文件 Windows 小工具
=========================================================
全屏 / 区域录制，输出 MP4（H.264）。支持热键开始停止、倒计时、
录制时自动最小化、鼠标高亮、可选录制系统声音。

许可证: MIT
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  必须显式导入，打包后 ctypes.wintypes 属性才可用
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

from ui_kit import (AppBase, AppleButton, F, Pill, RoundedFrame, SegmentedControl,
                    THEME, ToggleSwitch, open_uri, px, setup_dpi)

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import win_input

APP_NAME = "屏幕录制器"
APP_VERSION = "1.0.0"

try:                       # Pillow：绘制鼠标指针 + 区域选择器预览
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    HAVE_PIL = True
except Exception:  # noqa: BLE001
    HAVE_PIL = False

try:
    import mss
    HAVE_MSS = True
except Exception:  # noqa: BLE001
    HAVE_MSS = False

CREATE_NO_WINDOW = 0x08000000


# ============================================================ ffmpeg 定位

def find_ffmpeg() -> str:
    """优先用系统 PATH，其次用内置（imageio-ffmpeg / _MEIPASS）"""
    exe = "ffmpeg.exe"
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(folder.strip('"'), exe)
        if os.path.isfile(p):
            return p
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.isfile(p):
            return p
    except Exception:  # noqa: BLE001
        pass
    base = getattr(sys, "_MEIPASS", "")
    for rel in ("ffmpeg.exe", "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
                "bin/ffmpeg.exe"):
        p = os.path.join(base, rel.replace("/", os.sep)) if base else ""
        if p and os.path.isfile(p):
            return p
    return ""


def virtual_screen():
    u = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    return (u.GetSystemMetrics(SM_XVIRTUALSCREEN), u.GetSystemMetrics(SM_YVIRTUALSCREEN),
            u.GetSystemMetrics(SM_CXVIRTUALSCREEN), u.GetSystemMetrics(SM_CYVIRTUALSCREEN))


def cursor_pos():
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _mkss():
    if not HAVE_MSS:
        raise RuntimeError("缺少 mss 模块")
    try:
        return mss.MSS()          # mss >= 9
    except AttributeError:
        return mss.mss()


# ============================================================ 录制引擎

class Recorder:
    """抓屏 → 管道喂给 ffmpeg → H.264 mp4"""

    def __init__(self):
        self.proc = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.recording = False
        self.frames = 0
        self.started = 0.0
        self.out_path = ""
        self.area = {"left": 0, "top": 0, "width": 0, "height": 0}
        self.audio_device = ""
        self.segments = []
        self.draw_cursor = False
        self._crf = 1
        self._pipe_lock = threading.RLock()
        self.on_event = lambda *a: None

    # -- 状态
    @property
    def elapsed(self):
        return time.perf_counter() - self.started if self.started else 0.0

    def start(self, region, fps, crf, out_path, draw_cursor=False,
              audio_device="", minimize_note=""):
        if self.recording:
            return False
        exe = find_ffmpeg()
        if not exe:
            raise RuntimeError("未找到 ffmpeg（内置或系统 PATH 均没有）")
        self.stop_flag.clear()
        self.frames = 0
        self.out_path = out_path
        self.fps = fps
        self.audio_device = audio_device
        self.segments = []                 # 尺寸变化时的分段文件
        self.area = self._norm_area(region)
        self._crf = crf
        self._spawn(exe, self.area, out_path, draw_cursor)
        self.recording = True
        self.started = time.perf_counter()
        self.thread = threading.Thread(target=self._loop, args=(self.area, draw_cursor),
                                       daemon=True)
        self.thread.start()
        return True

    @staticmethod
    def _norm_area(region):
        """把 (l, t, w, h) 规整成 mss 用的 area 字典，宽高取偶数。"""
        area = {"left": int(region[0]), "top": int(region[1]),
                "width": int(region[2]), "height": int(region[3])}
        area["width"] -= area["width"] % 2
        area["height"] -= area["height"] % 2
        if area["width"] < 2 or area["height"] < 2:
            raise RuntimeError("选区太小")
        return area

    def _spawn(self, exe, area, out_path, draw_cursor):
        """启动一个 ffmpeg 管道。"""
        w, h = area["width"], area["height"]
        cmd = [exe, "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgra",
               "-s", "%dx%d" % (w, h), "-framerate", str(self.fps), "-i", "-"]
        use_audio = bool(self.audio_device)
        if use_audio:
            cmd += ["-f", "dshow", "-i", "audio=%s" % self.audio_device]
        preset = {0: "ultrafast", 1: "veryfast", 2: "faster"}.get(
            self._crf, "veryfast")
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(self._crf),
                "-pix_fmt", "yuv420p"]
        if use_audio:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
        cmd += [out_path]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE,
                                     creationflags=CREATE_NO_WINDOW)
        self.draw_cursor = draw_cursor

    def set_crf(self, crf):
        self._crf = crf

    def resize_to(self, region):
        """录制中改变取景尺寸：把当前分段收尾，用新尺寸续录到新分段。

        ffmpeg 的输入尺寸在启动时固定，无法中途改变，所以采用「分段录制 +
        最后 concat」的方式。窗口跟随移动时只有尺寸变了才需要重开管道；
        只是平移（宽高不变）则无需任何操作。
        """
        if not self.recording:
            return False
        new_area = self._norm_area(region)
        if (new_area["width"], new_area["height"]) == \
                (self.area["width"], self.area["height"]):
            self.area = new_area          # 仅位置变化，原地更新即可
            return False
        try:
            self._rotate_segment()
        except Exception:  # noqa: BLE001
            return False
        return True

    def _rotate_segment(self):
        """结束当前分段并立刻用新尺寸开一个新分段。"""
        exe = find_ffmpeg()
        with self._pipe_lock:
            old = self.proc
            self.proc = None          # 让 _loop 知道管道正在更换
            try:
                old.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                old.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            base, ext = os.path.splitext(self.out_path)
            seg = "%s_part%02d%s" % (base, len(self.segments) + 1, ext or ".mp4")
            self.segments.append(self.out_path)
            self.out_path = seg
            self._spawn(exe, self.area, seg, self.draw_cursor)

    def stop(self):
        if not self.recording:
            return
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.recording = False

    def _emit(self, kind, *a):
        try:
            self.on_event(kind, *a)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _loop(self, area, draw_cursor):
        try:
            with _mkss() as sct:
                next_t = time.perf_counter()
                interval = 1.0 / max(1, self.fps)
                while not self.stop_flag.is_set():
                    t0 = time.perf_counter()
                    # 每帧重新读 self.area：窗口跟随时位置可能已变
                    mon = dict(self.area)
                    proc = self.proc
                    if proc is None or proc.stdin is None:
                        break
                    try:
                        shot = sct.grab(mon)
                    except Exception:  # noqa: BLE001  取景瞬间失效（窗口最小化等）
                        if self.stop_flag.wait(0.2):
                            break
                        continue
                    raw = shot.raw
                    if draw_cursor and HAVE_PIL:
                        raw = draw_cursor_over(raw, shot.width, shot.height, mon)
                    try:
                        proc.stdin.write(raw)
                    except (ValueError, OSError):
                        # 分段轮换时旧管道已被关闭，重新取一次当前管道
                        proc = self.proc
                        if proc is None or proc.stdin is None or proc.stdin.closed:
                            break
                        try:
                            proc.stdin.write(raw)
                        except (ValueError, OSError):
                            break
                    self.frames += 1
                    next_t += interval
                    delay = next_t - time.perf_counter()
                    if delay < -1.0:
                        next_t = time.perf_counter()
                        delay = 0
                    self._emit("tick", self.frames, self.elapsed)
                    if delay > 0:
                        self.stop_flag.wait(delay)
            # 正常收尾：关闭 stdin 让 ffmpeg 写完 moov
            with self._pipe_lock:
                final_proc = self.proc
                self.proc = None
            if final_proc is not None:
                try:
                    final_proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
                rc = final_proc.wait(timeout=30)
            else:
                rc = 0
            final = self.out_path
            if self.segments:
                final = self._concat_segments()
            self._emit("done", rc, final)
        except Exception:  # noqa: BLE001
            self._kill()
            self._emit("error", traceback.format_exc(limit=4))

    def _concat_segments(self):
        """把分段文件拼成一个 mp4，返回最终路径。"""
        parts = [p for p in self.segments + [self.out_path] if p and os.path.exists(p)]
        if len(parts) <= 1:
            return parts[0] if parts else self.out_path
        exe = find_ffmpeg()
        lst = self.out_path + ".concat.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for p in parts:
                f.write("file '%s'\n" % os.path.abspath(p).replace("'", "'\\''"))
        final = self.segments[0]     # 合并结果写回第一个文件名
        tmp = final + ".merged.mp4"
        cmd = [exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", lst, "-c", "copy", tmp]
        try:
            subprocess.run(cmd, check=True, creationflags=CREATE_NO_WINDOW,
                           timeout=300)
            os.replace(tmp, final)
        except Exception:  # noqa: BLE001
            self._emit("warn", "分段合并失败，已保留 %d 个分段文件" % len(parts))
            return parts[0]
        finally:
            try:
                os.remove(lst)
            except Exception:  # noqa: BLE001
                pass
            for p in parts[1:]:
                try:
                    os.remove(p)
                except Exception:  # noqa: BLE001
                    pass
        return final

    def _kill(self):
        self.recording = False
        try:
            with self._pipe_lock:
                p = self.proc
                self.proc = None
            if p:
                p.kill()
        except Exception:  # noqa: BLE001
            pass


def draw_cursor_over(raw: bytes, w: int, h: int, area) -> bytes:
    """在抓到的画面上画一个鼠标指针（mss 不含光标）"""
    cx, cy = cursor_pos()
    lx, ly = cx - area["left"], cy - area["top"]
    if not (0 <= lx < w and 0 <= ly < h):
        return raw
    img = Image.frombytes("RGBA", (w, h), raw)
    d = ImageDraw.Draw(img)
    x, y = lx, ly
    d.polygon([(x, y), (x, y + 22), (x + 5.5, y + 17), (x + 9, y + 26),
               (x + 13, y + 24), (x + 9.5, y + 15), (x + 16, y + 14)],
              fill=(255, 255, 255, 255), outline=(0, 0, 0, 200))
    return img.tobytes()


def detect_content_rect(raw, mon):
    """从一帧画面里找出真实内容区（裁掉黑边 / 纯色边框）。

    raw: mss 抓到的 BGRA 帧；mon: mct.monitors[1] 显示器字典。
    返回 (left, top, width, height)（屏幕绝对坐标），识别失败返回 None。

    纯 Pillow 实现，刻意不引入 numpy 依赖：把图缩到 1/8 采样后按行/列
    统计「亮度足够」的像素占比，找出内容边界。
    """
    if raw is None or not HAVE_PIL:
        return None
    img = Image.frombytes("RGB", raw.size, raw.rgb)
    k = 8
    small = img.convert("L").resize((max(1, img.width // k),
                                    max(1, img.height // k)),
                                   Image.BILINEAR)
    w, h = small.size
    px = small.load()
    row_hits = []
    for y in range(h):
        n = 0
        for x in range(0, w, 2):
            if px[x, y] > 12:
                n += 1
        row_hits.append(n)
    col_hits = [0] * w
    for y in range(0, h, 2):
        for x in range(w):
            if px[x, y] > 12:
                col_hits[x] += 1
    thr_x = max(1, (w // 2) * 0.02)
    thr_y = max(1, (h // 2) * 0.02)
    rows = [i for i, v in enumerate(row_hits) if v > thr_x]
    cols = [i for i, v in enumerate(col_hits) if v > thr_y]
    if len(rows) < 3 or len(cols) < 3:
        return None
    pad = 4
    top = max(0, rows[0] * k - pad)
    left = max(0, cols[0] * k - pad)
    bottom = min(mon["height"], (rows[-1] + 1) * k + pad)
    right = min(mon["width"], (cols[-1] + 1) * k + pad)
    if right - left < 64 or bottom - top < 64:
        return None
    return (mon["left"] + left, mon["top"] + top, right - left, bottom - top)


def list_audio_devices(exe: str):
    """用 ffmpeg -list_devices 列出 dshow 音频设备名
    输出形如  "麦克风阵列 (Realtek Audio)" (audio)  —— 只取 (audio) 那一类"""
    if not exe:
        return []
    try:
        p = subprocess.run([exe, "-hide_banner", "-list_devices", "true",
                            "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, errors="ignore",
                           timeout=20, creationflags=CREATE_NO_WINDOW)
    except Exception:  # noqa: BLE001
        return []
    text = (p.stderr or "") + (p.stdout or "")
    out = []
    for m in re.finditer(r'"([^"]+)"\s*\((audio|video)\)', text, re.I):
        if m.group(2).lower() == "audio":
            name = m.group(1)
            if name not in out:
                out.append(name)
    return out


# ============================================================ 区域选择器

class RegionSelector(tk.Toplevel):
    """全屏半透明遮罩，拖拽框选区域"""

    def __init__(self, master, on_done, hint="拖拽框选要录制的区域 · Esc 取消"):
        super().__init__(master)
        self.on_done = on_done
        x, y, w, h = virtual_screen()
        self.vx, self.vy, self.vw, self.vh = x, y, w, h
        self.overrideredirect(True)
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.attributes("-topmost", True)
        self.configure(bg="#000000", cursor="cross")
        try:
            self.attributes("-alpha", 0.35)
        except Exception:  # noqa: BLE001
            pass
        self.canvas = tk.Canvas(self, bg="#000000", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(w / 2, px(40), fill="#FFFFFF",
                                text=hint, font=("Microsoft YaHei UI", -px(13), "bold"))
        self.start = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda _e: self._cancel())
        self.after(80, self._snapshot)

    def _snapshot(self):
        """底图：抓一张当前屏幕铺在底下，选区看起来更直观"""
        try:
            with _mkss() as sct:
                shot = sct.grab({"left": self.vx, "top": self.vy,
                                 "width": self.vw, "height": self.vh})
                img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
                img = img.filter(ImageFilter.GaussianBlur(2))
                self._photo = ImageTk.PhotoImage(img)
                self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
                self.canvas.tag_lower("all")
        except Exception:  # noqa: BLE001
            pass

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                 outline="#FF3B30", width=px(2))

    def _motion(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        self.canvas.coords(self.rect, x0, y0, e.x, e.y)
        self.canvas.itemconfig(
            self.rect,
            fill="#FF3B30")
        w, h = abs(e.x - x0), abs(e.y - y0)
        self.canvas.create_text(x0, y0, text="", tags="size")
        self.canvas.delete("size")
        self.canvas.create_text(min(max(x0, px(40)), self.vw - px(60)),
                                min(max(y0 - px(18), px(24)), self.vh - px(20)),
                                text="%d × %d" % (w, h), fill="#FFFFFF",
                                font=("Consolas", -px(11), "bold"), tags="size")

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        l, t = min(x0, e.x), min(y0, e.y)
        r, b = max(x0, e.x), max(y0, e.y)
        if r - l < 8 or b - t < 8:
            self._cancel()
            return
        self.on_done((self.vx + l, self.vy + t, r - l, b - t))
        self.destroy()

    def _cancel(self):
        self.on_done(None)
        self.destroy()


# ============================================================ GUI

class RecorderApp(AppBase):
    APP_NAME = APP_NAME
    APP_VERSION = APP_VERSION
    APP_SUB = "全屏 / 区域 / 窗口录制"
    PAGES = [("main", "录制控制"), ("settings", "录制设置"), ("about", "关于")]

    def __init__(self, root):
        self.rec = Recorder()
        self.rec.on_event = self._on_engine
        # 变量
        self.out_dir = tk.StringVar(value=self._default_dir())
        self.region = tk.StringVar(value="全屏")
        self.region_val = (0, 0, 0, 0)      # 0,0,0,0 = 全屏
        self.region_mode = "full"           # full / pick / window / auto
        self._windows = []
        self.follow = False                 # 录制中跟随窗口移动
        self.fps = tk.StringVar(value="30")
        self.crf = tk.IntVar(value=1)
        self.cursor_var = tk.BooleanVar(value=True)
        self.audio_var = tk.StringVar(value="不录音")
        self.minimize_var = tk.BooleanVar(value=True)
        self.countdown_var = tk.IntVar(value=3)
        self.hotkey_var = tk.StringVar(value="F9")
        self.state_var = tk.StringVar(value="就绪")
        self.time_var = tk.StringVar(value="0:00:00")
        self.size_var = tk.StringVar(value="0.0 MB")
        self.fpsreal_var = tk.StringVar(value="0.0")
        self.res_var = tk.StringVar(value="-")
        self._hotkey_down = False
        self._audio_devices = []
        super().__init__(root, size=(960, 720), minsize=(900, 660))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    @staticmethod
    def _default_dir():
        for p in (os.path.join(os.path.expanduser("~"), "Videos", "录屏"),
                  os.path.join(os.path.expanduser("~"), "Desktop", "录屏"),
                  os.path.expanduser("~")):
            if os.path.isdir(os.path.dirname(p)):
                return p
        return os.path.expanduser("~")

    # -------------------------------------------------- 页面
    def _build_pages(self):
        self._page_main()
        self._page_settings()
        self._page_about()

    def _page_main(self):
        p = self.pages["main"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()

        box = self._card(p, pad=20)
        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x")
        self.state_pill = Pill(row, "● 就绪", THEME["text3"], bg=THEME["card"], size=12)
        self.state_pill.pack(side="left")
        tk.Label(row, text="停止热键", bg=THEME["card"], fg=THEME["text3"],
                 font=F(10)).pack(side="right", padx=(px(8), px(6)))
        self.hk_pill = Pill(row, "F9", THEME["red"], bg=THEME["card"], size=11)
        self.hk_pill.pack(side="right")

        # 计时 + 大屏数字
        big = tk.Frame(box, bg=THEME["soft"])
        big.pack(fill="x", pady=(px(14), px(0)))
        c1 = tk.Frame(big, bg=THEME["soft"])
        c1.pack(side="left", fill="both", expand=True)
        tk.Label(c1, text="录制时长", bg=THEME["soft"], fg=THEME["text3"],
                 font=F(9)).pack(anchor="w")
        tk.Label(c1, textvariable=self.time_var, bg=THEME["soft"], fg=THEME["text"],
                 font=F(30, "bold", mono=True)).pack(anchor="w")
        for name, var, unit in (("文件大小", self.size_var, ""),
                                ("实际帧率", self.fpsreal_var, "fps"),
                                ("分辨率", self.res_var, "")):
            c2 = tk.Frame(big, bg=THEME["soft"])
            c2.pack(side="left", fill="x", expand=True, padx=(px(10), 0))
            tk.Label(c2, text=name, bg=THEME["soft"], fg=THEME["text3"],
                     font=F(9)).pack(anchor="w")
            tk.Label(c2, textvariable=var, bg=THEME["soft"], fg=THEME["text"],
                     font=F(15, "bold", mono=True)).pack(anchor="w")
            if unit:
                tk.Label(c2, text=unit, bg=THEME["soft"], fg=THEME["text3"],
                         font=F(9)).pack(anchor="w")

        # 主按钮
        acts = tk.Frame(p, bg=THEME["window"])
        acts.pack(fill="x", padx=px(28), pady=(px(0), px(12)))
        self.btn_rec = AppleButton(acts, "●  开始录制", command=self.toggle_record,
                                   style="danger", width=px(300), height=px(58),
                                   radius=px(16), font=F(16), fill=True)
        self.btn_rec.pack(side="left", fill="x", expand=True)
        self.btn_pick = AppleButton(acts, "框选区域", command=self.pick_region,
                                    style="secondary", width=px(300), height=px(58),
                                    radius=px(16), font=F(16), fill=True)
        self.btn_pick.pack(side="left", fill="x", expand=True, padx=(px(12), 0))

        # 输出
        box2 = self._card(p, "输出")
        r = tk.Frame(box2, bg=THEME["card"])
        r.pack(fill="x", pady=(px(0), px(10)))
        e = tk.Entry(r, textvariable=self.out_dir, font=F(10), relief="solid", bd=1,
                     highlightthickness=1, highlightcolor=THEME["blue"],
                     highlightbackground=THEME["border"])
        e.pack(side="left", fill="x", expand=True, ipady=px(4))
        AppleButton(r, "更改…", command=self.choose_dir, style="secondary",
                    width=px(84), height=px(30), radius=px(9),
                    font=F(10)).pack(side="left", padx=px(8))
        AppleButton(r, "打开目录", command=lambda: open_uri(self.out_dir.get()),
                    style="secondary", width=px(96), height=px(30), radius=px(9),
                    font=F(10)).pack(side="left")
        tk.Label(box2,
                 text="文件名自动生成为 录制_年月日_时分秒.mp4（H.264，MP4 容器，"
                      "可直接上传、微信/QQ 发送）。",
                 bg=THEME["card"], fg=THEME["text3"], font=F(9),
                 wraplength=px(620), justify="left").pack(anchor="w")

    def _page_settings(self):
        p = self.pages["settings"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()

        box = self._card(p, "画面")
        r1 = tk.Frame(box, bg=THEME["card"])
        r1.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r1, text="录制区域", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(0), px(10)))
        # fill=True：Canvas 类控件必须跟随容器宽度，否则第 4 个选项被裁掉
        self.region_seg = SegmentedControl(
            r1, ["全屏", "框选区域", "应用窗口", "自动识别"],
            command=self._on_region, width=px(400),
            height=px(34), bg=THEME["card"], fill=True)
        self.region_seg.pack(side="left", fill="x", expand=True)
        self.region_seg.select(0)
        # 说明文字换行到下一行，避免和分段控件抢宽度
        self.region_lbl = tk.Label(box, text="全屏（含所有显示器）",
                                   bg=THEME["card"], fg=THEME["text3"],
                                   font=F(10), anchor="w", justify="left")
        self.region_lbl.pack(fill="x", pady=(px(0), px(10)))

        # 操作按钮（随模式切换启用/禁用）
        # 用 grid 均分四列：pack(expand) 对 Canvas 按钮不生效，
        # 会按构造宽度绘制导致右侧按钮被挤出容器。
        r1b = tk.Frame(box, bg=THEME["card"])
        r1b.pack(fill="x", pady=(px(0), px(10)))
        r1b.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="btns")
        _mk = lambda parent, **kw: AppleButton(parent, bg=THEME["card"], **kw)
        self.btn_pick2 = _mk(r1b, text="框选区域", command=self.pick_region,
                             style="secondary", height=px(32), radius=px(9),
                             font=F(10), fill=True)
        self.btn_detect = _mk(r1b, text="识别内容区", command=self.detect_content,
                              style="secondary", height=px(32), radius=px(9),
                              font=F(10), fill=True)
        self.btn_refresh_win = _mk(r1b, text="刷新窗口", command=self.refresh_windows,
                                   style="secondary", height=px(32), radius=px(9),
                                   font=F(10), fill=True)
        self.btn_follow = _mk(r1b, text="跟随移动", command=self.toggle_follow,
                              style="secondary", height=px(32), radius=px(9),
                              font=F(10), fill=True)
        for i, b in enumerate((self.btn_pick2, self.btn_detect,
                               self.btn_refresh_win, self.btn_follow)):
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(8), 0))

        # 应用窗口选择
        r1c = tk.Frame(box, bg=THEME["card"])
        r1c.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r1c, text="目标窗口", bg=THEME["card"], fg=THEME["text3"],
                 font=F(9)).pack(side="left", padx=(px(0), px(8)))
        self.win_combo = ttk.Combobox(r1c, values=["（未选择，请先刷新窗口）"],
                                      state="readonly", style="Apple.Combo",
                                      font=F(10))
        self.win_combo.pack(side="left", fill="x", expand=True,
                            padx=(0, px(12)))
        tk.Label(r1c, text="录制时若窗口移动或缩放，区域会自动跟随",
                 bg=THEME["card"], fg=THEME["text3"], font=F(9)).pack(side="left")

        r2 = tk.Frame(box, bg=THEME["card"])
        r2.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r2, text="帧率", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        self.fps_seg = SegmentedControl(r2, ["24", "30", "60"], width=px(220),
                                        height=px(34), bg=THEME["card"],
                                        command=lambda i: self.fps.set(
                                            ["24", "30", "60"][i]))
        self.fps_seg.pack(side="left", padx=px(10))
        self.fps_seg.select(1)
        tk.Label(r2, text="帧率越高越流畅，体积也越大", bg=THEME["card"],
                 fg=THEME["text3"], font=F(9)).pack(side="left")

        r3 = tk.Frame(box, bg=THEME["card"])
        r3.pack(fill="x")
        tk.Label(r3, text="画质", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        self.crf_seg = SegmentedControl(r3, ["高（快编码）", "中（推荐）", "高（慢编码）"],
                                        width=px(300), height=px(34),
                                        bg=THEME["card"],
                                        command=lambda i: self.crf.set(i))
        self.crf_seg.pack(side="left", padx=px(10))
        self.crf_seg.select(1)
        ToggleSwitch(r3, variable=self.cursor_var, bg=THEME["card"]).pack(
            side="left", padx=px(16))
        tk.Label(r3, text="在画面里显示鼠标指针", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(6), 0))

        # 音频
        box2 = self._card(p, "声音与启动")
        r4 = tk.Frame(box2, bg=THEME["card"])
        r4.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r4, text="录音设备", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(0), px(10)))
        self.audio_combo = ttk.Combobox(r4, textvariable=self.audio_var,
                                        values=["不录音"], width=30,
                                        state="readonly", style="Apple.Combo",
                                        font=F(10))
        self.audio_combo.pack(side="left", ipady=px(3))
        AppleButton(r4, "刷新设备", command=self.refresh_audio, style="secondary",
                    width=px(96), height=px(32), radius=px(9),
                    font=F(10)).pack(side="left", padx=px(8))
        tk.Label(box2,
                 text="录系统声音：Windows「设置 → 隐私和安全性 → 声音 → 允许桌面应用"
                      "播放音频」后，这里会出现 虚拟音频 / 立体声混音 设备。",
                 bg=THEME["card"], fg=THEME["text3"], font=F(9),
                 wraplength=px(620), justify="left").pack(anchor="w",
                                                           pady=(px(0), px(10)))

        r5 = tk.Frame(box2, bg=THEME["card"])
        r5.pack(fill="x", pady=(px(0), px(10)))
        tk.Label(r5, text="开始倒计时(秒)", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left")
        tk.Spinbox(r5, from_=0, to=10, width=6, textvariable=self.countdown_var,
                   font=F(11), relief="solid", bd=1, highlightthickness=1,
                   highlightbackground=THEME["border"]).pack(side="left", padx=px(10))
        tk.Label(r5, text="给你时间切到要录的窗口", bg=THEME["card"],
                 fg=THEME["text3"], font=F(9)).pack(side="left")

        r6 = tk.Frame(box2, bg=THEME["card"])
        r6.pack(fill="x")
        ToggleSwitch(r6, variable=self.minimize_var, bg=THEME["card"]).pack(side="left")
        tk.Label(r6, text="开始录制后自动最小化窗口（避免录到界面本身）",
                 bg=THEME["card"], fg=THEME["text"], font=F(11)).pack(
            side="left", padx=(px(6), px(16)))
        tk.Label(r6, text="停止热键", bg=THEME["card"], fg=THEME["text"],
                 font=F(11)).pack(side="left", padx=(px(0), px(8)))
        hk = ttk.Combobox(r6, textvariable=self.hotkey_var,
                          values=["F%d" % i for i in range(1, 13)], width=6,
                          state="readonly", style="Apple.Combo", font=F(10))
        hk.pack(side="left")
        hk.bind("<<ComboboxSelected>>",
                lambda _e: self.hk_pill.set(self.hotkey_var.get(), THEME["red"]))

    def _page_about(self):
        p = self.pages["about"].body
        tk.Frame(p, bg=THEME["window"], height=px(4)).pack()
        box = self._card(p, "关于")
        tk.Label(box,
                 text="%s v%s · MIT 开源协议\n\n"
                      "原理：MSS（GDI BitBlt 多线程抓屏）逐帧送入内置 ffmpeg，"
                      "编码为 H.264 / MP4。全程本地进行，不联网上传。\n"
                      "录制是「实时抓屏」，不是系统级hook，因此无法录到受保护内容"
                      "（DRM 视频播放时会黑屏/空白），这是所有同类工具的共同限制。\n\n"
                      "内置 ffmpeg 采用 LGPL/GPL 许可的静态构建，仅作为独立进程调用，"
                      "未修改其代码。%s" % (APP_NAME, APP_VERSION,
                                            "" if HAVE_MSS else "\n⚠ 缺少 mss 模块"),
                 bg=THEME["card"], fg=THEME["text2"], font=F(10),
                 justify="left", wraplength=px(620)).pack(anchor="w")
        row = tk.Frame(box, bg=THEME["card"])
        row.pack(fill="x", pady=(px(14), px(0)))
        AppleButton(row, "打开项目主页", command=lambda: open_uri(
            "https://github.com/xavier111222/ScreenRecorder"), style="secondary",
            width=px(150), height=px(34), font=F(10)).pack(side="left")
        AppleButton(row, "系统自检", command=self.selftest_gui, style="secondary",
                    width=px(120), height=px(34), font=F(10)).pack(side="left",
                                                                   padx=px(10))

    # -------------------------------------------------- 交互
    def _on_region(self, i):
        self.region_seg.select(i)
        self.region.set(self.region_seg.options[i])
        self.region_mode = ("full", "pick", "window", "auto")[i]
        # 按钮启用状态
        self.btn_pick2.set_enabled(self.region_mode == "pick")
        self.btn_detect.set_enabled(self.region_mode == "auto")
        self.btn_refresh_win.set_enabled(self.region_mode == "window")
        self.btn_follow.set_enabled(self.region_mode == "window")
        if self.region_mode == "full":
            self.region_val = (0, 0, 0, 0)
            self.follow = False
            self._update_region_label()
        elif self.region_mode == "pick":
            if not self.region_val[2]:
                self.log("点「框选区域」在屏幕上拖拽选择。", "warn")
            self._update_region_label()
        elif self.region_mode == "window":
            if not self._windows:
                self.refresh_windows()
            self._update_region_label()
        else:
            if not self.region_val[2]:
                self.log("点「识别内容区」自动去掉黑边 / 纯色边框。", "warn")
            self._update_region_label()

    def _current_window(self):
        try:
            idx = self.win_combo.current()
        except Exception:  # noqa: BLE001
            return None
        # 下拉第 0 项是「（未选择…）」占位；current() 为 -1 表示未选中
        if idx is None or idx <= 0 or idx - 1 >= len(self._windows):
            return None
        return self._windows[idx - 1]

    def _update_region_label(self):
        l, t, w, h = self.region_val
        mode = getattr(self, "region_mode", "full")
        if mode == "window":
            win = self._current_window()
            if win is None:
                self.region_lbl.configure(text="未选择窗口")
            elif w:
                self.region_lbl.configure(
                    text="跟随「%s」 %d×%d" % ((win.title or "")[:22], w, h))
            else:
                self.region_lbl.configure(
                    text="已选「%s」%s" % ((win.title or "")[:26],
                                          "（最小化）" if win.minimized else ""))
        elif w:
            self.region_lbl.configure(text="区域 %d, %d · %d×%d" % (l, t, w, h))
        elif mode == "auto":
            self.region_lbl.configure(text="等待识别内容区")
        else:
            self.region_lbl.configure(text="全屏（含所有显示器）")

    # -- 窗口列表
    def refresh_windows(self):
        self.run_async(self._refresh_windows_bg)

    def _refresh_windows_bg(self):
        wins = win_input.list_windows(min_w=200, min_h=140)
        self.ui(self._set_windows, wins)

    def _set_windows(self, wins):
        self._windows = wins
        self.win_combo["values"] = (["（未选择，请先刷新窗口）"] +
                                    ["%s  —  %s" % (w.title[:46], w.exe or "?")
                                     for w in wins])
        self.win_combo.current(0)
        self.log("已刷新窗口列表：%d 个可用" % len(wins), "ok")
        self._update_region_label()

    def toggle_follow(self):
        self.follow = not self.follow
        self.btn_follow.set_text("跟随移动 ●" if self.follow else "跟随移动")
        self.log("窗口跟随：%s" % ("开启（录制中窗口移动会自动调整取景）"
                                  if self.follow else "关闭"), "ok")

    def _sync_window_region(self):
        """录制中实时读取目标窗口位置，实现跟随。"""
        if getattr(self, "region_mode", "full") != "window" or not self.follow:
            return
        win = self._current_window()
        if win is None or not win_input.is_window(win.hwnd):
            return
        l, t, r, b = win_input.window_rect(win.hwnd)
        w, h = r - l, b - t
        if w > 0 and h > 0 and (l, t, w, h) != self.region_val:
            self.region_val = (l, t, w, h)
            self._update_region_label()

    # -- 自动识别内容区
    def detect_content(self):
        if self.rec.recording:
            messagebox.showinfo("录制中", "请先停止录制再识别内容区。")
            return
        self.run_async(self._detect_content_bg)

    def _detect_content_bg(self):
        """抓一帧当前屏幕，找出真实内容区（去掉黑边 / 纯色边框）。"""
        with _mkss() as sct:
            mon = sct.monitors[1]          # 主显示器
            raw = sct.grab(mon)
        rect = detect_content_rect(raw, mon)
        if rect is None:
            self.ui(self._set_region_mode, 0)
            return
        self.ui(self._apply_detected, rect, (mon["width"], mon["height"]))

    def _apply_detected(self, rect, full):
        self.region_val = rect
        self.region_seg.select(3)
        self.region.set("自动识别")
        self.region_mode = "auto"
        self._update_region_label()
        self.log("已识别内容区：%d, %d · %d×%d（全屏 %d×%d，已裁掉 %d%% 边缘）"
                 % (rect[0], rect[1], rect[2], rect[3], full[0], full[1],
                    100 - rect[2] * 100 // max(1, full[0])), "ok")

    def _set_region_mode(self, i):
        self._on_region(i)

    def pick_region(self):
        if self.rec.recording:
            messagebox.showinfo("录制中", "请先停止录制再框选区域。")
            return
        self.log("请在屏幕上拖拽框选录制区域（Esc 取消）")

        def done(rect):
            if rect:
                self.region_val = rect
                self.region_seg.select(1)
                self.region.set("框选区域")
                self._update_region_label()
                self.log("已选择区域：%d, %d · %d×%d" % rect, "ok")
            else:
                self.log("已取消框选。")
        RegionSelector(self.root, done)

    def choose_dir(self):
        d = filedialog.askdirectory(title="选择输出目录",
                                    initialdir=self.out_dir.get())
        if d:
            self.out_dir.set(d)

    def refresh_audio(self):
        exe = find_ffmpeg()
        self._audio_devices = list_audio_devices(exe)
        vals = ["不录音"] + self._audio_devices
        self.audio_combo.configure(values=vals)
        self.audio_var.set("不录音")
        self.log("检测到 %d 个录音设备。" % len(self._audio_devices),
                 "ok" if self._audio_devices else "warn")

    def _target_region(self):
        # 应用窗口模式：每次都取窗口的实时矩形
        if getattr(self, "region_mode", "full") == "window":
            win = self._current_window()
            if win is not None and not win.minimized:
                l, t, r, b = win_input.window_rect(win.hwnd)
                if r - l > 4 and b - t > 4:
                    return (l, t, r - l, b - t)
        if self.region_val[2]:
            return self.region_val
        x, y, w, h = virtual_screen()
        return (x, y, w, h)

    def _poll_follow(self):
        """录制中每 500ms 检查一次窗口位置，实现跟随。"""
        if self.rec.recording and self.follow and \
                getattr(self, "region_mode", "full") == "window":
            win = self._current_window()
            if win is None:
                self.follow = False
                self.btn_follow.set_text("跟随移动")
                self.log("目标窗口已关闭，已停止跟随。", "warn")
            elif not win_input.is_window(win.hwnd):
                self.follow = False
                self.btn_follow.set_text("跟随移动")
                self.log("目标窗口已关闭，已停止跟随。", "warn")
            else:
                l, t, r, b = win_input.window_rect(win.hwnd)
                if r - l > 4 and b - t > 4:
                    new = (l, t, r - l, b - t)
                    if new != self.region_val:
                        self.region_val = new
                        rotated = self.rec.resize_to(new)
                        self.res_var.set("%d×%d" % (new[2], new[3]))
                        self._update_region_label()
                        if rotated:
                            self.log("窗口尺寸变化，已自动续录（%d×%d）"
                                     % (new[2], new[3]), "ok")
        self.root.after(500, self._poll_follow)

    def toggle_record(self):
        if self.rec.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if not HAVE_MSS:
            messagebox.showerror("缺少组件", "未找到 mss 模块，无法抓屏。")
            return
        d = self.out_dir.get().strip() or self._default_dir()
        os.makedirs(d, exist_ok=True)
        name = "录制_%s.mp4" % datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(d, name)
        reg = self._target_region()
        audio = "" if self.audio_var.get() == "不录音" else self.audio_var.get()
        try:
            self.rec.start(reg, int(self.fps.get()), self.crf.get(), out,
                           self.cursor_var.get(), audio)
        except Exception as e:  # noqa: BLE001
            self.log("启动失败：%s" % e, "error")
            messagebox.showerror("无法开始录制", str(e))
            return
        self.state_pill.set("● 录制中", THEME["red"])
        self.size_var.set("0.0 MB")
        self.fpsreal_var.set("0.0")
        self.res_var.set("%d×%d" % (reg[2], reg[3]))
        self.btn_rec.set_text("■  停止录制")
        self.log("开始录制 → %s（%d×%d @%s fps%s）"
                 % (out, reg[2], reg[3], self.fps.get(),
                    "，录音：%s" % audio if audio else ""), "ok")
        cd = max(0, int(self.countdown_var.get() or 0))
        if cd:
            self._after_countdown(cd, self._do_minimize)
        else:
            self._do_minimize()

    def _do_minimize(self):
        if self.minimize_var.get():
            self.root.iconify()

    def _after_countdown(self, n, then):
        if n <= 0:
            then()
            return
        self.state_pill.set("● %d 秒后开始" % n, THEME["orange"])
        self.root.after(1000, lambda: self._after_countdown(n - 1, then))

    def stop_recording(self):
        if not self.rec.recording:
            return
        self.log("正在停止并写入文件 …")
        self.rec.stop()
        self._reset_ui()
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
        except Exception:  # noqa: BLE001
            pass

    def _reset_ui(self):
        self.btn_rec.set_text("●  开始录制")
        self.state_pill.set("● 就绪", THEME["text3"])
        self.fpsreal_var.set("0.0")

    def _on_engine(self, kind, *a):
        if kind == "tick":
            frames, elapsed = a
            self.ui(self.time_var.set, fmt_dur(elapsed))
            self.ui(self.fpsreal_var.set, "%.1f" % (frames / max(elapsed, 0.001)))
            try:
                self.ui(self.size_var.set, "%.1f MB" % (
                    os.path.getsize(self.rec.out_path) / 1048576))
            except Exception:  # noqa: BLE001
                pass
        elif kind == "done":
            rc, path = a
            if rc == 0:
                mb = os.path.getsize(path) / 1048576 if os.path.isfile(path) else 0
                self.ui(self.log, "录制完成：%s（%.1f MB）" % (path, mb), "ok")
            else:
                self.ui(self.log, "ffmpeg 退出码 %s，请检查输出目录权限。" % rc,
                        "error")
            self.ui(self._reset_ui)
        elif kind == "error":
            self.ui(self.log, "录制出错: " + a[0], "error")
            self.ui(self._reset_ui)

    def on_ready(self):
        self.root.after(30, self._poll_hotkey)
        self.root.after(500, self._poll_follow)
        exe = find_ffmpeg()
        self.log("%s v%s 已启动" % (APP_NAME, APP_VERSION), "ok")
        # 只显示文件名：完整路径很长，会把日志区撑出横向滚动条
        shown = os.path.basename(exe) if exe else "未找到！录制将无法启动"
        self.log("ffmpeg：%s" % shown, "ok" if exe else "error")
        if not exe:
            self.log("请把 ffmpeg.exe 放到程序同目录，或安装 ffmpeg 并加入 PATH。", "error")

    def _poll_hotkey(self):
        vk = {"F%d" % i: 0x70 + i for i in range(1, 13)}.get(self.hotkey_var.get())
        try:
            if vk:
                down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
                if down and not self._hotkey_down:
                    self.toggle_record()
                self._hotkey_down = down
        except Exception:  # noqa: BLE001
            pass
        self.root.after(30, self._poll_hotkey)

    def selftest_gui(self):
        messagebox.showinfo("系统自检", selftest())

    def on_close(self):
        if self.rec.recording:
            if not messagebox.askyesno("退出", "正在录制中，退出将结束并保存当前文件，继续？"):
                return
            self.rec.stop()
        self.root.destroy()


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


# ============================================================ CLI

def selftest() -> str:
    x, y, w, h = virtual_screen()
    exe = find_ffmpeg()
    lines = [
        "虚拟屏幕：%d, %d · %dx%d" % (x, y, w, h),
        "ffmpeg：%s" % (exe or "未找到（录制将失败）"),
        "mss 模块：%s" % ("OK" if HAVE_MSS else "缺失"),
        "Pillow：%s" % ("OK" if HAVE_PIL else "缺失（无鼠标指针）"),
        "DPI 感知：%s" % setup_dpi(),
    ]
    if HAVE_MSS:
        try:
            with _mkss() as sct:
                t0 = time.perf_counter()
                shot = sct.grab({"left": 0, "top": 0, "width": 320, "height": 200})
                lines.append("抓屏测试：%d×%d，用时 %.0f ms"
                             % (shot.width, shot.height,
                                (time.perf_counter() - t0) * 1000))
        except Exception as e:  # noqa: BLE001
            lines.append("抓屏失败：%s" % e)
    if exe:
        devs = list_audio_devices(exe)
        lines.append("录音设备：%s" % ("、".join(devs) if devs else "未检测到"))
    # 区域识别 + 应用窗口录制自检
    try:
        import win_input
        wins = win_input.list_windows()
        lines.append("窗口枚举：%d 个可用窗口（应用窗口录制依赖此项）" % len(wins))
        for w in wins[:3]:
            lines.append("    - %s | %s | %d,%d %dx%d"
                         % (w.title[:32], w.exe, w.left, w.top,
                            w.right - w.left, w.bottom - w.top))
    except Exception as e:  # noqa: BLE001
        lines.append("窗口枚举失败：%s" % e)
    if HAVE_PIL and HAVE_MSS:
        try:
            with _mkss() as sct:
                mon = sct.monitors[1]
                raw = sct.grab(mon)
            box = detect_content_rect(raw, mon)
            if box:
                lines.append("区域识别：内容边界 %d, %d · %d×%d"
                             % (box[0], box[1], box[2], box[3]))
            else:
                lines.append("区域识别：未找到明确边界（画面接近纯色属正常）")
        except Exception as e:  # noqa: BLE001
            lines.append("区域识别失败：%s" % e)
    return "\n".join(lines)


def cli_main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog=APP_NAME, description="屏幕录制器")
    ap.add_argument("--version", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--devices", action="store_true", help="列出录音设备")
    ap.add_argument("--record", type=float, metavar="SEC", default=0,
                    help="端到端录制自检：真录 N 秒并校验产物")
    a = ap.parse_args(argv)
    if a.version:
        print("%s v%s" % (APP_NAME, APP_VERSION))
        return 0
    if a.devices:
        for d in list_audio_devices(find_ffmpeg()):
            _safe_print(d)
        return 0
    if a.record:
        import tempfile
        sec = max(1.0, min(a.record, 30.0))
        out = os.path.join(tempfile.mkdtemp(prefix="screc_"),
                           "smoke_%.0fs.mp4" % sec)
        r = Recorder()
        r.start((0, 0, 320, 240), 24, 2, out, True, "")
        time.sleep(sec)
        r.stop()
        ok = os.path.isfile(out) and os.path.getsize(out) > 2000
        print("录制自检：%s（%d 字节）-> %s"
              % (out, os.path.getsize(out) if os.path.isfile(out) else 0,
                 "OK" if ok else "失败"))
        return 0 if ok else 1
    if a.selftest:
        _safe_print(selftest())
        return 0
    return None


def _safe_print(s):
    """打印可能被 GBK 控制台拒绝的文本。

    坑：窗口标题里可能含零宽字符（U+200B 等），中文 Windows 控制台
    默认编码 GBK，遇到这些字符 print 直接抛 UnicodeEncodeError，
    整个 --selftest 崩掉。降级成 replace 后不可见字符变问号，不影响诊断。
    """
    try:
        print(s)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "gbk"
        print(s.encode(enc, "replace").decode(enc, "replace"))


def main():
    argv = sys.argv[1:]
    if argv and argv[0].startswith("-"):
        rc = cli_main(argv)
        if rc is not None:
            return rc
    setup_dpi()
    root = tk.Tk()
    import ui_kit
    ui_kit.init_ui_scale(root)      # 传 root 才能拿到该显示器真实 DPI
    ui_kit.apply_tk_scaling(root)
    ui_kit.apply_base_fonts(root)   # 命名默认字体也要缩放
    root.report_callback_exception = lambda exc, val, tb: (
        messagebox.showerror("程序错误", "".join(traceback.format_exception(exc, val, tb)))
        or traceback.print_exception(exc, val, tb))
    RecorderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
