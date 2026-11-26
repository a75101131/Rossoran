import sys
import os
import time
import sqlite3
import threading
import configparser
import ctypes
from datetime import datetime
import requests
import pyautogui
import pydirectinput
import cv2
import numpy as np
from mss import mss
import pygetwindow as gw
import win32gui
import win32con
from pynput import keyboard as pynput_keyboard
import keyboard as kb_global

import re
from PyQt5.QtWidgets import (
    QApplication, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox, QFormLayout, QGroupBox, QCheckBox,
    QRadioButton, QButtonGroup, QComboBox, QDialog, QListWidget, QPlainTextEdit,
    QFileDialog
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject

# 停用 Safety 檢查以利自動化連續控制
pyautogui.FAILSAFE = False
pydirectinput.FAILSAFE = False

CONFIG_FILE = "config.ini"
DB_FILE = os.path.join(os.path.abspath("."), "accounts.db")

# 全域變數宣告
window = None
auto_sell_running = False
revive_running = False
stop_flag = False
IS_NOTIFIED = False
revive_mode = 'same'         # 'same' 或 'cross'
sell_map_mode1 = 'phoenix'   # 車頭 1（左邊）：'phoenix' 鳳凰本館 | 'prison' 監獄 | 'cave' 中洞
sell_map_mode2 = 'phoenix'   # 車頭 2（右邊）：'phoenix' 鳳凰本館 | 'prison' 監獄 | 'cave' 中洞

# 監獄與中洞共用同地圖賣物/復活邏輯，只差圖片檔名
SAME_MAP_CONFIGS = {
    'prison': {
        'label': '監獄',
        'map_teacher_pic': 'prison_map_teacher.png',
        'screen_npc_pic': 'prison_npc.png',
        'buy_sell_pic': 'prison_buy_sell.png',
        'start_f6_pic': 'prison_start_F6.png',
    },
    'cave': {
        'label': '中洞',
        'map_teacher_pic': 'cave_map_teacher.png',
        'screen_npc_pic': 'cave_npc.png',
        'buy_sell_pic': 'cave_buy_sell.png',
        'start_f6_pic': 'cave_start_F6.png',
    },
}

# 各賣物地圖獨立設定表（所有 region 皆為相對於遊戲客戶區左上角 resolved_base_x, resolved_base_y 的 (dx, dy, 寬, 高)）
MAP_SELL_PROFILES = {
    'phoenix': {
        'label': '鳳凰本館',
        'map_teacher_pic': 'map_main_teacher.png',
        'map_teacher_region': (694, 1, 323, 295),
        'map_teacher_offset': (15, 35),
        'screen_npc_pic': 'kendo_teacher.png',
        'screen_npc_region': (392, 133, 363, 250),
        'screen_npc_offset': (37, 11),
        'buy_sell_pic': 'buy_sell.png',
        'buy_sell_region': (480, 350, 200, 200),
        'buy_sell_offset': (24, 6),
        'start_f6_pic': 'start_F6.png',
        'hunting_setup_region': (324, 375, 378, 292),
    },
    'prison': {
        'label': '監獄',
        'map_teacher_pic': 'prison_map_teacher.png',
        'map_teacher_region': (694, 1, 323, 295),
        'map_teacher_offset': (21, 17),
        'screen_npc_pic': 'prison_npc.png',
        'screen_npc_region': (100, 100, 500, 500),
        'screen_npc_offset': (24, 6),
        'buy_sell_pic': 'prison_buy_sell.png',
        'buy_sell_region': (480, 350, 200, 200),
        'buy_sell_offset': (24, 6),
        'start_f6_pic': 'prison_start_F6.png',
        'hunting_setup_region': (324, 375, 378, 292),
    },
    'cave': {
        'label': '中洞',
        'map_teacher_pic': 'cave_map_teacher.png',
        'map_teacher_region': (694, 1, 323, 295),
        'map_teacher_offset': (7, 13),
        'screen_npc_pic': 'cave_npc.png',
        'screen_npc_region': (100, 100, 800, 800),
        'screen_npc_offset': (29, 7),
        'buy_sell_pic': 'cave_buy_sell.png',
        'buy_sell_region': (480, 350, 200, 200),
        'buy_sell_offset': (24, 6),
        'start_f6_pic': 'cave_start_F6.png',
        'hunting_setup_region': (324, 375, 378, 292),
    },
}

def resolve_region(base_x, base_y, rel_region):
    """將相對於視窗客戶區的偵測區域 (dx, dy, w, h) 轉換為螢幕絕對座標區域"""
    rx, ry, rw, rh = rel_region
    return (base_x + rx, base_y + ry, rw, rh)

# ==========================================
# 0. 底層 DirectInput 硬體 ScanCode 發送模組
# ==========================================
PUL = ctypes.POINTER(ctypes.c_ulong)

class KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL)
    ]

class HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort)
    ]

class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL)
    ]

class Input_I(ctypes.Union):
    _fields_ = [("ki", KeyBdInput), ("mi", MouseInput), ("hi", HardwareInput)]

class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ii", Input_I)]

SCANCODE_TAB = 0x0F
SCANCODE_ENTER = 0x1C

def send_scancode(scancode):
    extra = ctypes.c_ulong(0)
    ii_ = Input_I()
    ii_.ki = KeyBdInput(0, scancode, 0x0008, 0, ctypes.pointer(extra))
    x = Input(ctypes.c_ulong(1), ii_)
    ctypes.windll.user32.SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))
    time.sleep(0.12)

    ii_.ki = KeyBdInput(0, scancode, 0x0008 | 0x0002, 0, ctypes.pointer(extra))
    x = Input(ctypes.c_ulong(1), ii_)
    ctypes.windll.user32.SendInput(1, ctypes.pointer(x), ctypes.sizeof(x))
    time.sleep(0.2)

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

# ==========================================
# 0.5 SQLite 角色帳密二道資料庫模組
# ==========================================
db_lock = threading.Lock()

def get_db_path():
    return os.path.join(os.path.abspath("."), "accounts.db")

def init_database():
    with db_lock:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS characters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    char_name TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    pin_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    sort_order INTEGER NOT NULL DEFAULT 0
                )
            """)
            cols = [row[1] for row in conn.execute("PRAGMA table_info(characters)").fetchall()]
            if "sort_order" not in cols:
                conn.execute("ALTER TABLE characters ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
                existing = conn.execute(
                    "SELECT id FROM characters ORDER BY id ASC"
                ).fetchall()
                for idx, (row_id,) in enumerate(existing, start=1):
                    conn.execute("UPDATE characters SET sort_order = ? WHERE id = ?", (idx, row_id))
            conn.commit()
        finally:
            conn.close()

def fetch_all_characters():
    init_database()
    with db_lock:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        try:
            cur = conn.execute(
                "SELECT id, char_name, username, password, pin_code, updated_at "
                "FROM characters ORDER BY sort_order ASC, id ASC"
            )
            return cur.fetchall()
        finally:
            conn.close()

def fetch_character_by_name(char_name):
    if not char_name:
        return None
    init_database()
    with db_lock:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        try:
            cur = conn.execute(
                "SELECT id, char_name, username, password, pin_code FROM characters WHERE char_name = ?",
                (char_name,)
            )
            return cur.fetchone()
        finally:
            conn.close()

def upsert_character(char_name, username, password, pin_code):
    char_name = char_name.strip()
    username = username.strip()
    password = password.strip()
    pin_code = pin_code.strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    init_database()
    with db_lock:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        try:
            cur = conn.execute("SELECT id, sort_order FROM characters WHERE char_name = ?", (char_name,))
            existing = cur.fetchone()
            if existing:
                conn.execute("""
                    UPDATE characters
                    SET username = ?, password = ?, pin_code = ?, updated_at = ?
                    WHERE char_name = ?
                """, (username, password, pin_code, now_str, char_name))
            else:
                max_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM characters").fetchone()[0]
                conn.execute("""
                    INSERT INTO characters (char_name, username, password, pin_code, updated_at, sort_order)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (char_name, username, password, pin_code, now_str, max_order + 1))
            conn.commit()
        finally:
            conn.close()

def move_character_order(char_name, direction):
    """direction: -1 代表上移一格，+1 代表下移一格"""
    init_database()
    with db_lock:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        try:
            rows = conn.execute(
                "SELECT id, char_name FROM characters ORDER BY sort_order ASC, id ASC"
            ).fetchall()
            idx = next((i for i, r in enumerate(rows) if r[1] == char_name), None)
            if idx is None:
                return False
            target_idx = idx + direction
            if target_idx < 0 or target_idx >= len(rows):
                return False

            rows[idx], rows[target_idx] = rows[target_idx], rows[idx]
            for new_order, (row_id, _) in enumerate(rows, start=1):
                conn.execute("UPDATE characters SET sort_order = ? WHERE id = ?", (new_order, row_id))
            conn.commit()
            return True
        finally:
            conn.close()

def delete_character(char_name):
    init_database()
    with db_lock:
        conn = sqlite3.connect(get_db_path(), timeout=10)
        try:
            conn.execute("DELETE FROM characters WHERE char_name = ?", (char_name,))
            conn.commit()
        finally:
            conn.close()

def parse_characters_from_txt_content(content):
    """
    同時支援：
    1) 標準單行格式：角色名稱,帳號,密碼,二道密碼（亦相容 | / Tab / 空白分隔）
    2) 多行區塊格式（如 Rosso1.txt）：帳號： / 密碼： / 二道： / 角色：
    回傳 (parsed_list, skipped_lines)
    """
    lines = content.splitlines()
    parsed_list = []
    skipped_lines = []

    has_block_syntax = any(
        re.search(r'(帳號|密碼|二道|角色)\s*[：:]', ln) for ln in lines
    )

    if has_block_syntax:
        current = {"char_name": "", "username": "", "password": "", "pin_code": ""}

        def flush_current():
            if current["username"] and current["password"]:
                cname = current["char_name"] or current["username"]
                parsed_list.append((cname, current["username"], current["password"], current["pin_code"]))
            current["char_name"] = ""
            current["username"] = ""
            current["password"] = ""
            current["pin_code"] = ""

        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("---") or (line.startswith("[") and line.endswith("]")):
                continue

            if re.match(r'^\d+[\.、\)]\s*', line) and current["username"]:
                flush_current()

            m_user = re.search(r'帳號\s*[：:]\s*(\S+)', line)
            if m_user:
                if current["username"]:
                    flush_current()
                current["username"] = m_user.group(1).strip()
                continue

            m_pass = re.search(r'密碼\s*[：:]\s*(\S+)', line)
            if m_pass:
                current["password"] = m_pass.group(1).strip()
                continue

            m_pin = re.search(r'二道\s*[：:]\s*(\S+)', line)
            if m_pin:
                current["pin_code"] = m_pin.group(1).strip()
                continue

            m_char = re.search(r'角色\s*[：:]\s*(.+)', line)
            if m_char:
                current["char_name"] = m_char.group(1).strip()
                continue

        flush_current()
        return parsed_list, skipped_lines

    for idx, raw_line in enumerate(lines, start=1):
        raw = raw_line.strip()
        if not raw or raw.startswith("#") or raw.startswith("//") or raw.startswith("---"):
            continue

        if "," in raw:
            parts = [p.strip() for p in raw.split(",")]
        elif "，" in raw:
            parts = [p.strip() for p in raw.split("，")]
        elif "|" in raw:
            parts = [p.strip() for p in raw.split("|")]
        elif "\t" in raw:
            parts = [p.strip() for p in raw.split("\t")]
        else:
            parts = [p.strip() for p in re.split(r"\s+", raw)]

        parts = [p for p in parts if p != ""]
        if len(parts) >= 4:
            parsed_list.append((parts[0], parts[1], parts[2], parts[3]))
        elif len(parts) == 3:
            parsed_list.append((parts[0], parts[0], parts[1], parts[2]))
        elif len(parts) == 2:
            parsed_list.append((parts[0], parts[0], parts[1], ""))
        else:
            skipped_lines.append(f"第 {idx} 行：{raw}")

    return parsed_list, skipped_lines

# ==========================================
# 1. 通用資源、訊號與基礎工具
# ==========================================
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def get_asset_path(filename):
    return os.path.join(os.path.abspath("."), "assets", filename)

class WorkerSignals(QObject):
    log_signal = pyqtSignal(str)
    sell_status_signal = pyqtSignal(int, str)
    sell_header_signal = pyqtSignal(str, str)
    revive_status_signal = pyqtSignal(int, str)

signals = WorkerSignals()

def emit_action(msg):
    """統一輸出單行狀態列與第五頁詳細動作紀錄"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    signals.log_signal.emit(f"[{ts}] {msg}")

def click_smooth(x=None, y=None, hold_time=0.04):
    if x is not None and y is not None:
        pydirectinput.moveTo(int(x), int(y))
        time.sleep(0.04)
    pydirectinput.mouseDown()
    time.sleep(hold_time)
    pydirectinput.mouseUp()
    time.sleep(0.04)

def click_map_point_humanlike(x, y):
    target_x = int(x)
    target_y = int(y)
    pydirectinput.moveTo(target_x, target_y)
    time.sleep(0.2)
    pydirectinput.mouseDown(button='left')
    time.sleep(0.1)
    pydirectinput.mouseUp(button='left')
    time.sleep(0.15)

def press_key_smooth(key_name, hold_time=0.15):
    pydirectinput.keyDown(key_name)
    time.sleep(hold_time)
    pydirectinput.keyUp(key_name)
    time.sleep(0.1)

def send_telegram_notification(bot_token, chat_id, msg_text):
    def _send():
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg_text}, timeout=8)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

# ==========================================
# 2. 圖像模板比對核心模組
# ==========================================
def find_pic_in_region_sell(pic_name, region, threshold=0.72, get_center=False):
    try:
        pic_path = get_asset_path(pic_name)
        if not os.path.exists(pic_path):
            return None
        rx, ry, rw, rh = region
        if rw <= 0 or rh <= 0:
            return None
        screenshot = pyautogui.screenshot(region=(rx, ry, rw, rh))
        screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        template = cv2.imread(pic_path, cv2.IMREAD_UNCHANGED)
        if template is None:
            return None
        h, w = template.shape[:2]
        if h > screenshot_cv.shape[0] or w > screenshot_cv.shape[1]:
            return None

        if len(template.shape) == 3 and template.shape[2] == 4:
            bgr_template = template[:, :, :3]
            alpha_mask = template[:, :, 3]
            res = cv2.matchTemplate(screenshot_cv, bgr_template, cv2.TM_SQDIFF_NORMED, mask=alpha_mask)
            min_val, _, min_loc, _ = cv2.minMaxLoc(res)
            if (1.0 - min_val) >= threshold:
                if get_center:
                    return (rx + min_loc[0] + (w // 2), ry + min_loc[1] + (h // 2))
                return (rx + min_loc[0], ry + min_loc[1])

        gray_screenshot = cv2.cvtColor(screenshot_cv, cv2.COLOR_BGR2GRAY)
        gray_template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if len(template.shape) == 3 else template
        res = cv2.matchTemplate(gray_screenshot, gray_template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val >= threshold:
            if get_center:
                return (rx + max_loc[0] + (w // 2), ry + max_loc[1] + (h // 2))
            return (rx + max_loc[0], ry + max_loc[1])
        return None
    except Exception:
        return None

def wait_pic_exist_custom(pic_name, region, timeout_ms=1000, threshold=0.75):
    start_time = time.time()
    timeout_sec = timeout_ms / 1000.0
    while time.time() - start_time < timeout_sec:
        if not auto_sell_running:
            return False
        if find_pic_in_region_sell(pic_name, region, threshold):
            time.sleep(0.3)
            if find_pic_in_region_sell(pic_name, region, threshold):
                return True
        time.sleep(0.15)
    return False

def wait_and_click_map_teacher(pic_name, region, offset, timeout_ms=4000, threshold=0.72):
    start_time = time.time()
    timeout_sec = timeout_ms / 1000.0
    while time.time() - start_time < timeout_sec:
        if not auto_sell_running:
            return False
        pos = find_pic_in_region_sell(pic_name, region, threshold, get_center=False)
        if pos:
            click_x = pos[0] + offset[0]
            click_y = pos[1] + offset[1]
            emit_action(f"點擊地圖目標 [{pic_name}] 偏移後座標 ({click_x}, {click_y})")
            click_map_point_humanlike(click_x, click_y)
            return True
        time.sleep(0.2)
    return False

def wait_and_click_pic_offset(pic_name, region, offset, timeout_ms=3000, threshold=0.72):
    start_time = time.time()
    timeout_sec = timeout_ms / 1000.0
    while time.time() - start_time < timeout_sec:
        if not auto_sell_running:
            return False
        pos = find_pic_in_region_sell(pic_name, region, threshold, get_center=False)
        if pos:
            click_x = pos[0] + offset[0]
            click_y = pos[1] + offset[1]
            emit_action(f"偵測到 [{pic_name}] 於 {pos}，點擊 ({click_x}, {click_y})")
            click_smooth(click_x, click_y)
            return True
        time.sleep(0.2)
    return False

def wait_and_click_pic_center(pic_name, region, timeout_ms=3000, threshold=0.72):
    start_time = time.time()
    timeout_sec = timeout_ms / 1000.0
    while time.time() - start_time < timeout_sec:
        if not auto_sell_running and not revive_running:
            return False
        center_pos = find_pic_in_region_sell(pic_name, region, threshold, get_center=True)
        if center_pos:
            emit_action(f"偵測到 [{pic_name}] 中心點 {center_pos}，執行點擊")
            click_smooth(center_pos[0], center_pos[1])
            return True
        time.sleep(0.2)
    return False

# ==========================================
# 3. 遊戲登入器流程模組
# ==========================================
def focus_game_window():
    hwnd = None
    for win in gw.getAllWindows():
        if "RAN" in win.title.upper() or "ROSSO" in win.title.upper():
            hwnd = win._hWnd
            break

    if not hwnd:
        active = gw.getActiveWindow()
        hwnd = active._hWnd if active else None

    if hwnd:
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.1)
        except Exception:
            pass
    return hwnd

def win_click(rel_x, rel_y):
    focus_game_window()
    try:
        active_win = gw.getActiveWindow()
        if active_win:
            abs_x, abs_y = active_win.left + rel_x, active_win.top + rel_y
            emit_action(f"登入點擊視窗相對 ({rel_x}, {rel_y}) -> 螢幕 ({abs_x}, {abs_y})")
            pyautogui.moveTo(abs_x, abs_y, duration=0.1)
            pyautogui.mouseDown(button='left')
            time.sleep(0.08)
            pyautogui.mouseUp(button='left')
        else:
            pyautogui.click(rel_x, rel_y)
    except Exception as e:
        emit_action(f"點擊異常: {e}")
        pyautogui.click(rel_x, rel_y)

def wait_image_in_window(img_filename, timeout_sec, delay_after_found=4):
    global stop_flag
    img_path = get_resource_path(os.path.join("assets", img_filename))
    if not os.path.exists(img_path):
        img_path = get_resource_path(img_filename)
    if not os.path.exists(img_path):
        emit_action(f"錯誤: 找不到圖片檔案 -> {img_filename}")
        return False, 0, 0

    emit_action(f"正在等待畫面圖片: {img_filename}...")
    start_time = time.time()

    with mss() as sct:
        while time.time() - start_time < timeout_sec:
            if stop_flag:
                return False, 0, 0

            try:
                active_win = gw.getActiveWindow()
                if not active_win or active_win.width <= 0 or active_win.height <= 0:
                    time.sleep(0.5)
                    continue
                monitor = {
                    "top": max(0, active_win.top),
                    "left": max(0, active_win.left),
                    "width": max(10, active_win.width),
                    "height": max(10, active_win.height)
                }
            except Exception:
                monitor = sct.monitors[1]

            match_pos = find_pic_in_region_sell(
                img_filename,
                (monitor["left"], monitor["top"], monitor["width"], monitor["height"]),
                threshold=0.72,
                get_center=True
            )
            if match_pos:
                emit_action(f"偵測到 {img_filename} 於 {match_pos}！緩衝延遲 {delay_after_found} 秒...")
                for _ in range(int(delay_after_found * 10)):
                    if stop_flag:
                        return False, 0, 0
                    time.sleep(0.1)
                return True, match_pos[0], match_pos[1]
            time.sleep(0.5)
    return False, 0, 0

def type_string(text):
    for char in text:
        if stop_flag:
            break
        pyautogui.write(char)
        time.sleep(0.15)

def login_flow(acc_data, target_x, target_y, game_path, move_window=True, char_label=""):
    global stop_flag
    if stop_flag:
        return False

    user, pswd, pin = acc_data
    display_name = f"{char_label} ({user})" if char_label else user
    emit_action(f"開始執行 [{display_name}] 登入流程...")

    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", game_path, None, os.path.dirname(game_path), 1)
    except Exception as e:
        emit_action(f"遊戲啟動失敗: {e}")
        return False

    found, img_x, img_y = wait_image_in_window("ServerText.png", 300, delay_after_found=4)
    if not found:
        return False
    pyautogui.click(img_x + 50, img_y)
    time.sleep(0.8)

    win_click(497, 406); time.sleep(1.0)
    win_click(707, 407); time.sleep(1.0)
    win_click(770, 512); time.sleep(1.0)

    found, _, _ = wait_image_in_window("AccountText.png", 300, delay_after_found=3)
    if not found:
        return False

    win_click(519, 349); time.sleep(0.5)
    type_string(user); time.sleep(0.8)
    send_scancode(SCANCODE_TAB); time.sleep(0.8)
    type_string(pswd); time.sleep(0.8)
    send_scancode(SCANCODE_ENTER)

    found, _, _ = wait_image_in_window("CharSelect.png", 300, delay_after_found=3)
    if not found:
        return False

    win_click(887, 250); time.sleep(1.0)
    win_click(945, 561); time.sleep(1.0)
    type_string(pin); time.sleep(0.8)
    send_scancode(SCANCODE_ENTER)

    found, _, _ = wait_image_in_window("smile_feature.png", 300, delay_after_found=5)
    if not found:
        return False

    current_hwnd = win32gui.GetForegroundWindow()

    if move_window:
        try:
            active_win = gw.getActiveWindow()
            if active_win:
                active_win.moveTo(target_x, target_y)
                emit_action(f"視窗已移動至座標: ({target_x}, {target_y})")

                time.sleep(3)
                win32gui.ShowWindow(current_hwnd, win32con.SW_MINIMIZE)
                emit_action(f"[{display_name}] 已自動縮小至工作列！")
        except Exception as e:
            emit_action(f"移動或縮小視窗失敗: {e}")
    else:
        emit_action("車頭模式：保持原位。")

    emit_action(f"[{display_name}] 登入成功！")
    return True

# ==========================================
# 4. 第三頁：車頭自動賣東西完整核心邏輯
# ==========================================
sell_action_lock = threading.Lock()

def run_auto_sell_bot(bot_id, bot_token, chat_id, base_x, base_y, char_name="",
                      map_mode="phoenix",
                      phoenix_wait_town=9.0, phoenix_wait_walk=10.0, phoenix_wait_s=6.0,
                      same_revive_wait1=9.0, same_revive_wait2=13.0, same_revive_wait3=2.0,
                      same_sell_wait_town=9.0, same_sell_wait_walk=10.0, same_sell_wait_s=6.0,
                      sell_slot_count=30, sell_reverse=True,
                      sell_speed_scale=1.0, stuck_timeout_sec=3600.0):
    # ------------------------------------------
    # 視窗客戶區原點定位（取得遊戲內部畫面左上角座標）
    # ------------------------------------------
    resolved_base_x, resolved_base_y = base_x, base_y + 31
    try:
        game_wins = []
        for win in gw.getAllWindows():
            title = win.title.upper()
            if "RAN" in title or "ROSSO" in title:
                if win.width > 200 and "YOUTUBE" not in title:
                    game_wins.append(win)
        game_wins.sort(key=lambda w: w.left)

        if len(game_wins) >= bot_id:
            target_win = game_wins[bot_id - 1]
            hwnd = target_win._hWnd
            ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
            resolved_base_x, resolved_base_y = ox, oy + 31
    except Exception:
        pass

    # ------------------------------------------
    # 座標系統設定（皆以遊戲客戶區左上角 resolved_base_x, resolved_base_y 為基準）
    # ------------------------------------------
    check_slot_x = resolved_base_x + 970
    check_slot_y = resolved_base_y + 447
    frame_color = (167, 176, 183)

    first_slot_x = resolved_base_x + 776
    first_slot_y = resolved_base_y + 285
    grid_width = 36
    grid_height = 36
    cols_count = 6
    max_bag_slots = 30

    shop_empty_x = resolved_base_x + 717
    shop_empty_y = resolved_base_y + 493
    confirm_btn_x = resolved_base_x + 505
    confirm_btn_y = resolved_base_y + 386

    close_shop_xx_x = resolved_base_x + 736
    close_shop_xx_y = resolved_base_y + 187
    close_bag_xx_x = resolved_base_x + 975
    close_bag_xx_y = resolved_base_y + 95

    revive_region = (resolved_base_x + 342, resolved_base_y + 336, 341, 98)
    revive_offset = (39, 7)
    disconnect_region = (resolved_base_x + 154, resolved_base_y + 257, 660, 279)
    bugtrap_region = (resolved_base_x + 50, resolved_base_y + 50, 920, 620)

    pet_buff_region = (resolved_base_x + 39, resolved_base_y + 77, 41, 38)
    pet_tab_region = (resolved_base_x + 597, resolved_base_y + 278, 205, 29)
    pet_tab_offset = (6, 6)
    pet_list_region = (resolved_base_x + 597, resolved_base_y + 311, 204, 98)
    pet_skill_offset = (59, 6)

    clear_x = resolved_base_x + 490
    clear_y = resolved_base_y + 393
    pet_click_x = resolved_base_x + 576
    pet_click_y = resolved_base_y + 384

    profile = MAP_SELL_PROFILES.get(map_mode, MAP_SELL_PROFILES['phoenix'])
    p_map_region = resolve_region(resolved_base_x, resolved_base_y, profile['map_teacher_region'])
    p_map_offset = profile['map_teacher_offset']
    p_npc_region = resolve_region(resolved_base_x, resolved_base_y, profile['screen_npc_region'])
    p_npc_offset = profile['screen_npc_offset']
    p_bs_region = resolve_region(resolved_base_x, resolved_base_y, profile['buy_sell_region'])
    p_bs_offset = profile['buy_sell_offset']
    p_f6_region = resolve_region(resolved_base_x, resolved_base_y, profile['hunting_setup_region'])

    def update_status(text):
        msg = f"[車頭賣物 - 車頭 {bot_id}] {text}"
        emit_action(msg)
        signals.sell_status_signal.emit(bot_id, f"車頭 {bot_id}：{text}")

    def sleep_interruptible(seconds, check_death_flag=False):
        end_t = time.time() + max(0.0, float(seconds))
        while time.time() < end_t:
            if not auto_sell_running or stop_flag:
                return False
            if check_death_flag and is_dead():
                return False
            time.sleep(0.05)
        return True

    def click_smooth_sell(x=None, y=None, hold_time=0.04, speed_factor=1.0):
        sf = max(0.2, float(speed_factor))
        if x is not None and y is not None:
            pydirectinput.moveTo(int(x), int(y))
            time.sleep(0.04 * sf)
        pydirectinput.mouseDown()
        time.sleep(hold_time * sf)
        pydirectinput.mouseUp()
        time.sleep(0.04 * sf)

    def press_key_smooth_sell(key_name, hold_time=0.15):
        emit_action(f"[車頭 {bot_id}] 鍵盤按鍵: [{key_name}]")
        pydirectinput.keyDown(key_name)
        time.sleep(hold_time)
        pydirectinput.keyUp(key_name)
        time.sleep(0.1)

    def compare_rgb_color(c1, c2, max_diff=25):
        return all(abs(a - b) <= max_diff for a, b in zip(c1, c2))

    def is_bag_full():
        try:
            x, y = int(check_slot_x), int(check_slot_y)
            c1 = pyautogui.pixel(x, y)
            if compare_rgb_color(c1, frame_color, max_diff=45):
                time.sleep(0.15)
                c2 = pyautogui.pixel(x, y)
                if compare_rgb_color(c2, frame_color, max_diff=45):
                    return True
        except Exception:
            pass
        return False

    disconnect_notified = False
    bugtrap_notified = False

    def check_disconnect_or_bugtrap():
        """偵測斷線視窗與 BUGTRAP 視窗，各自發送 Telegram 通知"""
        nonlocal disconnect_notified, bugtrap_notified

        if not bugtrap_notified:
            bt_pos = find_pic_in_region_sell("bugtrap.png", bugtrap_region, threshold=0.72)
            if bt_pos:
                time.sleep(0.3)
                if find_pic_in_region_sell("bugtrap.png", bugtrap_region, threshold=0.72):
                    target_name = char_name if char_name else f"車頭{bot_id}"
                    msg = f"{target_name} 跳出 BUGTRAP 視窗"
                    update_status(f"🚨 偵測到 BUGTRAP 視窗於 {bt_pos}！")
                    send_telegram_notification(bot_token, chat_id, msg)
                    bugtrap_notified = True
                    return True

        if not disconnect_notified:
            pos = find_pic_in_region_sell("disconnect.png", disconnect_region, threshold=0.72)
            if pos:
                time.sleep(0.3)
                if find_pic_in_region_sell("disconnect.png", disconnect_region, threshold=0.72):
                    target_name = char_name if char_name else f"車頭{bot_id}"
                    msg = f"{target_name}斷線"
                    update_status(f"⚠️ 偵測到斷線於 {pos}！")
                    send_telegram_notification(bot_token, chat_id, msg)
                    disconnect_notified = True
                    return True

        return disconnect_notified or bugtrap_notified

    def clear_mouse_cursor(reason=""):
        emit_action(f"[車頭 {bot_id}] 清除滑鼠游標 ({clear_x}, {clear_y}) {reason}")
        click_smooth_sell(clear_x, clear_y, hold_time=0.04)
        time.sleep(0.15)
        click_smooth_sell(clear_x, clear_y, hold_time=0.04)
        time.sleep(0.15)

    def is_dead():
        return find_pic_in_region_sell("revive.png", revive_region, threshold=0.9) is not None

    def check_and_buff_pet_skill():
        if not auto_sell_running or stop_flag:
            return
        update_status("檢查並補發寵物技能...")
        if wait_pic_exist_custom("pet_buff.png", pet_buff_region, timeout_ms=1200, threshold=0.75):
            emit_action(f"[車頭 {bot_id}] 寵物撿物 BUFF 已存在")
        else:
            press_key_smooth_sell('l')
            time.sleep(0.3)
            if wait_and_click_pic_offset("none.png", pet_tab_region, pet_tab_offset, timeout_ms=2500, threshold=0.72):
                time.sleep(0.3)
            wait_and_click_pic_offset("loot_all.png", pet_list_region, pet_skill_offset, timeout_ms=2500, threshold=0.72)
            time.sleep(0.3)
            press_key_smooth_sell('l')
            time.sleep(0.3)

    def open_pet_and_bag(log_prefix="[執行程序]"):
        if not auto_sell_running or stop_flag:
            return
        press_key_smooth_sell('d')
        update_status(f"{log_prefix} 開啟寵物 (D)...")
        time.sleep(0.6)
        emit_action(f"[車頭 {bot_id}] 點擊寵物確認按鈕 ({pet_click_x}, {pet_click_y})")
        click_smooth_sell(pet_click_x, pet_click_y)
        time.sleep(0.6)

        if not auto_sell_running or stop_flag:
            return
        check_and_buff_pet_skill()

        if not auto_sell_running or stop_flag:
            return
        update_status(f"{log_prefix} 按下 I 鍵開啟背包...")
        press_key_smooth_sell('i')
        time.sleep(0.4)

    def phoenix_teleport_and_setup_hunting(log_prefix="[執行程序]"):
        if not auto_sell_running or stop_flag:
            return
        time.sleep(0.5)
        update_status(f"{log_prefix} 按下 S 前點卡...")
        press_key_smooth_sell('s')

        update_status(f"{log_prefix} 已按下 S，等待 {phoenix_wait_s:g} 秒地圖傳送與載入...")
        if not sleep_interruptible(phoenix_wait_s, check_death_flag=False):
            return

        press_key_smooth_sell('f5')
        update_status(f"{log_prefix} 已按下 F5，進行 5 秒偵測開始圖片...")
        wait_and_click_pic_center("start_F5.png", region=p_f6_region, timeout_ms=5000, threshold=0.8)
        time.sleep(0.6)
        press_key_smooth_sell('f5')
        time.sleep(0.6)

        if not auto_sell_running or stop_flag:
            return
        press_key_smooth_sell('f6')
        update_status(f"{log_prefix} 已按下 F6，進行 4 秒偵測開始圖片 ({profile['start_f6_pic']})...")
        wait_and_click_pic_center(profile['start_f6_pic'], region=p_f6_region, timeout_ms=4000, threshold=0.8)
        time.sleep(0.6)
        press_key_smooth_sell('f6')
        time.sleep(0.6)

        open_pet_and_bag(log_prefix=log_prefix)

    def build_slot_order():
        """輸入 N 格：勾選往回賣時從第 N 格往回賣到第 1 格；未勾選時從第 1 格賣到第 N 格"""
        count = max(1, min(60, int(sell_slot_count)))
        if sell_reverse:
            # 例如輸入 32 -> 32, 31, ..., 1（轉 0-based 為 31, 30, ..., 0）
            return [slot_1based - 1 for slot_1based in range(count, 0, -1)]
        else:
            # 例如輸入 32 -> 1, 2, ..., 32（轉 0-based 為 0, 1, ..., 31）
            return [slot_1based - 1 for slot_1based in range(1, count + 1)]

    def sell_inventory_items():
        slot_indices = build_slot_order()
        dir_text = "尾巴往回" if sell_reverse else "順向"
        update_status(f"開始出售背包物品（共 {len(slot_indices)} 格，{dir_text}，速度倍率 {sell_speed_scale:g}x）...")

        sf = max(0.2, float(sell_speed_scale))
        for step_idx, slot_idx in enumerate(slot_indices):
            if not auto_sell_running or stop_flag:
                emit_action(f"[車頭 {bot_id}] 賣物中斷：停止旗標觸發（第 {step_idx + 1}/{len(slot_indices)} 格）")
                return False
            if is_dead():
                update_status("💀 賣物途中偵測到死亡！立刻停止賣物並轉交復活流程...")
                clear_mouse_cursor("(賣物中死亡)")
                return False
            if step_idx % 10 == 0:
                check_disconnect_or_bugtrap()

            row = slot_idx // cols_count
            col = slot_idx % cols_count
            target_x = first_slot_x + (col * grid_width)
            target_y = first_slot_y + (row * grid_height)

            emit_action(
                f"[車頭 {bot_id}] 賣物進度 {step_idx + 1}/{len(slot_indices)} | 第{row + 1}橫向第{col + 1}格 (slot={slot_idx + 1}) -> ({target_x}, {target_y})"
            )

            # 1. 點擊背包物品
            click_smooth_sell(target_x, target_y, hold_time=0.04, speed_factor=sf)
            if is_dead() or not auto_sell_running or stop_flag:
                clear_mouse_cursor("(點背包後中斷)")
                return False
            time.sleep(0.06 * sf)

            # 2. 點擊商店空格放入
            click_smooth_sell(shop_empty_x, shop_empty_y, hold_time=0.04, speed_factor=sf)
            if is_dead() or not auto_sell_running or stop_flag:
                clear_mouse_cursor("(點商店空格後中斷)")
                return False
            time.sleep(0.06 * sf)

            # 3. 點擊確認賣出
            click_smooth_sell(confirm_btn_x, confirm_btn_y, hold_time=0.04, speed_factor=sf)
            if is_dead() or not auto_sell_running or stop_flag:
                clear_mouse_cursor("(點確認賣出後中斷)")
                return False
            time.sleep(0.08 * sf)

        clear_mouse_cursor("(賣物完成)")
        update_status("背包物品販賣完畢！")
        return True

    def phoenix_return_town_and_sell():
        update_status("【鳳凰本館】偵測背包已滿，按下起點卡回本館...")
        clear_mouse_cursor("(回本館前)")
        press_key_smooth_sell('a')

        update_status(f"【鳳凰本館】已按下起點卡，固定等待 {phoenix_wait_town:g} 秒完成回城載入...")
        end_t = time.time() + float(phoenix_wait_town)
        idx = 0
        while time.time() < end_t:
            if not auto_sell_running or stop_flag or is_dead():
                emit_action(f"[車頭 {bot_id}] 回本館等待期間中斷")
                return
            if idx % 20 == 0:
                check_disconnect_or_bugtrap()
            idx += 1
            time.sleep(0.05)

        clear_mouse_cursor("(本館載入後)")
        update_status("【鳳凰本館】地圖載入完成，開始尋找地圖商人...")
        if is_dead() or stop_flag:
            return

        max_retries = 3
        teacher_found = False
        for attempt in range(1, max_retries + 1):
            if not auto_sell_running or stop_flag or is_dead():
                return
            update_status(f"【鳳凰本館】尋找地圖商人中 (第 {attempt} 次)...")
            press_key_smooth_sell('m')
            time.sleep(1.0)
            if wait_and_click_map_teacher(profile['map_teacher_pic'], p_map_region, p_map_offset, timeout_ms=4000, threshold=0.9):
                time.sleep(0.4)
                press_key_smooth_sell('m')
                if not sleep_interruptible(phoenix_wait_walk, check_death_flag=True):
                    return
                check_disconnect_or_bugtrap()
                if find_pic_in_region_sell(profile['screen_npc_pic'], p_npc_region, threshold=0.9):
                    teacher_found = True
                    break
            else:
                emit_action(f"[車頭 {bot_id}] 地圖未找到 {profile['map_teacher_pic']}，關閉地圖重試")
                press_key_smooth_sell('m')
                time.sleep(1.0)

        if not teacher_found:
            if is_dead() or not auto_sell_running or stop_flag:
                return
            update_status("⚠️ 嘗試 3 次仍未偵測到地圖商人！執行防卡死備援：直接按下 S 返回...")
            clear_mouse_cursor("(備援前)")
            phoenix_teleport_and_setup_hunting(log_prefix="[備援程序]")
            return

        if is_dead() or stop_flag:
            return

        update_status("【鳳凰本館】點擊老師準備進行交易...")
        if wait_and_click_pic_offset(profile['screen_npc_pic'], p_npc_region, p_npc_offset, timeout_ms=5000, threshold=0.9):
            time.sleep(1.0)
            if is_dead() or stop_flag:
                return
            if wait_and_click_pic_offset(profile['buy_sell_pic'], p_bs_region, p_bs_offset, timeout_ms=4000, threshold=0.9):
                time.sleep(1.0)
                if is_dead() or stop_flag:
                    return
                finished = sell_inventory_items()
                if not finished or is_dead() or stop_flag:
                    return
        else:
            emit_action(f"[車頭 {bot_id}] 未找到畫面 NPC {profile['screen_npc_pic']}")

        clear_mouse_cursor("(交易結束後)")
        if is_dead() or stop_flag:
            return

        update_status("【鳳凰本館】販賣完成，準備返回練功點...")
        phoenix_teleport_and_setup_hunting(log_prefix="[執行程序]")

    def same_map_return_point_and_sell(cfg):
        map_label = cfg['label']

        def same_map_finish_and_resume_hunting(log_prefix="[同地圖]"):
            if is_dead() or not auto_sell_running or stop_flag:
                return

            update_status(f"{log_prefix} 依序點擊兩個 XX 關閉視窗...")
            emit_action(f"[車頭 {bot_id}] 點擊關閉商店 XX ({close_shop_xx_x}, {close_shop_xx_y})")
            click_smooth_sell(close_shop_xx_x, close_shop_xx_y)
            time.sleep(0.25)
            if is_dead() or not auto_sell_running or stop_flag:
                return

            emit_action(f"[車頭 {bot_id}] 點擊關閉背包 XX ({close_bag_xx_x}, {close_bag_xx_y})")
            click_smooth_sell(close_bag_xx_x, close_bag_xx_y)
            time.sleep(0.25)
            if is_dead() or not auto_sell_running or stop_flag:
                return

            update_status(f"{log_prefix} 按下 S 前點卡返回...")
            press_key_smooth_sell('s')

            update_status(f"{log_prefix} 已按下 S，等待 {same_sell_wait_s:g} 秒載入...")
            if not sleep_interruptible(same_sell_wait_s, check_death_flag=True):
                return

            if is_dead() or not auto_sell_running or stop_flag:
                return

            update_status(f"{log_prefix} 開啟掛機設定 (F6)...")
            press_key_smooth_sell('f6')
            time.sleep(0.5)

            update_status(f"{log_prefix} 偵測並點擊開始圖片 ({cfg['start_f6_pic']})...")
            wait_and_click_pic_center(cfg['start_f6_pic'], region=p_f6_region, timeout_ms=4000, threshold=0.8)
            time.sleep(0.6)

            update_status(f"{log_prefix} 關閉掛機設定 (F6)...")
            press_key_smooth_sell('f6')
            time.sleep(0.4)

            if is_dead() or not auto_sell_running or stop_flag:
                return

            check_and_buff_pet_skill()

            if is_dead() or not auto_sell_running or stop_flag:
                return

            update_status(f"{log_prefix} 按下 I 鍵開啟背包...")
            press_key_smooth_sell('i')
            time.sleep(0.4)

            update_status(f"{log_prefix} 重返掛打流程完成！")

        update_status(f"【{map_label}同地圖】偵測背包已滿，按下起點卡回同地圖復活點...")
        clear_mouse_cursor("(同地圖起點前)")
        press_key_smooth_sell('a')
        time.sleep(0.5)

        update_status(f"【{map_label}同地圖】等待 {same_sell_wait_town:g} 秒回同地圖復活點...")
        end_t = time.time() + float(same_sell_wait_town)
        idx = 0
        while time.time() < end_t:
            if not auto_sell_running or stop_flag or is_dead():
                return
            if idx % 20 == 0:
                check_disconnect_or_bugtrap()
            idx += 1
            time.sleep(0.05)

        if is_dead() or stop_flag:
            return

        max_retries = 3
        npc_found = False
        for attempt in range(1, max_retries + 1):
            if not auto_sell_running or stop_flag or is_dead():
                return
            update_status(f"【{map_label}同地圖】開地圖偵測老師座標 ({cfg['map_teacher_pic']})，第 {attempt} 次...")
            press_key_smooth_sell('m')
            time.sleep(1.0)
            if wait_and_click_map_teacher(cfg['map_teacher_pic'], p_map_region, p_map_offset, timeout_ms=4000, threshold=0.9):
                time.sleep(0.4)
                press_key_smooth_sell('m')
                update_status(f"【{map_label}同地圖】已關地圖，走路等待 {same_sell_wait_walk:g} 秒偵測 NPC...")
                if not sleep_interruptible(same_sell_wait_walk, check_death_flag=True):
                    return
                check_disconnect_or_bugtrap()
                if find_pic_in_region_sell(cfg['screen_npc_pic'], p_npc_region, threshold=0.9):
                    npc_found = True
                    break
            else:
                emit_action(f"[車頭 {bot_id}] {map_label} 地圖未找到 {cfg['map_teacher_pic']}")
                press_key_smooth_sell('m')
                time.sleep(1.0)

        if not npc_found:
            if is_dead() or not auto_sell_running or stop_flag:
                return
            update_status(f"⚠️ 【{map_label}同地圖】未偵測到 NPC，執行備援重返掛打...")
            clear_mouse_cursor("(同地圖備援前)")
            same_map_finish_and_resume_hunting(log_prefix=f"[{map_label}備援]")
            return

        if is_dead() or stop_flag:
            return

        update_status(f"【{map_label}同地圖】點擊 NPC ({cfg['screen_npc_pic']})...")
        if wait_and_click_pic_offset(cfg['screen_npc_pic'], p_npc_region, p_npc_offset, timeout_ms=5000, threshold=0.9):
            time.sleep(1.0)
            if is_dead() or stop_flag:
                return
            if wait_and_click_pic_offset(cfg['buy_sell_pic'], p_bs_region, p_bs_offset, timeout_ms=4000, threshold=0.9):
                time.sleep(1.0)
                if is_dead() or stop_flag:
                    return
                finished = sell_inventory_items()
                if not finished or is_dead() or stop_flag:
                    return
        else:
            emit_action(f"[車頭 {bot_id}] {map_label} 未找到畫面 NPC {cfg['screen_npc_pic']}")

        clear_mouse_cursor("(同地圖賣物後)")
        if is_dead() or stop_flag:
            return

        same_map_finish_and_resume_hunting(log_prefix=f"[{map_label}執行]")

    def check_and_handle_death():
        if not is_dead():
            return False

        if map_mode in SAME_MAP_CONFIGS:
            cfg = SAME_MAP_CONFIGS[map_mode]
            map_label = cfg['label']
            update_status(f"💀 【{map_label}同地圖】偵測死亡，排隊等待同地圖復活...")
            with sell_action_lock:
                if not auto_sell_running or stop_flag:
                    return False
                pos = find_pic_in_region_sell("revive.png", revive_region, threshold=0.9)
                if not pos:
                    return False

                update_status(f"💀 【{map_label}同地圖】原地等待 {same_revive_wait1:g} 秒...")
                if not sleep_interruptible(same_revive_wait1):
                    return False

                update_status(f"【{map_label}同地圖】點擊復活...")
                click_smooth_sell(pos[0] + revive_offset[0], pos[1] + revive_offset[1])

                update_status(f"【{map_label}同地圖】復活後計時 {same_revive_wait2:g} 秒（第 2 秒點停止）...")
                start_after = time.time()
                clicked_stop = False
                while time.time() - start_after < float(same_revive_wait2):
                    if not auto_sell_running or stop_flag:
                        return False
                    if (time.time() - start_after) >= 2.0 and not clicked_stop:
                        update_status(f"【{map_label}同地圖】到達 2 秒，點擊掛打停止...")
                        click_smooth_sell(pos[0] + 1, pos[1] + 226)
                        clicked_stop = True
                    time.sleep(0.05)

                if not auto_sell_running or stop_flag:
                    return False

                clear_mouse_cursor("(同地圖復活-關商店前)")
                update_status(f"【{map_label}同地圖】點擊關閉商店 XX...")
                click_smooth_sell(close_shop_xx_x, close_shop_xx_y)
                time.sleep(0.25)
                clear_mouse_cursor("(同地圖復活-關商店後)")

                if not auto_sell_running or stop_flag:
                    return False

                update_status(f"【{map_label}同地圖】點擊關閉背包 XX...")
                click_smooth_sell(close_bag_xx_x, close_bag_xx_y)
                time.sleep(0.25)

                if not auto_sell_running or stop_flag:
                    return False

                update_status(f"【{map_label}同地圖】按下 S 前點卡潛回...")
                press_key_smooth_sell('s')

                update_status(f"【{map_label}同地圖】前點卡後等待 {same_revive_wait3:g} 秒...")
                if not sleep_interruptible(same_revive_wait3):
                    return False

                if not auto_sell_running or stop_flag:
                    return False

                update_status(f"【{map_label}同地圖】開啟掛機設定 (F6)...")
                press_key_smooth_sell('f6')
                time.sleep(0.5)

                update_status(f"【{map_label}同地圖】偵測並點擊開始圖片 ({cfg['start_f6_pic']})...")
                if not wait_and_click_pic_center(cfg['start_f6_pic'], region=p_f6_region, timeout_ms=4000, threshold=0.8):
                    click_smooth_sell(pos[0] + 1, pos[1] + 226)
                time.sleep(0.6)

                update_status(f"【{map_label}同地圖】關閉掛機設定 (F6)...")
                press_key_smooth_sell('f6')
                time.sleep(0.4)

                if not auto_sell_running or stop_flag:
                    return False

                check_and_buff_pet_skill()

                if not auto_sell_running or stop_flag:
                    return False

                update_status(f"【{map_label}同地圖】按下 I 鍵開啟背包...")
                press_key_smooth_sell('i')
                time.sleep(0.4)

                update_status(f"【{map_label}同地圖】復活與重返掛機流程結束，釋放執行權。")
            return True

        update_status("💀 【鳳凰本館】偵測到角色死亡，排隊等待復活與重返練功點...")
        with sell_action_lock:
            if not auto_sell_running or stop_flag:
                return False
            if not wait_and_click_pic_offset("revive.png", revive_region, revive_offset, timeout_ms=800, threshold=0.9):
                return False

            update_status("【鳳凰本館】正在點擊復活...")
            if not sleep_interruptible(5.0):
                return False

            update_status(f"【鳳凰本館】固定等待 {phoenix_wait_town:g} 秒完成回本館載入...")
            end_t = time.time() + float(phoenix_wait_town)
            idx = 0
            while time.time() < end_t:
                if not auto_sell_running or stop_flag:
                    return False
                if idx % 20 == 0:
                    check_disconnect_or_bugtrap()
                idx += 1
                time.sleep(0.05)

            clear_mouse_cursor("(本館復活後)")
            phoenix_teleport_and_setup_hunting(log_prefix="[執行程序]")
            update_status("【鳳凰本館】死亡復活與重返練功點流程結束！釋放執行權。")
        return True

    mode_name = SAME_MAP_CONFIGS[map_mode]['label'] + "（同地圖）" if map_mode in SAME_MAP_CONFIGS else "鳳凰本館（跨地圖）"
    update_status(
        f"車頭自動賣東西已啟動 [{mode_name}] (角色: {char_name}) | "
        f"格數={sell_slot_count} 倒賣={sell_reverse} 速度={sell_speed_scale:g}x 卡圖超時={stuck_timeout_sec:g}s"
    )
    last_active_time = time.time()
    stuck_notified = False

    while auto_sell_running and not stop_flag:
        if check_disconnect_or_bugtrap():
            time.sleep(1.0)
            continue

        if is_dead():
            check_and_handle_death()
            last_active_time = time.time()
            stuck_notified = False
            continue

        if is_bag_full():
            update_status("📦 偵測背包已滿，排隊等待賣物與重返練功點...")
            with sell_action_lock:
                if not auto_sell_running or stop_flag:
                    break
                update_status("取得執行權，開始賣物流程...")
                if map_mode in SAME_MAP_CONFIGS:
                    same_map_return_point_and_sell(SAME_MAP_CONFIGS[map_mode])
                else:
                    phoenix_return_town_and_sell()
                update_status("賣物流程結束，釋放執行權。")
            update_status("車頭自動賣東西恢復巡邏中...")
            last_active_time = time.time()
            stuck_notified = False
            continue

        if not stuck_notified and (time.time() - last_active_time >= float(stuck_timeout_sec)):
            target_name = char_name if char_name else f"車頭{bot_id}"
            msg = f"{target_name}\n疑似卡圖斷線請上線查看"
            update_status(f"⚠️ 超過 {stuck_timeout_sec:g} 秒無動作，準備發送 Telegram 卡圖通知")
            send_telegram_notification(bot_token, chat_id, msg)
            stuck_notified = True

        time.sleep(0.8)
    update_status("車頭自動賣東西已停止。")

# ==========================================
# 5. 第四頁：三區自動復活與監控核心邏輯
# ==========================================
revive_mouse_lock = threading.Lock()
revive_action_labels = {}

def update_revive_bot_status(bot_id, text):
    emit_action(f"[三區復活 - 角色 {bot_id}] {text}")
    signals.revive_status_signal.emit(bot_id, f"角色 {bot_id}：{text}")

class ReviveBotThread(threading.Thread):
    def __init__(self, bot_id, bot_token, chat_id, base_x, base_y, char_name="", mode="same",
                 same_wait_before=9.0, same_wait_after_revive=13.0, same_wait_after_card=2.0,
                 cross_wait_after_revive=20.0, cross_wait_after_card=6.0):
        super().__init__()
        self.bot_id = bot_id
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.char_name = char_name
        self.mode = mode
        self.daemon = True
        self.disconnect_notified = False
        self.bugtrap_notified = False

        self.same_wait_before = float(same_wait_before)
        self.same_wait_after_revive = float(same_wait_after_revive)
        self.same_wait_after_card = float(same_wait_after_card)
        self.cross_wait_after_revive = float(cross_wait_after_revive)
        self.cross_wait_after_card = float(cross_wait_after_card)

        self.base_x = base_x
        self.base_y = base_y

        self.DISCONNECT_REGION_REVIVE = (base_x + 602, base_y + 424, 660, 279)
        self.BUGTRAP_REGION_REVIVE = (base_x + 50, base_y + 50, 960, 680)
        self.REVIVE_REGION_REVIVE = (base_x + 373, base_y + 371, 296, 80)
        self.REVIVE_OFFSET_REVIVE = (39, 7)

    def focus_this_window(self):
        center_x = self.base_x + 512
        center_y = self.base_y + 384
        pydirectinput.moveTo(int(center_x), int(center_y))
        time.sleep(0.05)
        pydirectinput.mouseDown()
        time.sleep(0.04)
        pydirectinput.mouseUp()
        time.sleep(0.05)

    def click_smooth_revive(self, x=None, y=None, hold_time=0.04):
        if x is not None and y is not None:
            pydirectinput.moveTo(int(x), int(y))
            time.sleep(0.06)
        pydirectinput.mouseDown()
        time.sleep(hold_time)
        pydirectinput.mouseUp()
        time.sleep(0.06)

    def press_key_smooth_revive(self, key_name, hold_time=0.15):
        emit_action(f"[三區復活 - 角色 {self.bot_id}] 按鍵 [{key_name}]")
        self.focus_this_window()
        pydirectinput.keyDown(key_name)
        time.sleep(hold_time)
        pydirectinput.keyUp(key_name)
        time.sleep(0.1)

    def find_pic_in_region_revive(self, pic_name, region, threshold=0.72, get_center=False):
        return find_pic_in_region_sell(pic_name, region, threshold, get_center)

    def check_disconnect_revive(self):
        if not self.bugtrap_notified:
            bt_pos = self.find_pic_in_region_revive("bugtrap.png", self.BUGTRAP_REGION_REVIVE, threshold=0.72)
            if bt_pos:
                update_revive_bot_status(self.bot_id, "🚨 偵測到 BUGTRAP 視窗！")
                target_name = self.char_name if self.char_name else f"3區角色{self.bot_id}"
                send_telegram_notification(self.bot_token, self.chat_id, f"{target_name} 跳出 BUGTRAP 視窗")
                self.bugtrap_notified = True
                return True

        if not self.disconnect_notified:
            pos = self.find_pic_in_region_revive("disconnect.png", self.DISCONNECT_REGION_REVIVE, threshold=0.72)
            if pos:
                update_revive_bot_status(self.bot_id, "⚠ 偵測到斷線！")
                target_name = self.char_name if self.char_name else f"3區角色{self.bot_id}"
                send_telegram_notification(self.bot_token, self.chat_id, f"{target_name}斷線")
                self.disconnect_notified = True
                return True

        return self.disconnect_notified or self.bugtrap_notified

    def check_and_handle_death_revive(self):
        if self.mode == "same":
            pos = self.find_pic_in_region_revive("revive.png", self.REVIVE_REGION_REVIVE, threshold=0.9)
            if pos:
                if not revive_running:
                    return
                revive_pos = pos
                update_revive_bot_status(self.bot_id, f"💀 偵測到死亡，獨立等待 {self.same_wait_before:g} 秒...")
                start_wait = time.time()
                while time.time() - start_wait < self.same_wait_before:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    time.sleep(0.05)

                update_revive_bot_status(self.bot_id, f"{self.same_wait_before:g} 秒已過，正式點擊復活...")
                click_x = revive_pos[0] + self.REVIVE_OFFSET_REVIVE[0]
                click_y = revive_pos[1] + self.REVIVE_OFFSET_REVIVE[1]
                with revive_mouse_lock:
                    self.focus_this_window()
                    self.click_smooth_revive(click_x, click_y)

                update_revive_bot_status(self.bot_id, f"已點擊復活，開始 {self.same_wait_after_revive:g} 秒獨立計時...")
                start_after_revive = time.time()
                clicked_stop = False
                while time.time() - start_after_revive < self.same_wait_after_revive:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    elapsed = time.time() - start_after_revive
                    if elapsed >= 2.0 and not clicked_stop:
                        update_revive_bot_status(self.bot_id, "到達 2 秒，點擊掛打停止按鈕...")
                        target_btn_x = revive_pos[0] + 1
                        target_btn_y = revive_pos[1] + 226
                        with revive_mouse_lock:
                            self.focus_this_window()
                            self.click_smooth_revive(target_btn_x, target_btn_y)
                        clicked_stop = True
                    time.sleep(0.05)

                with revive_mouse_lock:
                    update_revive_bot_status(self.bot_id, f"{self.same_wait_after_revive:g} 秒到，按下 s 前點卡潛回...")
                    self.press_key_smooth_revive('s')

                update_revive_bot_status(self.bot_id, f"已按下 s，等待 {self.same_wait_after_card:g} 秒畫面載入...")
                start_after_card = time.time()
                while time.time() - start_after_card < self.same_wait_after_card:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    time.sleep(0.05)

                if not revive_running or self.check_disconnect_revive():
                    return

                with revive_mouse_lock:
                    update_revive_bot_status(self.bot_id, "點擊掛打開始按鈕...")
                    target_btn_x = revive_pos[0] + 1
                    target_btn_y = revive_pos[1] + 226
                    self.focus_this_window()
                    self.click_smooth_revive(target_btn_x, target_btn_y)

                update_revive_bot_status(self.bot_id, "同地圖復活流程完成！恢復即時監控。")
        else:
            pos = self.find_pic_in_region_revive("revive.png", self.REVIVE_REGION_REVIVE, threshold=0.9)
            if pos:
                if not revive_running:
                    return
                revive_pos = pos
                update_revive_bot_status(self.bot_id, "💀 偵測死亡！排隊等待跨地圖復活...")
                with revive_mouse_lock:
                    if not revive_running:
                        return
                    update_revive_bot_status(self.bot_id, "取得執行權，點擊復活 (跨地圖)...")
                    click_x = revive_pos[0] + self.REVIVE_OFFSET_REVIVE[0]
                    click_y = revive_pos[1] + self.REVIVE_OFFSET_REVIVE[1]
                    self.focus_this_window()
                    self.click_smooth_revive(click_x, click_y)

                    update_revive_bot_status(self.bot_id, f"跨地圖復活，精準等待 {self.cross_wait_after_revive:g} 秒...")
                    start_after_revive = time.time()
                    while time.time() - start_after_revive < self.cross_wait_after_revive:
                        if not revive_running or self.check_disconnect_revive():
                            return
                        time.sleep(0.05)

                    self.press_key_smooth_revive('s')
                    update_revive_bot_status(self.bot_id, f"已按下 s 前點卡，精準等待 {self.cross_wait_after_card:g} 秒...")
                    start_after_card = time.time()
                    while time.time() - start_after_card < self.cross_wait_after_card:
                        if not revive_running or self.check_disconnect_revive():
                            return
                        time.sleep(0.05)

                    if not revive_running or self.check_disconnect_revive():
                        return

                    update_revive_bot_status(self.bot_id, "開啟掛機流程 (F6)...")
                    self.press_key_smooth_revive('f6')
                    time.sleep(0.5)

                    update_revive_bot_status(self.bot_id, "點擊掛打開始按鈕...")
                    target_btn_x = revive_pos[0] + 1
                    target_btn_y = revive_pos[1] + 226
                    self.focus_this_window()
                    self.click_smooth_revive(target_btn_x, target_btn_y)

                    time.sleep(0.5)
                    self.press_key_smooth_revive('f6')
                    time.sleep(1.0)
                    update_revive_bot_status(self.bot_id, "跨地圖復活流程完整結束！釋放執行權。")

    def run(self):
        update_revive_bot_status(self.bot_id, "執行緒啟動，開始巡邏監控...")
        while revive_running:
            self.check_disconnect_revive()
            self.check_and_handle_death_revive()
            time.sleep(0.8)
        update_revive_bot_status(self.bot_id, "執行緒已停止")

# ==========================================
# 5.5 角色資料庫新增 / 修改 / 刪除彈窗
# ==========================================
class CharacterManagerDialog(QDialog):
    def __init__(self, parent=None, initial_name=""):
        super().__init__(parent)
        self.setWindowTitle("角色帳密二道資料庫管理 (accounts.db)")
        self.resize(580, 350)
        self.init_ui()
        self.refresh_list(select_name=initial_name)

    def init_ui(self):
        layout = QHBoxLayout()

        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("已存角色清單（可上下調整順序）："))

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("搜尋角色名稱或帳號...")
        self.txt_search.textChanged.connect(lambda: self.refresh_list(select_name=self.txt_name.text().strip()))
        left_box.addWidget(self.txt_search)

        self.lst_chars = QListWidget()
        self.lst_chars.setSelectionMode(QListWidget.ExtendedSelection)
        self.lst_chars.currentTextChanged.connect(self.on_select_char)
        left_box.addWidget(self.lst_chars)

        move_row = QHBoxLayout()
        self.btn_move_up = QPushButton("▲ 上移")
        self.btn_move_down = QPushButton("▼ 下移")
        self.btn_move_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_move_down.clicked.connect(lambda: self.move_selected(1))
        move_row.addWidget(self.btn_move_up)
        move_row.addWidget(self.btn_move_down)
        left_box.addLayout(move_row)

        btn_new = QPushButton("清空 / 新增單筆")
        btn_new.clicked.connect(self.clear_form)
        left_box.addWidget(btn_new)

        right_box = QVBoxLayout()
        form = QFormLayout()
        self.txt_name = QLineEdit()
        self.txt_user = QLineEdit()
        self.txt_pass = QLineEdit()
        self.txt_pin = QLineEdit()

        form.addRow("角色名稱:", self.txt_name)
        form.addRow("帳號:", self.txt_user)
        form.addRow("密碼:", self.txt_pass)
        form.addRow("二道密碼:", self.txt_pin)
        right_box.addLayout(form)

        lbl_hint = QLabel(
            f"<small style='color:#555;'>資料庫：{get_db_path()}<br>"
            "• 支援匯入「角色名稱,帳號,密碼,二道密碼」或您的多行區塊 TXT。<br>"
            "• 匯入或整理完，直接複製 accounts.db 到其他 VM 即可讀取。</small>"
        )
        lbl_hint.setWordWrap(True)
        right_box.addWidget(lbl_hint)
        right_box.addStretch()

        txt_row = QHBoxLayout()
        btn_import_txt = QPushButton("批次匯入 TXT")
        btn_import_txt.setStyleSheet("font-weight:bold; color:#0055aa;")
        btn_import_txt.clicked.connect(self.import_from_txt)

        btn_export_txt = QPushButton("匯出標準 TXT")
        btn_export_txt.clicked.connect(self.export_to_txt)
        txt_row.addWidget(btn_import_txt)
        txt_row.addWidget(btn_export_txt)
        right_box.addLayout(txt_row)

        act_row = QHBoxLayout()
        btn_save = QPushButton("儲存 / 更新")
        btn_save.setStyleSheet("font-weight:bold; color:green;")
        btn_save.clicked.connect(self.save_char)

        btn_del = QPushButton("刪除角色")
        btn_del.setStyleSheet("font-weight:bold; color:red;")
        btn_del.clicked.connect(self.delete_char)

        btn_close = QPushButton("關閉")
        btn_close.clicked.connect(self.accept)

        act_row.addWidget(btn_save)
        act_row.addWidget(btn_del)
        act_row.addWidget(btn_close)
        right_box.addLayout(act_row)

        layout.addLayout(left_box, 1)
        layout.addLayout(right_box, 2)
        self.setLayout(layout)

    def refresh_list(self, select_name=""):
        keyword = self.txt_search.text().strip().lower() if hasattr(self, "txt_search") else ""
        is_filtered = bool(keyword)
        if hasattr(self, "btn_move_up"):
            self.btn_move_up.setEnabled(not is_filtered)
            self.btn_move_down.setEnabled(not is_filtered)

        self.lst_chars.blockSignals(True)
        self.lst_chars.clear()
        rows = fetch_all_characters()
        for r in rows:
            c_name, u_name = r[1], r[2]
            if keyword and (keyword not in c_name.lower()) and (keyword not in u_name.lower()):
                continue
            self.lst_chars.addItem(c_name)
        self.lst_chars.blockSignals(False)

        if select_name:
            matches = self.lst_chars.findItems(select_name, Qt.MatchExactly)
            if matches:
                self.lst_chars.setCurrentItem(matches[0])
                self.on_select_char(select_name)
                return
        if self.lst_chars.count() > 0 and not select_name:
            self.lst_chars.setCurrentRow(0)

    def move_selected(self, direction):
        if self.txt_search.text().strip():
            QMessageBox.information(self, "提示", "請先清空搜尋關鍵字，再調整完整清單順序。")
            return
        cur_item = self.lst_chars.currentItem()
        if not cur_item:
            return
        name = cur_item.text().strip()
        if move_character_order(name, direction):
            self.refresh_list(select_name=name)

    def clear_form(self):
        self.lst_chars.clearSelection()
        self.txt_name.clear()
        self.txt_user.clear()
        self.txt_pass.clear()
        self.txt_pin.clear()
        self.txt_name.setFocus()

    def on_select_char(self, name):
        if not name:
            return
        row = fetch_character_by_name(name)
        if row:
            _, char_name, user, pswd, pin = row
            self.txt_name.setText(char_name)
            self.txt_user.setText(user)
            self.txt_pass.setText(pswd)
            self.txt_pin.setText(pin)

    def save_char(self):
        name = self.txt_name.text().strip()
        user = self.txt_user.text().strip()
        pswd = self.txt_pass.text().strip()
        pin = self.txt_pin.text().strip()
        if not name or not user or not pswd:
            QMessageBox.warning(self, "提示", "請至少填寫「角色名稱」、「帳號」與「密碼」！")
            return
        try:
            upsert_character(name, user, pswd, pin)
            self.refresh_list(select_name=name)
            QMessageBox.information(self, "完成", f"已儲存角色 [{name}] 至資料庫！")
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"儲存失敗：{e}")

    def delete_char(self):
        selected_items = self.lst_chars.selectedItems()
        names = [item.text().strip() for item in selected_items if item.text().strip()]

        # 若清單沒用 Shift/Ctrl 框選，退回檢查右側名稱欄位的單筆角色
        if not names:
            single_name = self.txt_name.text().strip()
            if single_name:
                names = [single_name]

        if not names:
            QMessageBox.information(self, "提示", "請先用滑鼠、Shift 或 Ctrl 選取要刪除的角色。")
            return

        if len(names) == 1:
            prompt = f"確定要從資料庫刪除角色 [{names[0]}] 嗎？"
        else:
            preview = "、".join(names[:6])
            extra = f" 等共 {len(names)} 隻角色" if len(names) > 6 else ""
            prompt = f"確定要批次刪除以下 {len(names)} 隻角色嗎？\n{preview}{extra}"

        ans = QMessageBox.question(self, "確認刪除", prompt)
        if ans == QMessageBox.Yes:
            for name in names:
                delete_character(name)
            self.clear_form()
            self.refresh_list()
            QMessageBox.information(self, "完成", f"已刪除 {len(names)} 筆角色資料。")

    def import_from_txt(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "選擇要匯入的 TXT 帳密檔", "", "Text Files (*.txt);;All Files (*)"
        )
        if not path:
            return

        content = None
        for enc in ("utf-8-sig", "utf-8", "cp950"):
            try:
                with open(path, "r", encoding=enc) as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue

        if content is None:
            QMessageBox.critical(self, "錯誤", "無法辨識該 TXT 檔案編碼（請確認為 UTF-8 或繁體中文 ANSI/CP950）。")
            return

        parsed_list, skipped_lines = parse_characters_from_txt_content(content)
        last_name = ""
        for c_name, u_name, pwd, pin in parsed_list:
            upsert_character(c_name, u_name, pwd, pin)
            last_name = c_name

        self.refresh_list(select_name=last_name)
        summary = f"匯入完成！共寫入 / 更新 {len(parsed_list)} 筆角色資料。"
        if skipped_lines:
            preview = "\n".join(skipped_lines[:8])
            summary += f"\n另有 {len(skipped_lines)} 行格式無法辨識已略過：\n{preview}"
        QMessageBox.information(self, "TXT 匯入結果", summary)

    def export_to_txt(self):
        default_name = f"characters_{datetime.now().strftime('%Y%m%d')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出標準格式 TXT（名稱,帳號,密碼,二道）", default_name, "Text Files (*.txt)"
        )
        if not path:
            return
        rows = fetch_all_characters()
        try:
            with open(path, "w", encoding="utf-8") as f:
                for _, c_name, u_name, pwd, pin, _ in rows:
                    f.write(f"{c_name},{u_name},{pwd},{pin}\n")
            QMessageBox.information(self, "完成", f"已匯出 {len(rows)} 筆資料至：\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"匯出失敗：{e}")

# ==========================================
# 6. PyQt5 主介面設計
# ==========================================
sell_action_labels = {}

class GameLauncher(QWidget):
    def __init__(self, version="1.0.2"):
        super().__init__()
        self.app_version = version
        self.correct_license = "8888"
        init_database()
        self.init_ui()
        self.reload_character_combos()
        self.load_config()
        signals.sell_header_signal.connect(self.update_sell_header)

    def update_sell_header(self, text, style):
        self.lbl_sell_status.setText(text)
        self.lbl_sell_status.setStyleSheet(style)

    def on_sell_map1_changed(self):
        global sell_map_mode1
        if self.rb1_map_prison.isChecked():
            sell_map_mode1 = 'prison'
            emit_action("已切換【車頭 1（左）】賣物地圖：監獄（同地圖）")
        elif self.rb1_map_cave.isChecked():
            sell_map_mode1 = 'cave'
            emit_action("已切換【車頭 1（左）】賣物地圖：中洞（同地圖）")
        else:
            sell_map_mode1 = 'phoenix'
            emit_action("已切換【車頭 1（左）】賣物地圖：鳳凰本館（跨地圖）")

    def on_sell_map2_changed(self):
        global sell_map_mode2
        if self.rb2_map_prison.isChecked():
            sell_map_mode2 = 'prison'
            emit_action("已切換【車頭 2（右）】賣物地圖：監獄（同地圖）")
        elif self.rb2_map_cave.isChecked():
            sell_map_mode2 = 'cave'
            emit_action("已切換【車頭 2（右）】賣物地圖：中洞（同地圖）")
        else:
            sell_map_mode2 = 'phoenix'
            emit_action("已切換【車頭 2（右）】賣物地圖：鳳凰本館（跨地圖）")

    def on_revive_mode_changed(self):
        global revive_mode
        if self.rb_same_map.isChecked():
            revive_mode = 'same'
            emit_action("已切換模式：【同地圖復活】，按 F7 啟動。")
        else:
            revive_mode = 'cross'
            emit_action("已切換模式：【跨地圖復活】，按 F7 啟動。")

    def update_sell_label(self, bot_id, text):
        if bot_id in sell_action_labels:
            sell_action_labels[bot_id].setText(text)

    def update_revive_label(self, bot_id, text):
        if bot_id in revive_action_labels:
            revive_action_labels[bot_id].setText(text)

    def reload_character_combos(self):
        rows = fetch_all_characters()
        self._all_char_records = [(r[1], r[2]) for r in rows]
        names = [r[1] for r in rows]
        for key, (cmb, u_edit, p_edit, pin_edit) in self.role_controls.items():
            prev = cmb.currentText().strip()
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem("")
            for n in names:
                cmb.addItem(n)
            if prev and prev in names:
                cmb.setCurrentText(prev)
            cmb.blockSignals(False)
            if cmb.currentText().strip():
                self.on_role_combo_changed(key)

    def filter_role_combo(self, key, keyword):
        cmb, _, _, _ = self.role_controls[key]
        kw = keyword.strip().lower()
        all_chars = getattr(self, "_all_char_records", [])
        if not kw:
            matched = [c_name for c_name, _ in all_chars]
        else:
            matched = [
                c_name for c_name, u_name in all_chars
                if kw in c_name.lower() or kw in u_name.lower()
            ]

        cmb.blockSignals(True)
        cmb.clear()
        cmb.addItem("")
        for name in matched:
            cmb.addItem(name)
        if cmb.lineEdit():
            cmb.lineEdit().setText(keyword)
        cmb.blockSignals(False)

        if kw and cmb.isVisible():
            cmb.showPopup()

    def on_role_combo_changed(self, key):
        cmb, u_edit, p_edit, pin_edit = self.role_controls[key]
        selected_name = cmb.currentText().strip()
        if not selected_name:
            return
        row = fetch_character_by_name(selected_name)
        if row:
            _, _, user, pswd, pin = row
            u_edit.setText(user)
            p_edit.setText(pswd)
            pin_edit.setText(pin)

    def open_char_manager(self, default_key="leader"):
        cmb, _, _, _ = self.role_controls[default_key]
        dlg = CharacterManagerDialog(self, initial_name=cmb.currentText().strip())
        dlg.exec_()
        self.reload_character_combos()
        emit_action("已重新載入角色資料庫 (accounts.db)")

    def init_ui(self):
        self.setWindowTitle("Youtube")
        self.resize(580, 610)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(4)
        self.btn_page1 = QPushButton("1.序號")
        self.btn_page2 = QPushButton("2.多開登入")
        self.btn_page3 = QPushButton("3.車頭賣物")
        self.btn_page4 = QPushButton("4.三區復活")
        self.btn_page5 = QPushButton("5.動作紀錄")

        for btn in [self.btn_page1, self.btn_page2, self.btn_page3, self.btn_page4, self.btn_page5]:
            btn.setFixedHeight(28)

        self.btn_page1.clicked.connect(lambda: self.switch_page(0))
        self.btn_page2.clicked.connect(lambda: self.switch_page(1))
        self.btn_page3.clicked.connect(lambda: self.switch_page(2))
        self.btn_page4.clicked.connect(lambda: self.switch_page(3))
        self.btn_page5.clicked.connect(lambda: self.switch_page(4))

        for btn in [self.btn_page1, self.btn_page2, self.btn_page3, self.btn_page4, self.btn_page5]:
            nav_layout.addWidget(btn)
        main_layout.addLayout(nav_layout)

        self.stacked_widget = QStackedWidget()

        # 第一頁：序號驗證
        self.page_auth = QWidget()
        auth_layout = QVBoxLayout()
        lbl_title = QLabel("<h4>請輸入認證序號</h4>")
        lbl_title.setAlignment(Qt.AlignCenter)
        self.lbl_version = QLabel(f"<small style='color: gray;'>目前版本：v{self.app_version}</small>")
        self.lbl_version.setAlignment(Qt.AlignCenter)

        self.txt_license = QLineEdit()
        self.txt_license.setEchoMode(QLineEdit.Password)
        btn_verify = QPushButton("驗證並進入總控台")
        btn_verify.setFixedHeight(32)
        btn_verify.clicked.connect(self.verify_license)

        auth_layout.addStretch()
        auth_layout.addWidget(lbl_title)
        auth_layout.addWidget(self.lbl_version)
        auth_layout.addWidget(self.txt_license)
        auth_layout.addWidget(btn_verify)
        auth_layout.addStretch()
        self.page_auth.setLayout(auth_layout)

        # 第二頁：多開登入主控面板（角色下拉選單 + 資料庫管理按鈕）
        self.page_main = QWidget()
        main_page_layout = QVBoxLayout()
        main_page_layout.setContentsMargins(4, 4, 4, 4)

        path_group = QGroupBox("遊戲路徑與角色資料庫")
        path_layout = QFormLayout()
        path_layout.setContentsMargins(6, 6, 6, 6)
        self.txt_game_path = QLineEdit(r"d:\Desktop\Rosso RAN Online [ Full Client ]\Rosso-RAN.EXE")
        path_layout.addRow("Exe:", self.txt_game_path)

        db_btn_row = QHBoxLayout()
        self.btn_manage_db = QPushButton("新增 / 修改 / 匯入TXT 角色資料庫")
        self.btn_manage_db.setStyleSheet("font-weight: bold; color: #0055aa;")
        self.btn_manage_db.clicked.connect(lambda: self.open_char_manager("leader"))
        self.btn_reload_db = QPushButton("重新讀取 DB")
        self.btn_reload_db.clicked.connect(self.reload_character_combos)
        db_btn_row.addWidget(self.btn_manage_db)
        db_btn_row.addWidget(self.btn_reload_db)
        path_layout.addRow("角色庫:", db_btn_row)
        path_group.setLayout(path_layout)
        main_page_layout.addWidget(path_group)

        acc_group = QGroupBox("下拉選單選擇角色（自動帶入帳號 / 密碼 / 二道密碼；車頭與打手互斥）")
        acc_layout = QFormLayout()
        acc_layout.setContentsMargins(6, 6, 6, 6)

        self.chk_leader = QCheckBox()
        self.cmb_leader = QComboBox()
        self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_leader2 = QCheckBox()
        self.cmb_leader2 = QComboBox()
        self.acc_leader2_user, self.acc_leader2_pass, self.acc_leader2_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_sub1 = QCheckBox()
        self.cmb_sub1 = QComboBox()
        self.acc1_user, self.acc1_pass, self.acc1_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_sub2 = QCheckBox()
        self.cmb_sub2 = QComboBox()
        self.acc2_user, self.acc2_pass, self.acc2_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_sub3 = QCheckBox()
        self.cmb_sub3 = QComboBox()
        self.acc3_user, self.acc3_pass, self.acc3_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.role_controls = {
            'leader': (self.cmb_leader, self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin),
            'leader2': (self.cmb_leader2, self.acc_leader2_user, self.acc_leader2_pass, self.acc_leader2_pin),
            'sub1': (self.cmb_sub1, self.acc1_user, self.acc1_pass, self.acc1_pin),
            'sub2': (self.cmb_sub2, self.acc2_user, self.acc2_pass, self.acc2_pin),
            'sub3': (self.cmb_sub3, self.acc3_user, self.acc3_pass, self.acc3_pin),
        }

        for k, (cmb, _, _, _) in self.role_controls.items():
            cmb.setMinimumWidth(175)
            cmb.setEditable(True)
            cmb.setInsertPolicy(QComboBox.NoInsert)
            cmb.view().setMinimumWidth(260)
            if cmb.lineEdit():
                cmb.lineEdit().setPlaceholderText("輸入關鍵字篩選...")
                cmb.lineEdit().textEdited.connect(lambda text, rk=k: self.filter_role_combo(rk, text))
            cmb.currentTextChanged.connect(lambda _, rk=k: self.on_role_combo_changed(rk))

        for chk in [self.chk_leader, self.chk_leader2, self.chk_sub1, self.chk_sub2, self.chk_sub3]:
            chk.toggled.connect(self.on_mode_toggled)

        acc_layout.addRow("F1車頭1:", self.create_role_row(self.chk_leader, self.cmb_leader, self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin))
        acc_layout.addRow("F2車頭2:", self.create_role_row(self.chk_leader2, self.cmb_leader2, self.acc_leader2_user, self.acc_leader2_pass, self.acc_leader2_pin))
        acc_layout.addRow("F1打手1:", self.create_role_row(self.chk_sub1, self.cmb_sub1, self.acc1_user, self.acc1_pass, self.acc1_pin))
        acc_layout.addRow("F2打手2:", self.create_role_row(self.chk_sub2, self.cmb_sub2, self.acc2_user, self.acc2_pass, self.acc2_pin))
        acc_layout.addRow("F3打手3:", self.create_role_row(self.chk_sub3, self.cmb_sub3, self.acc3_user, self.acc3_pass, self.acc3_pin))
        acc_group.setLayout(acc_layout)
        main_page_layout.addWidget(acc_group)

        btn_action_group = QGroupBox("遠端滑鼠控制面板")
        btn_action_layout = QHBoxLayout()
        btn_action_layout.setContentsMargins(6, 6, 6, 6)

        self.btn_ui_f1 = QPushButton("F1 單開")
        self.btn_ui_f2 = QPushButton("F2 單開")
        self.btn_ui_f3 = QPushButton("F3 單開")
        self.btn_ui_f4 = QPushButton("F4 一條龍")
        self.btn_ui_f12 = QPushButton("F12 全部中止")
        self.btn_ui_f12.setStyleSheet("color: red; font-weight: bold;")

        self.btn_ui_f1.clicked.connect(lambda: threading.Thread(target=run_f1, daemon=True).start())
        self.btn_ui_f2.clicked.connect(lambda: threading.Thread(target=run_f2, daemon=True).start())
        self.btn_ui_f3.clicked.connect(lambda: threading.Thread(target=run_single, args=('sub3',), daemon=True).start())
        self.btn_ui_f4.clicked.connect(lambda: threading.Thread(target=run_all, daemon=True).start())
        self.btn_ui_f12.clicked.connect(trigger_stop_all)

        for b in [self.btn_ui_f1, self.btn_ui_f2, self.btn_ui_f3, self.btn_ui_f4, self.btn_ui_f12]:
            b.setFixedHeight(30)
            btn_action_layout.addWidget(b)

        btn_action_group.setLayout(btn_action_layout)
        main_page_layout.addWidget(btn_action_group)
        self.page_main.setLayout(main_page_layout)

        # 第三頁：車頭自動賣東西介面
        self.page_sell = QWidget()
        sell_layout = QVBoxLayout()
        sell_layout.setContentsMargins(4, 4, 4, 4)
        sell_layout.setSpacing(4)

        tg_group = QGroupBox("Telegram、雙車頭與卡圖超時設定")
        tg_form = QFormLayout()
        tg_form.setContentsMargins(6, 4, 6, 4)
        tg_form.setVerticalSpacing(2)

        self.txt_tg_token = QLineEdit()
        self.txt_tg_chatid = QLineEdit()
        self.txt_char_name1 = QLineEdit()
        self.txt_char_name2 = QLineEdit()
        self.txt_stuck_timeout = QLineEdit("3600")
        self.txt_stuck_timeout.setFixedWidth(70)

        btn_test_tg = QPushButton("發送測試 Telegram 訊息")
        btn_test_tg.clicked.connect(self.test_telegram_msg)

        tg_form.addRow("Bot Token:", self.txt_tg_token)
        tg_form.addRow("Chat ID:", self.txt_tg_chatid)
        tg_form.addRow("車頭1角色:", self.txt_char_name1)
        tg_form.addRow("車頭2角色:", self.txt_char_name2)
        tg_form.addRow("卡圖通知秒數(預設3600):", self.txt_stuck_timeout)
        tg_form.addRow("", btn_test_tg)
        tg_group.setLayout(tg_form)
        sell_layout.addWidget(tg_group)

        sell_opt_group = QGroupBox("賣物格數與速度設定 (1=第1橫向第1格, 30=第5橫向第6格, 32=第6橫向第2格)")
        sell_opt_layout = QHBoxLayout()
        sell_opt_layout.setContentsMargins(6, 4, 6, 4)

        self.txt_sell_count = QLineEdit("30")
        self.txt_sell_speed = QLineEdit("1.0")
        self.chk_sell_reverse = QCheckBox("從尾巴往回賣")
        self.chk_sell_reverse.setChecked(True)

        for w in [self.txt_sell_count, self.txt_sell_speed]:
            w.setFixedWidth(48)
            w.setAlignment(Qt.AlignCenter)

        sell_opt_layout.addWidget(QLabel("賣幾格:"))
        sell_opt_layout.addWidget(self.txt_sell_count)
        sell_opt_layout.addWidget(QLabel("速度倍率1以下是加速:"))
        sell_opt_layout.addWidget(self.txt_sell_speed)
        sell_opt_layout.addWidget(self.chk_sell_reverse)
        sell_opt_group.setLayout(sell_opt_layout)
        sell_layout.addWidget(sell_opt_group)

        map_sell_group = QGroupBox("雙車頭賣物地圖選擇 (鳳凰本館跨地圖 / 監獄、中洞同地圖)")
        map_sell_vlayout = QVBoxLayout()
        map_sell_vlayout.setContentsMargins(6, 4, 6, 4)
        map_sell_vlayout.setSpacing(4)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("車頭1(左):"))
        self.rb1_map_phoenix = QRadioButton("鳳凰本館")
        self.rb1_map_prison = QRadioButton("監獄")
        self.rb1_map_cave = QRadioButton("中洞")
        self.rb1_map_phoenix.setChecked(True)
        self.btn_group_sell_map1 = QButtonGroup(self)
        for rb in [self.rb1_map_phoenix, self.rb1_map_prison, self.rb1_map_cave]:
            self.btn_group_sell_map1.addButton(rb)
            rb.toggled.connect(self.on_sell_map1_changed)
            row1.addWidget(rb)
        map_sell_vlayout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("車頭2(右):"))
        self.rb2_map_phoenix = QRadioButton("鳳凰本館")
        self.rb2_map_prison = QRadioButton("監獄")
        self.rb2_map_cave = QRadioButton("中洞")
        self.rb2_map_phoenix.setChecked(True)
        self.btn_group_sell_map2 = QButtonGroup(self)
        for rb in [self.rb2_map_phoenix, self.rb2_map_prison, self.rb2_map_cave]:
            self.btn_group_sell_map2.addButton(rb)
            rb.toggled.connect(self.on_sell_map2_changed)
            row2.addWidget(rb)
        map_sell_vlayout.addLayout(row2)

        delay_row = QHBoxLayout()

        phoenix_box = QGroupBox("鳳凰本館秒數")
        p_form = QFormLayout()
        p_form.setContentsMargins(5, 4, 5, 4)
        p_form.setVerticalSpacing(2)
        self.txt_p_wait_town = QLineEdit("9")
        self.txt_p_wait_walk = QLineEdit("10")
        self.txt_p_wait_s = QLineEdit("6")
        for w in [self.txt_p_wait_town, self.txt_p_wait_walk, self.txt_p_wait_s]:
            w.setFixedWidth(42)
            w.setAlignment(Qt.AlignCenter)
        p_form.addRow("按下復活後在本館等幾秒(秒):", self.txt_p_wait_town)
        p_form.addRow("地圖走路幾秒(秒):", self.txt_p_wait_walk)
        p_form.addRow("前點後等待幾秒開掛打(秒):", self.txt_p_wait_s)
        phoenix_box.setLayout(p_form)

        same_sell_box = QGroupBox("監獄/中洞同地圖秒數")
        s_form = QFormLayout()
        s_form.setContentsMargins(5, 3, 5, 3)
        s_form.setVerticalSpacing(2)
        self.txt_s_rev_w1 = QLineEdit("9")
        self.txt_s_rev_w2 = QLineEdit("13")
        self.txt_s_rev_w3 = QLineEdit("2")
        self.txt_s_sell_town = QLineEdit("9")
        self.txt_s_sell_walk = QLineEdit("10")
        self.txt_s_sell_s = QLineEdit("6")
        for w in [self.txt_s_rev_w1, self.txt_s_rev_w2, self.txt_s_rev_w3,
                  self.txt_s_sell_town, self.txt_s_sell_walk, self.txt_s_sell_s]:
            w.setFixedWidth(40)
            w.setAlignment(Qt.AlignCenter)

        s_form.addRow("死亡原地等待幾秒按復活(秒):", self.txt_s_rev_w1)
        s_form.addRow("按下復活後等幾秒按前點(秒):", self.txt_s_rev_w2)
        s_form.addRow("前點後等待幾秒開掛打(秒):", self.txt_s_rev_w3)
        s_form.addRow("包滿起點後等幾秒開地圖(秒):", self.txt_s_sell_town)
        s_form.addRow("地圖走路幾秒(秒):", self.txt_s_sell_walk)
        s_form.addRow("前點後等待幾秒開掛打(秒):", self.txt_s_sell_s)
        same_sell_box.setLayout(s_form)

        delay_row.addWidget(phoenix_box)
        delay_row.addWidget(same_sell_box)
        map_sell_vlayout.addLayout(delay_row)
        map_sell_group.setLayout(map_sell_vlayout)
        sell_layout.addWidget(map_sell_group)

        sell_status_group = QGroupBox("車頭賣東西運行狀態與即時監控")
        sell_status_layout = QVBoxLayout()
        sell_status_layout.setContentsMargins(6, 4, 6, 4)
        sell_status_layout.setSpacing(3)

        self.lbl_sell_status = QLabel("狀態：已停止 (F12)")
        self.lbl_sell_status.setStyleSheet("font-weight: bold; color: red; font-size: 10pt;")
        sell_status_layout.addWidget(self.lbl_sell_status)

        for bot_id in [1, 2]:
            lbl = QLabel(f"車頭 {bot_id}：尚未啟動")
            lbl.setStyleSheet("font-weight: bold; color: #0055aa;")
            sell_status_layout.addWidget(lbl)
            sell_action_labels[bot_id] = lbl

        sell_btn_layout = QHBoxLayout()
        self.btn_sell_f7 = QPushButton("F7 啟動車頭賣物")
        self.btn_sell_f7.setStyleSheet("color: green; font-weight: bold;")
        self.btn_sell_f12 = QPushButton("F12 中止")
        self.btn_sell_f12.setStyleSheet("color: red; font-weight: bold;")
        self.btn_sell_f7.setFixedHeight(28)
        self.btn_sell_f12.setFixedHeight(28)
        self.btn_sell_f7.clicked.connect(trigger_start_f7)
        self.btn_sell_f12.clicked.connect(trigger_stop_all)

        sell_btn_layout.addWidget(self.btn_sell_f7)
        sell_btn_layout.addWidget(self.btn_sell_f12)
        sell_status_layout.addLayout(sell_btn_layout)
        sell_status_group.setLayout(sell_status_layout)
        sell_layout.addWidget(sell_status_group)
        self.page_sell.setLayout(sell_layout)

        # 第四頁：三區自動復活介面
        self.page_revive = QWidget()
        revive_layout = QVBoxLayout()
        revive_layout.setContentsMargins(4, 4, 4, 4)

        revive_setting_group = QGroupBox("三區 Telegram 與角色設定")
        revive_setting_form = QFormLayout()
        revive_setting_form.setContentsMargins(6, 6, 6, 6)

        self.txt_revive_tg_token = QLineEdit()
        self.txt_revive_tg_chatid = QLineEdit()
        self.txt_revive_char1 = QLineEdit()
        self.txt_revive_char2 = QLineEdit()
        self.txt_revive_char3 = QLineEdit()

        revive_setting_form.addRow("Token:", self.txt_revive_tg_token)
        revive_setting_form.addRow("Chat ID:", self.txt_revive_tg_chatid)
        revive_setting_form.addRow("1區角色:", self.txt_revive_char1)
        revive_setting_form.addRow("2區角色:", self.txt_revive_char2)
        revive_setting_form.addRow("3區角色:", self.txt_revive_char3)
        revive_setting_group.setLayout(revive_setting_form)
        revive_layout.addWidget(revive_setting_group)

        mode_group = QGroupBox("復活模式與自訂延遲秒數 (請勾選後按 F7 啟動)")
        mode_main_layout = QHBoxLayout()
        mode_main_layout.setContentsMargins(6, 6, 6, 6)

        same_col = QVBoxLayout()
        self.rb_same_map = QRadioButton("同地圖復活")
        self.rb_same_map.setChecked(True)
        same_col.addWidget(self.rb_same_map)

        same_form = QFormLayout()
        self.txt_same_wait1 = QLineEdit("9")
        self.txt_same_wait2 = QLineEdit("13")
        self.txt_same_wait3 = QLineEdit("2")
        for w in [self.txt_same_wait1, self.txt_same_wait2, self.txt_same_wait3]:
            w.setFixedWidth(45)
            w.setAlignment(Qt.AlignCenter)

        same_form.addRow("死亡原地等待幾秒按復活(秒):", self.txt_same_wait1)
        same_form.addRow("按下復活後等幾秒按前點(秒):", self.txt_same_wait2)
        same_form.addRow("前點後等待幾秒開掛打(秒):", self.txt_same_wait3)
        same_col.addLayout(same_form)

        cross_col = QVBoxLayout()
        self.rb_cross_map = QRadioButton("跨地圖復活")
        cross_col.addWidget(self.rb_cross_map)

        cross_form = QFormLayout()
        self.txt_cross_wait1 = QLineEdit("20")
        self.txt_cross_wait2 = QLineEdit("6")
        for w in [self.txt_cross_wait1, self.txt_cross_wait2]:
            w.setFixedWidth(45)
            w.setAlignment(Qt.AlignCenter)

        cross_form.addRow("按下復活後等幾秒按前點(秒):", self.txt_cross_wait1)
        cross_form.addRow("前點後等待幾秒開掛打(秒):", self.txt_cross_wait2)
        cross_col.addLayout(cross_form)
        cross_col.addStretch()

        self.btn_group_mode = QButtonGroup(self)
        self.btn_group_mode.addButton(self.rb_same_map)
        self.btn_group_mode.addButton(self.rb_cross_map)
        self.rb_same_map.toggled.connect(self.on_revive_mode_changed)

        mode_main_layout.addLayout(same_col)
        mode_main_layout.addLayout(cross_col)
        mode_group.setLayout(mode_main_layout)
        revive_layout.addWidget(mode_group)

        revive_status_group = QGroupBox("三區復活狀態與即時監控")
        revive_status_layout = QVBoxLayout()
        revive_status_layout.setContentsMargins(6, 6, 6, 6)

        self.lbl_revive_status = QLabel("狀態：已停止 (F12)")
        self.lbl_revive_status.setStyleSheet("font-weight: bold; color: red; font-size: 11pt;")
        revive_status_layout.addWidget(self.lbl_revive_status)

        for bot_id in [1, 2, 3]:
            lbl = QLabel(f"角色 {bot_id}：尚未啟動")
            lbl.setStyleSheet("font-weight: bold; color: #0055aa;")
            revive_status_layout.addWidget(lbl)
            revive_action_labels[bot_id] = lbl

        revive_btn_layout = QHBoxLayout()
        self.btn_revive_f7 = QPushButton("F7 啟動三區復活")
        self.btn_revive_f7.setStyleSheet("color: green; font-weight: bold;")
        self.btn_revive_f12 = QPushButton("F12 中止")
        self.btn_revive_f12.setStyleSheet("color: red; font-weight: bold;")
        self.btn_revive_f7.setFixedHeight(32)
        self.btn_revive_f12.setFixedHeight(32)
        self.btn_revive_f7.clicked.connect(trigger_start_f7)
        self.btn_revive_f12.clicked.connect(trigger_stop_all)

        revive_btn_layout.addWidget(self.btn_revive_f7)
        revive_btn_layout.addWidget(self.btn_revive_f12)
        revive_status_layout.addLayout(revive_btn_layout)

        lbl_revive_help = QLabel("<small>• 按下 F7：啟動當前勾選之模式監控 | F12：中止所有動作</small>")
        revive_status_layout.addWidget(lbl_revive_help)
        revive_status_group.setLayout(revive_status_layout)
        revive_layout.addWidget(revive_status_group)

        self.page_revive.setLayout(revive_layout)

        # 第五頁：完整動作紀錄（抓背包跑掉與卡關步驟）
        self.page_history = QWidget()
        history_layout = QVBoxLayout()
        history_layout.setContentsMargins(4, 4, 4, 4)

        history_group = QGroupBox("輔助完整動作紀錄（含時間戳記、座標與狀態）")
        hg_layout = QVBoxLayout()
        self.txt_history = QPlainTextEdit()
        self.txt_history.setReadOnly(True)
        self.txt_history.setMaximumBlockCount(3000)
        self.txt_history.setStyleSheet("font-family: Consolas, 'Microsoft JhengHei'; font-size: 9pt;")
        hg_layout.addWidget(self.txt_history)

        h_btn_row = QHBoxLayout()
        self.chk_auto_scroll = QCheckBox("自動捲動至最新")
        self.chk_auto_scroll.setChecked(True)

        btn_clear_log = QPushButton("清空紀錄")
        btn_clear_log.clicked.connect(self.txt_history.clear)

        btn_export_log = QPushButton("匯出 TXT")
        btn_export_log.clicked.connect(self.export_history_log)

        h_btn_row.addWidget(self.chk_auto_scroll)
        h_btn_row.addStretch()
        h_btn_row.addWidget(btn_clear_log)
        h_btn_row.addWidget(btn_export_log)
        hg_layout.addLayout(h_btn_row)
        history_group.setLayout(hg_layout)
        history_layout.addWidget(history_group)
        self.page_history.setLayout(history_layout)

        self.stacked_widget.addWidget(self.page_auth)
        self.stacked_widget.addWidget(self.page_main)
        self.stacked_widget.addWidget(self.page_sell)
        self.stacked_widget.addWidget(self.page_revive)
        self.stacked_widget.addWidget(self.page_history)

        main_layout.addWidget(self.stacked_widget)

        self.txt_log = QLineEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("系統訊息提示...")
        main_layout.addWidget(self.txt_log)

        self.setLayout(main_layout)
        signals.log_signal.connect(self.update_log)
        signals.sell_status_signal.connect(self.update_sell_label)
        signals.revive_status_signal.connect(self.update_revive_label)

    def create_role_row(self, chk, cmb, u_edit, p_edit, pin_edit):
        box = QWidget()
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        u_edit.setPlaceholderText("帳號")
        p_edit.setPlaceholderText("密碼")
        pin_edit.setPlaceholderText("二道")
        pin_edit.setFixedWidth(56)
        lay.addWidget(chk)
        lay.addWidget(cmb)
        lay.addWidget(u_edit)
        lay.addWidget(p_edit)
        lay.addWidget(pin_edit)
        box.setLayout(lay)
        return box

    def switch_page(self, index):
        if index >= 1:
            if self.stacked_widget.currentIndex() == 0:
                QMessageBox.warning(self, "提示", "請先通過第一頁序號驗證！")
                return
        self.stacked_widget.setCurrentIndex(index)

    def on_mode_toggled(self):
        is_leader_checked = self.chk_leader.isChecked() or self.chk_leader2.isChecked()
        is_sub_checked = self.chk_sub1.isChecked() or self.chk_sub2.isChecked() or self.chk_sub3.isChecked()

        if is_leader_checked:
            self.chk_sub1.setChecked(False)
            self.chk_sub2.setChecked(False)
            self.chk_sub3.setChecked(False)
            self.chk_sub1.setEnabled(False)
            self.chk_sub2.setEnabled(False)
            self.chk_sub3.setEnabled(False)
            emit_action("切換為【車頭模式】：打手已鎖定。")
        elif is_sub_checked:
            self.chk_leader.setChecked(False)
            self.chk_leader2.setChecked(False)
            self.chk_leader.setEnabled(False)
            self.chk_leader2.setEnabled(False)
            emit_action("切換為【打手模式】：車頭已鎖定。")
        else:
            self.chk_leader.setEnabled(True)
            self.chk_leader2.setEnabled(True)
            self.chk_sub1.setEnabled(True)
            self.chk_sub2.setEnabled(True)
            self.chk_sub3.setEnabled(True)
            emit_action("請勾選車頭或打手。")

    def verify_license(self):
        entered = self.txt_license.text().strip()
        if entered == self.correct_license:
            self.stacked_widget.setCurrentIndex(1)
            emit_action("驗證成功！已解鎖各功能分頁。")
        else:
            QMessageBox.warning(self, "錯誤", "序號錯誤！請重新輸入。")

    def update_log(self, text):
        self.txt_log.setText(text)
        if hasattr(self, "txt_history"):
            self.txt_history.appendPlainText(text)
            if self.chk_auto_scroll.isChecked():
                sb = self.txt_history.verticalScrollBar()
                sb.setValue(sb.maximum())
        print(text)

    def export_history_log(self):
        default_name = f"action_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(self, "匯出動作紀錄", default_name, "Text Files (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.txt_history.toPlainText())
                QMessageBox.information(self, "完成", f"已匯出動作紀錄至：\n{path}")
            except Exception as e:
                QMessageBox.critical(self, "錯誤", f"匯出失敗：{e}")

    def test_telegram_msg(self):
        token = self.txt_tg_token.text().strip()
        chatid = self.txt_tg_chatid.text().strip()
        if not token or not chatid:
            QMessageBox.warning(self, "提示", "請輸入完整的 Bot Token 與 Chat ID！")
            return

        cname1 = self.txt_char_name1.text().strip()
        cname2 = self.txt_char_name2.text().strip()

        sent_msgs = []
        if cname1:
            msg1 = f"{cname1}斷線"
            send_telegram_notification(token, chatid, msg1)
            sent_msgs.append(msg1)
        if cname2:
            msg2 = f"{cname2}斷線"
            send_telegram_notification(token, chatid, msg2)
            sent_msgs.append(msg2)

        if not sent_msgs:
            default_msg = "測試車頭1斷線 / 測試車頭2斷線"
            send_telegram_notification(token, chatid, default_msg)
            sent_msgs.append(default_msg)

        QMessageBox.information(self, "成功", "已發送測試 Telegram 訊息：\n" + "\n".join(sent_msgs))

    def get_acc_data(self, key_type):
        mapping = {
            'leader': (self.chk_leader, self.cmb_leader, self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin, 0, 0),
            'leader2': (self.chk_leader2, self.cmb_leader2, self.acc_leader2_user, self.acc_leader2_pass, self.acc_leader2_pin, 960, 0),
            'sub1': (self.chk_sub1, self.cmb_sub1, self.acc1_user, self.acc1_pass, self.acc1_pin, -321, 0),
            'sub2': (self.chk_sub2, self.cmb_sub2, self.acc2_user, self.acc2_pass, self.acc2_pin, 297, 0),
            'sub3': (self.chk_sub3, self.cmb_sub3, self.acc3_user, self.acc3_pass, self.acc3_pin, 896, 0),
        }
        chk, cmb, u_edit, p_edit, pin_edit, x, y = mapping[key_type]
        acc = [u_edit.text().strip(), p_edit.text().strip(), pin_edit.text().strip()]
        return acc, x, y, chk.isChecked(), True, cmb.currentText().strip()

    def load_config(self):
        global sell_map_mode1, sell_map_mode2
        if not os.path.exists(CONFIG_FILE):
            self.chk_leader.setChecked(True)
            self.chk_leader2.setChecked(True)
            return
        config = configparser.ConfigParser()
        try:
            config.read(CONFIG_FILE, encoding='utf-8')
            if 'SETTINGS' in config:
                sec = config['SETTINGS']
                self.txt_game_path.setText(sec.get('game_path', self.txt_game_path.text()))

                role_specs = [
                    ('leader', self.chk_leader, self.cmb_leader, self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin, True),
                    ('leader2', self.chk_leader2, self.cmb_leader2, self.acc_leader2_user, self.acc_leader2_pass, self.acc_leader2_pin, True),
                    ('sub1', self.chk_sub1, self.cmb_sub1, self.acc1_user, self.acc1_pass, self.acc1_pin, False),
                    ('sub2', self.chk_sub2, self.cmb_sub2, self.acc2_user, self.acc2_pass, self.acc2_pin, False),
                    ('sub3', self.chk_sub3, self.cmb_sub3, self.acc3_user, self.acc3_pass, self.acc3_pin, False),
                ]
                for prefix, chk, cmb, u_ed, p_ed, pin_ed, default_chk in role_specs:
                    chk.setChecked(sec.getboolean(f'chk_{prefix}', default_chk))
                    saved_char = sec.get(f'{prefix}_char_name', '')
                    u_ed.setText(sec.get(f'{prefix}_user', ''))
                    p_ed.setText(sec.get(f'{prefix}_pass', ''))
                    pin_ed.setText(sec.get(f'{prefix}_pin', ''))
                    if saved_char:
                        if cmb.findText(saved_char) == -1:
                            cmb.addItem(saved_char)
                        cmb.setCurrentText(saved_char)
                        self.on_role_combo_changed(prefix)

                self.txt_tg_token.setText(sec.get('tg_token', ''))
                self.txt_tg_chatid.setText(sec.get('tg_chatid', ''))
                self.txt_char_name1.setText(sec.get('char_name1', ''))
                self.txt_char_name2.setText(sec.get('char_name2', ''))
                self.txt_stuck_timeout.setText(sec.get('stuck_timeout', '3600'))

                self.txt_sell_count.setText(sec.get('sell_slot_count', '30'))
                self.txt_sell_speed.setText(sec.get('sell_speed_scale', '1.0'))
                self.chk_sell_reverse.setChecked(sec.getboolean('sell_reverse', True))

                saved_map1 = sec.get('sell_map_mode1', sec.get('sell_map_mode', 'phoenix'))
                if saved_map1 == 'prison':
                    self.rb1_map_prison.setChecked(True)
                    sell_map_mode1 = 'prison'
                elif saved_map1 == 'cave':
                    self.rb1_map_cave.setChecked(True)
                    sell_map_mode1 = 'cave'
                else:
                    self.rb1_map_phoenix.setChecked(True)
                    sell_map_mode1 = 'phoenix'

                saved_map2 = sec.get('sell_map_mode2', sec.get('sell_map_mode', 'phoenix'))
                if saved_map2 == 'prison':
                    self.rb2_map_prison.setChecked(True)
                    sell_map_mode2 = 'prison'
                elif saved_map2 == 'cave':
                    self.rb2_map_cave.setChecked(True)
                    sell_map_mode2 = 'cave'
                else:
                    self.rb2_map_phoenix.setChecked(True)
                    sell_map_mode2 = 'phoenix'

                self.txt_p_wait_town.setText(sec.get('p_wait_town', '9'))
                self.txt_p_wait_walk.setText(sec.get('p_wait_walk', '10'))
                self.txt_p_wait_s.setText(sec.get('p_wait_s', '6'))

                self.txt_s_rev_w1.setText(sec.get('s_rev_w1', '9'))
                self.txt_s_rev_w2.setText(sec.get('s_rev_w2', '13'))
                self.txt_s_rev_w3.setText(sec.get('s_rev_w3', '2'))
                self.txt_s_sell_town.setText(sec.get('s_sell_town', '9'))
                self.txt_s_sell_walk.setText(sec.get('s_sell_walk', '10'))
                self.txt_s_sell_s.setText(sec.get('s_sell_s', '6'))

                self.txt_revive_tg_token.setText(sec.get('revive_tg_token', ''))
                self.txt_revive_tg_chatid.setText(sec.get('revive_tg_chatid', ''))
                self.txt_revive_char1.setText(sec.get('revive_char1', ''))
                self.txt_revive_char2.setText(sec.get('revive_char2', ''))
                self.txt_revive_char3.setText(sec.get('revive_char3', ''))

                self.txt_same_wait1.setText(sec.get('same_wait1', '9'))
                self.txt_same_wait2.setText(sec.get('same_wait2', '13'))
                self.txt_same_wait3.setText(sec.get('same_wait3', '2'))
                self.txt_cross_wait1.setText(sec.get('cross_wait1', '20'))
                self.txt_cross_wait2.setText(sec.get('cross_wait2', '6'))
        except Exception:
            pass

    def save_config(self):
        config = configparser.ConfigParser()
        config['SETTINGS'] = {
            'game_path': self.txt_game_path.text(),
            'chk_leader': str(self.chk_leader.isChecked()),
            'leader_char_name': self.cmb_leader.currentText(),
            'leader_user': self.acc_leader_user.text(),
            'leader_pass': self.acc_leader_pass.text(),
            'leader_pin': self.acc_leader_pin.text(),

            'chk_leader2': str(self.chk_leader2.isChecked()),
            'leader2_char_name': self.cmb_leader2.currentText(),
            'leader2_user': self.acc_leader2_user.text(),
            'leader2_pass': self.acc_leader2_pass.text(),
            'leader2_pin': self.acc_leader2_pin.text(),

            'chk_sub1': str(self.chk_sub1.isChecked()),
            'sub1_char_name': self.cmb_sub1.currentText(),
            'sub1_user': self.acc1_user.text(),
            'sub1_pass': self.acc1_pass.text(),
            'sub1_pin': self.acc1_pin.text(),

            'chk_sub2': str(self.chk_sub2.isChecked()),
            'sub2_char_name': self.cmb_sub2.currentText(),
            'sub2_user': self.acc2_user.text(),
            'sub2_pass': self.acc2_pass.text(),
            'sub2_pin': self.acc2_pin.text(),

            'chk_sub3': str(self.chk_sub3.isChecked()),
            'sub3_char_name': self.cmb_sub3.currentText(),
            'sub3_user': self.acc3_user.text(),
            'sub3_pass': self.acc3_pass.text(),
            'sub3_pin': self.acc3_pin.text(),

            'tg_token': self.txt_tg_token.text(),
            'tg_chatid': self.txt_tg_chatid.text(),
            'char_name1': self.txt_char_name1.text(),
            'char_name2': self.txt_char_name2.text(),
            'stuck_timeout': self.txt_stuck_timeout.text(),

            'sell_slot_count': self.txt_sell_count.text(),
            'sell_speed_scale': self.txt_sell_speed.text(),
            'sell_reverse': str(self.chk_sell_reverse.isChecked()),

            'sell_map_mode1': sell_map_mode1,
            'sell_map_mode2': sell_map_mode2,
            'p_wait_town': self.txt_p_wait_town.text(),
            'p_wait_walk': self.txt_p_wait_walk.text(),
            'p_wait_s': self.txt_p_wait_s.text(),

            's_rev_w1': self.txt_s_rev_w1.text(),
            's_rev_w2': self.txt_s_rev_w2.text(),
            's_rev_w3': self.txt_s_rev_w3.text(),
            's_sell_town': self.txt_s_sell_town.text(),
            's_sell_walk': self.txt_s_sell_walk.text(),
            's_sell_s': self.txt_s_sell_s.text(),

            'revive_tg_token': self.txt_revive_tg_token.text(),
            'revive_tg_chatid': self.txt_revive_tg_chatid.text(),
            'revive_char1': self.txt_revive_char1.text(),
            'revive_char2': self.txt_revive_char2.text(),
            'revive_char3': self.txt_revive_char3.text(),

            'same_wait1': self.txt_same_wait1.text(),
            'same_wait2': self.txt_same_wait2.text(),
            'same_wait3': self.txt_same_wait3.text(),
            'cross_wait1': self.txt_cross_wait1.text(),
            'cross_wait2': self.txt_cross_wait2.text(),
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                config.write(f)
        except Exception:
            pass

    def closeEvent(self, event):
        self.save_config()
        event.accept()

# ==========================================
# 7. 全域熱鍵管理與 F7/F12 控制
# ==========================================
def run_f1():
    global stop_flag, window
    if not window:
        return
    stop_flag = False
    window.save_config()
    game_path = window.txt_game_path.text()

    if window.chk_leader.isChecked():
        acc, x, y, _, move_win, cname = window.get_acc_data('leader')
    elif window.chk_sub1.isChecked():
        acc, x, y, _, move_win, cname = window.get_acc_data('sub1')
    else:
        emit_action("⚠️ F1 單開：請先勾選 F1車頭1 或 F1打手1！")
        return

    if acc[0]:
        login_flow(acc, x, y, game_path, move_window=move_win, char_label=cname)

def run_f2():
    global stop_flag, window
    if not window:
        return
    stop_flag = False
    window.save_config()
    game_path = window.txt_game_path.text()

    if window.chk_leader2.isChecked():
        acc, x, y, _, move_win, cname = window.get_acc_data('leader2')
    elif window.chk_sub2.isChecked():
        acc, x, y, _, move_win, cname = window.get_acc_data('sub2')
    else:
        emit_action("⚠️ F2 單開：請先勾選 F2車頭2 或 F2打手2！")
        return

    if acc[0]:
        login_flow(acc, x, y, game_path, move_window=move_win, char_label=cname)

def run_single(key_type):
    global stop_flag, window
    if not window:
        return
    stop_flag = False
    window.save_config()
    if window.chk_leader.isChecked():
        return
    acc, x, y, _, move_win, cname = window.get_acc_data(key_type)
    game_path = window.txt_game_path.text()
    if not acc[0]:
        return
    login_flow(acc, x, y, game_path, move_window=move_win, char_label=cname)

def run_all():
    global stop_flag, window
    if not window:
        return
    stop_flag = False
    window.save_config()
    game_path = window.txt_game_path.text()

    roles = ['leader', 'leader2', 'sub1', 'sub2', 'sub3']
    for role in roles:
        if stop_flag:
            break
        acc, x, y, checked, move_win, cname = window.get_acc_data(role)
        if checked and acc[0]:
            success = login_flow(acc, x, y, game_path, move_window=move_win, char_label=cname)
            if not success:
                break

def align_three_zone_windows():
    target_coords = [(-321, 0), (297, 0), (896, 0)]
    game_wins = []

    for win in gw.getAllWindows():
        title_up = win.title.upper()
        if "RAN" in title_up or "ROSSO" in title_up:
            if "自動化總控台" in win.title:
                continue
            game_wins.append(win)

    if not game_wins:
        emit_action("⚠️ 視窗歸位警告：找不到任何遊戲視窗！")
        return

    for w in game_wins:
        try:
            if win32gui.IsIconic(w._hWnd):
                win32gui.ShowWindow(w._hWnd, win32con.SW_RESTORE)
                time.sleep(0.15)
        except Exception:
            pass

    game_wins.sort(key=lambda w: w.left)

    for idx, (tx, ty) in enumerate(target_coords):
        if idx < len(game_wins):
            hwnd = game_wins[idx]._hWnd
            try:
                win32gui.SetWindowPos(
                    hwnd, 0, int(tx), int(ty), 0, 0,
                    win32con.SWP_NOSIZE | win32con.SWP_NOZORDER
                )
            except Exception as e:
                emit_action(f"⚠️ 視窗 {idx + 1} 歸位失敗: {e}")

    emit_action("✅ 已自動將 3 個遊戲視窗還原並歸位至指定位置！")

def parse_sec(line_edit, default_val):
    try:
        val = float(line_edit.text().strip())
        return val if val >= 0 else default_val
    except Exception:
        line_edit.setText(str(default_val))
        return default_val

def trigger_start_f7():
    global auto_sell_running, revive_running, IS_NOTIFIED, stop_flag, revive_mode, sell_map_mode1, sell_map_mode2, window
    if not window:
        return
    stop_flag = False
    current_tab = window.stacked_widget.currentIndex()

    if current_tab == 2:
        if revive_running:
            emit_action("⚠️ 【互斥警告】三區自動復活正在運行中，無法同時啟動車頭賣物！")
            return

        auto_sell_running = False
        time.sleep(0.2)

        stop_flag = False
        auto_sell_running = True

        token = window.txt_tg_token.text().strip()
        chatid = window.txt_tg_chatid.text().strip()
        if not token or not chatid:
            emit_action("⚠️ 錯誤：請先於第三頁設定 Telegram Token 與 Chat ID！")
            auto_sell_running = False
            return

        cname1 = window.txt_char_name1.text().strip()
        cname2 = window.txt_char_name2.text().strip()

        game_wins = []
        for win in gw.getAllWindows():
            title_up = win.title.upper()
            if "RAN" in title_up or "ROSSO" in title_up:
                if win.width > 200 and "YOUTUBE" not in title_up:
                    game_wins.append(win)

        game_wins.sort(key=lambda w: w.left)

        if not game_wins:
            emit_action("⚠️ 錯誤：畫面上找不到任何遊戲視窗！")
            auto_sell_running = False
            return

        p_town = parse_sec(window.txt_p_wait_town, 9.0)
        p_walk = parse_sec(window.txt_p_wait_walk, 10.0)
        p_s = parse_sec(window.txt_p_wait_s, 6.0)

        s_rev1 = parse_sec(window.txt_s_rev_w1, 9.0)
        s_rev2 = parse_sec(window.txt_s_rev_w2, 13.0)
        s_rev3 = parse_sec(window.txt_s_rev_w3, 2.0)
        s_town = parse_sec(window.txt_s_sell_town, 9.0)
        s_walk = parse_sec(window.txt_s_sell_walk, 10.0)
        s_s = parse_sec(window.txt_s_sell_s, 6.0)

        sell_count = max(1, int(parse_sec(window.txt_sell_count, 30.0)))
        sell_speed = max(0.2, parse_sec(window.txt_sell_speed, 1.0))
        sell_rev = window.chk_sell_reverse.isChecked()
        stuck_sec = max(1.0, parse_sec(window.txt_stuck_timeout, 3600.0))

        window.save_config()

        desc1 = SAME_MAP_CONFIGS[sell_map_mode1]['label'] if sell_map_mode1 in SAME_MAP_CONFIGS else "鳳凰本館"
        desc2 = SAME_MAP_CONFIGS[sell_map_mode2]['label'] if sell_map_mode2 in SAME_MAP_CONFIGS else "鳳凰本館"
        signals.sell_header_signal.emit(
            f"狀態：車頭1【{desc1}】/ 車頭2【{desc2}】監控中 (F7)",
            "font-weight: bold; color: green; font-size: 10pt;"
        )

        target_positions = [(-46, 0), (931, 0)]
        names = [cname1, cname2]
        bot_modes = [sell_map_mode1, sell_map_mode2]

        for idx, win in enumerate(game_wins[:2]):
            bot_id = idx + 1
            bx, by = target_positions[idx]
            cname = names[idx]
            chosen_mode = bot_modes[idx] if idx < len(bot_modes) else 'phoenix'

            try:
                if win.isMinimized:
                    win.restore()
                win.moveTo(bx, by)
                emit_action(f"車頭 {bot_id} 已自動歸位至座標: ({bx}, {by})")
            except Exception as e:
                emit_action(f"車頭 {bot_id} 歸位失敗: {e}")

            threading.Thread(
                target=run_auto_sell_bot,
                args=(bot_id, token, chatid, bx, by, cname, chosen_mode,
                        p_town, p_walk, p_s,
                        s_rev1, s_rev2, s_rev3, s_town, s_walk, s_s,
                        sell_count, sell_rev, sell_speed, stuck_sec),
                daemon=True
          ).start()

        emit_action(f"【F7】雙車頭監控已啟動！（左車頭1：{desc1}／右車頭2：{desc2}）")

    elif current_tab == 3:
        if auto_sell_running:
            emit_action("⚠️ 【互斥警告】車頭自動賣東西正在運行中，無法同時啟動三區復活！")
            return
        if not revive_running:
            token = window.txt_revive_tg_token.text().strip()
            chatid = window.txt_revive_tg_chatid.text().strip()
            if not token or not chatid:
                emit_action("⚠️ 錯誤：請先於第四頁設定 Telegram Token 與 Chat ID！")
                return

            align_three_zone_windows()
            time.sleep(0.3)

            s_w1 = parse_sec(window.txt_same_wait1, 9.0)
            s_w2 = parse_sec(window.txt_same_wait2, 13.0)
            s_w3 = parse_sec(window.txt_same_wait3, 2.0)
            c_w1 = parse_sec(window.txt_cross_wait1, 20.0)
            c_w2 = parse_sec(window.txt_cross_wait2, 6.0)

            window.save_config()

            char1 = window.txt_revive_char1.text().strip()
            char2 = window.txt_revive_char2.text().strip()
            char3 = window.txt_revive_char3.text().strip()

            revive_running = True
            mode_desc = "同地圖" if revive_mode == "same" else "跨地圖"
            window.lbl_revive_status.setText(f"狀態：3 角色【{mode_desc}】復活監控中 (F7)")
            window.lbl_revive_status.setStyleSheet("font-weight: bold; color: green; font-size: 11pt;")

            bot_configs = [
                (1, -321, 0, char1),
                (2, 297, 0, char2),
                (3, 896, 0, char3)
            ]
            for bot_id, bx, by, cname in bot_configs:
                revive_thread = ReviveBotThread(
                    bot_id=bot_id,
                    bot_token=token,
                    chat_id=chatid,
                    base_x=bx,
                    base_y=by,
                    char_name=cname,
                    mode=revive_mode,
                    same_wait_before=s_w1,
                    same_wait_after_revive=s_w2,
                    same_wait_after_card=s_w3,
                    cross_wait_after_revive=c_w1,
                    cross_wait_after_card=c_w2
                )
                revive_thread.start()
            emit_action(f"【F7】已啟動三區【{mode_desc}】自動復活監控！")

def trigger_stop_all():
    global stop_flag, auto_sell_running, revive_running, window
    stop_flag = True
    auto_sell_running = False
    revive_running = False

    if window:
        signals.sell_header_signal.emit(
            "狀態：已停止 (F12)",
            "font-weight: bold; color: red; font-size: 11pt;"
        )
        window.lbl_revive_status.setText("狀態：已停止 (F12)")
        window.lbl_revive_status.setStyleSheet("font-weight: bold; color: red; font-size: 11pt;")
        for bot_id in [1, 2, 3]:
            signals.revive_status_signal.emit(bot_id, f"角色 {bot_id}：已停止")

    emit_action("【F12】已緊急中止所有登入、車頭賣裝與三區復活動作！")

def pynput_on_press(key):
    global window
    if not window or window.stacked_widget.currentIndex() != 1:
        return
    try:
        is_leader_mode = window.chk_leader.isChecked() or window.chk_leader2.isChecked()
        if key == pynput_keyboard.Key.f12:
            trigger_stop_all()
        elif key == pynput_keyboard.Key.f1:
            if is_leader_mode:
                threading.Thread(target=run_f1, daemon=True).start()
            else:
                threading.Thread(target=run_single, args=('sub1',), daemon=True).start()
        elif key == pynput_keyboard.Key.f2:
            if is_leader_mode:
                threading.Thread(target=run_f2, daemon=True).start()
            else:
                threading.Thread(target=run_single, args=('sub2',), daemon=True).start()
        elif key == pynput_keyboard.Key.f3:
            threading.Thread(target=run_single, args=('sub3',), daemon=True).start()
        elif key == pynput_keyboard.Key.f4:
            threading.Thread(target=run_all, daemon=True).start()
    except Exception:
        pass

if __name__ == "__main__":
    if not is_admin():
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit()

    app = QApplication(sys.argv)
    window = GameLauncher()
    window.show()

    pynput_listener = pynput_keyboard.Listener(on_press=pynput_on_press)
    pynput_listener.start()

    kb_global.add_hotkey('f7', trigger_start_f7)
    kb_global.add_hotkey('f12', trigger_stop_all)

    sys.exit(app.exec_())