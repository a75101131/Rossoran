import sys
import os
import time
import subprocess
import threading
import configparser
import ctypes
import requests
import pyautogui
import pydirectinput
import cv2
import numpy as np
from mss import mss
import pygetwindow as gw
import win32gui
import win32con
import win32api
from pynput import keyboard as pynput_keyboard
import keyboard as kb_global

from PyQt5.QtWidgets import (
    QApplication, QWidget, QStackedWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QMessageBox, QFormLayout, QGroupBox, QCheckBox,
    QRadioButton, QButtonGroup
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject

# 停用 Safety 檢查以利自動化連續控制
pyautogui.FAILSAFE = False
pydirectinput.FAILSAFE = False

CONFIG_FILE = "config.ini"

# 全域變數宣告
window = None
auto_sell_running = False
revive_running = False
stop_flag = False
IS_NOTIFIED = False
revive_mode = 'same'  # 'same' 或 'cross'

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
    revive_status_signal = pyqtSignal(int, str)

signals = WorkerSignals()

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
        screenshot = pyautogui.screenshot(region=(rx, ry, rw, rh))
        screenshot_cv = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        template = cv2.imread(pic_path, cv2.IMREAD_UNCHANGED)
        if template is None:
            return None
        h, w = template.shape[:2]

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
            pyautogui.moveTo(active_win.left + rel_x, active_win.top + rel_y, duration=0.1)
            pyautogui.mouseDown(button='left')
            time.sleep(0.08)
            pyautogui.mouseUp(button='left')
        else:
            pyautogui.click(rel_x, rel_y)
    except Exception as e:
        signals.log_signal.emit(f"點擊異常: {e}")
        pyautogui.click(rel_x, rel_y)

def wait_image_in_window(img_filename, timeout_sec, delay_after_found=4):
    global stop_flag
    img_path = get_resource_path(os.path.join("assets", img_filename))
    if not os.path.exists(img_path):
        img_path = get_resource_path(img_filename)
    if not os.path.exists(img_path):
        signals.log_signal.emit(f"錯誤: 找不到圖片檔案 -> {img_filename}")
        return False, 0, 0

    signals.log_signal.emit(f"正在等待畫面圖片: {img_filename}...")
    start_time = time.time()
    
    with mss() as sct:
        while time.time() - start_time < timeout_sec:
            if stop_flag:
                return False, 0, 0
            
            # 💡 拿掉這裡強制搶焦點的動作，改成直接抓取當前活動視窗或全螢幕監控
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

            match_pos = find_pic_in_region_sell(img_filename, (monitor["left"], monitor["top"], monitor["width"], monitor["height"]), threshold=0.72, get_center=True)
            if match_pos:
                signals.log_signal.emit(f"偵測到 {img_filename}！緩衝延遲 {delay_after_found} 秒...")
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

def login_flow(acc_data, target_x, target_y, game_path, move_window=True):
    global stop_flag
    if stop_flag:
        return False

    user, pswd, pin = acc_data
    signals.log_signal.emit(f"開始執行帳號 [{user}] 登入流程...")

    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", game_path, None, os.path.dirname(game_path), 1)
    except Exception as e:
        signals.log_signal.emit(f"遊戲啟動失敗: {e}")
        return False

    found, img_x, img_y = wait_image_in_window("ServerText.png", 300, delay_after_found=4)
    if not found: return False
    pyautogui.click(img_x + 50, img_y)
    time.sleep(0.8)

    win_click(497, 406); time.sleep(1.0)
    win_click(707, 407); time.sleep(1.0)
    win_click(770, 512); time.sleep(1.0)

    found, _, _ = wait_image_in_window("AccountText.png", 300, delay_after_found=3)
    if not found: return False

    win_click(519, 349); time.sleep(0.5)
    type_string(user); time.sleep(0.8)
    send_scancode(SCANCODE_TAB); time.sleep(0.8)
    type_string(pswd); time.sleep(0.8)
    send_scancode(SCANCODE_ENTER)

    found, _, _ = wait_image_in_window("CharSelect.png", 300, delay_after_found=3)
    if not found: return False

    win_click(887, 250); time.sleep(1.0)
    win_click(945, 561); time.sleep(1.0)
    type_string(pin); time.sleep(0.8)
    send_scancode(SCANCODE_ENTER)

    found, _, _ = wait_image_in_window("smile_feature.png", 300, delay_after_found=5)
    if not found: return False

    # 取得當前登入成功的遊戲視窗 Handle (HWND)
    current_hwnd = win32gui.GetForegroundWindow()

    if move_window:
        try:
            active_win = gw.getActiveWindow()
            if active_win:
                active_win.moveTo(target_x, target_y)
                signals.log_signal.emit(f"視窗已移動至座標: ({target_x}, {target_y})")
                
                # 移動完成後，自動將視窗縮小到工作列（背景運行）
                time.sleep(3)
                win32gui.ShowWindow(current_hwnd, win32con.SW_MINIMIZE)
                signals.log_signal.emit(f"帳號 [{user}] 已自動縮小至工作列！")
        except Exception as e:
            signals.log_signal.emit(f"移動或縮小視窗失敗: {e}")
    else:
        signals.log_signal.emit("車頭模式：保持原位。")
        # 如果車頭不需要移動但也想縮小，可在此處自行決定是否縮小

    signals.log_signal.emit(f"帳號 [{user}] 登入成功！")
    return True

# ==========================================
# 4. 第三頁：車頭自動賣東西完整核心邏輯
# ==========================================
HUNTING_SETUP_REGION = (772, 542, 378, 292)
CHECK_SLOT_X = 1418
CHECK_SLOT_Y = 614
FRAME_COLOR = (167, 176, 183)

FIRST_SLOT_X = 1224
FIRST_SLOT_Y = 488
GRID_WIDTH = 36
GRID_HEIGHT = 36
COLS_COUNT = 6
STOP_SLOT = 30

SHOP_EMPTY_X = 1165
SHOP_EMPTY_Y = 691
CONFIRM_BTN_X = 953
CONFIRM_BTN_Y = 584
CLOSE_SHOP_X = 1185
CLOSE_SHOP_Y = 384

MAP_MERCHANT_REGION = (1142, 168, 323, 295)
MAP_MERCHANT_OFFSET = (15, 35)

GAME_MAIN_REGION = (448, 134, 1032, 804)
SCREEN_NPC_REGION = (840, 300, 363, 250)
SCREEN_NPC_OFFSET = (37, 11)
BUY_SELL_REGION =(971, 550, 86, 27)
BUY_SELL_OFFSET = (24, 6)
REVIVE_REGION = (790, 503, 341, 98)
REVIVE_OFFSET = (39, 7)
DISCONNECT_REGION = (602, 424, 660, 279)

PET_BUFF_REGION = (487, 275, 41, 38)
PET_TAB_REGION = (1045, 476, 205, 29)
PET_TAB_OFFSET = (6, 6)
PET_LIST_REGION = (1045, 509, 204, 98)
PET_SKILL_OFFSET = (59, 6)

def compare_rgb_color(c1, c2, max_diff=25):
    return all(abs(a - b) <= max_diff for a, b in zip(c1, c2))

def is_bag_full():
    try:
        current_color = pyautogui.pixel(CHECK_SLOT_X, CHECK_SLOT_Y)
        if compare_rgb_color(current_color, FRAME_COLOR, max_diff=25):
            return True
    except Exception:
        pass
    return False

def check_disconnect(bot_token, chat_id, char_name):
    global IS_NOTIFIED
    if IS_NOTIFIED:
        return True
    pos = find_pic_in_region_sell("disconnect.png", DISCONNECT_REGION, threshold=0.72)
    if pos:
        time.sleep(0.3)
        if find_pic_in_region_sell("disconnect.png", DISCONNECT_REGION, threshold=0.72):
            msg = f"{char_name}斷線" if char_name else "車頭角色斷線"
            signals.log_signal.emit(f"⚠️ 偵測到斷線！準備發送通知：{msg}")
            send_telegram_notification(bot_token, chat_id, msg)
            IS_NOTIFIED = True
            return True
    return False

def clear_mouse_cursor():
    click_smooth(962, 589, hold_time=0.04)
    time.sleep(0.15)
    click_smooth(962, 589, hold_time=0.04)
    time.sleep(0.15)

def is_dead():
    return find_pic_in_region_sell("revive.png", REVIVE_REGION, threshold=0.75) is not None

def check_and_buff_pet_skill():
    signals.log_signal.emit("[執行程序] 檢查並補發寵物技能...")
    if wait_pic_exist_custom("pet_buff.png", PET_BUFF_REGION, timeout_ms=1200, threshold=0.75):
        pass
    else:
        press_key_smooth('l')
        time.sleep(0.3)
        if wait_and_click_pic_offset("none.png", PET_TAB_REGION, PET_TAB_OFFSET, timeout_ms=2500, threshold=0.72):
            time.sleep(0.3)
        wait_and_click_pic_offset("loot_all.png", PET_LIST_REGION, PET_SKILL_OFFSET, timeout_ms=2500, threshold=0.72)
        time.sleep(0.3)
        press_key_smooth('l')
        time.sleep(0.3)

def teleport_and_setup_hunting(log_prefix="[執行程序]"):
    time.sleep(0.5)
    signals.log_signal.emit(f"{log_prefix} 按下前點卡...")
    press_key_smooth('s')
    
    signals.log_signal.emit(f"{log_prefix} 已按下前點卡，延遲 6 秒等待地圖傳送與載入...")
    for _ in range(60):
        if not auto_sell_running:
            return
        time.sleep(0.1)

    press_key_smooth('f5')
    signals.log_signal.emit(f"{log_prefix} 已按下 F5，進行 5 秒偵測開始圖片...")
    wait_and_click_pic_center("start_F5.png", region=HUNTING_SETUP_REGION, timeout_ms=5000, threshold=0.8)

    time.sleep(0.6)
    press_key_smooth('f5')
    time.sleep(0.6)

    if is_dead() or not auto_sell_running:
        return
    press_key_smooth('f6')
    signals.log_signal.emit(f"{log_prefix} 已按下 F6，進行 4 秒偵測開始圖片...")
    wait_and_click_pic_center("start_F6.png", region=HUNTING_SETUP_REGION, timeout_ms=4000, threshold=0.8)
    time.sleep(0.6)
    press_key_smooth('f6')
    time.sleep(0.6)

    if is_dead() or not auto_sell_running:
        return
    press_key_smooth('d')
    signals.log_signal.emit(f"{log_prefix} 開啟寵物 (D)...")
    time.sleep(0.6)
    click_smooth(982, 586)
    time.sleep(0.6)

    if is_dead() or not auto_sell_running:
        return
    check_and_buff_pet_skill()

    signals.log_signal.emit(f"{log_prefix} 直接按下 I 鍵開啟背包...")
    press_key_smooth('i')
    time.sleep(0.4)

def sell_inventory_items(bot_token, chat_id, char_name):
    signals.log_signal.emit("[執行程序] 開始依序出售背包物品...")
    for i in range(STOP_SLOT):
        if not auto_sell_running:
            return
        if i % 10 == 0:
            check_disconnect(bot_token, chat_id, char_name)
            if is_dead():
                clear_mouse_cursor()
                return
        row = i // COLS_COUNT
        col = i % COLS_COUNT
        target_x = FIRST_SLOT_X + (col * GRID_WIDTH)
        target_y = FIRST_SLOT_Y + (row * GRID_HEIGHT)
        click_smooth(target_x, target_y, hold_time=0.04)
        time.sleep(0.06)
        click_smooth(SHOP_EMPTY_X, SHOP_EMPTY_Y, hold_time=0.04)
        time.sleep(0.06)
        click_smooth(CONFIRM_BTN_X, CONFIRM_BTN_Y, hold_time=0.04)
        time.sleep(0.08)
    clear_mouse_cursor()
    signals.log_signal.emit("[執行程序] 背包物品販賣完畢！")

def return_town_and_sell(bot_token, chat_id, char_name):
    signals.log_signal.emit("[執行程序] 偵測背包已滿，準備回城賣東西...")
    clear_mouse_cursor()
    press_key_smooth('a')
    time.sleep(0.5)
    press_key_smooth('a')
    time.sleep(0.5)
    press_key_smooth('a')
    time.sleep(10.0)
    
    signals.log_signal.emit("[執行程序] 已按下起點卡，持續等待右上角本館圖片...")
    while not wait_pic_exist_custom("TownFeature.png", (1214, 133, 267, 68), timeout_ms=1000, threshold=0.9):
        if not auto_sell_running:
            return
        check_disconnect(bot_token, chat_id, char_name)
        time.sleep(0.5)
        if is_dead():
            return

    signals.log_signal.emit("[執行程序] 偵測到本館圖片，等待 3 秒完成畫面載入...")
    for _ in range(30):
        if not auto_sell_running or is_dead():
            return
        time.sleep(0.1)

    clear_mouse_cursor()

    signals.log_signal.emit("[執行程序] 地圖載入完成，開始尋找地圖商人...")
    if is_dead():
        return

    max_retries = 3
    teacher_found = False
    for attempt in range(1, max_retries + 1):
        if not auto_sell_running or is_dead():
            return
        signals.log_signal.emit(f"[執行程序] 尋找地圖商人中 (嘗試第 {attempt} 次)...")
        press_key_smooth('m')
        time.sleep(1.0)
        if wait_and_click_map_teacher("map_main_teacher.png", MAP_MERCHANT_REGION, MAP_MERCHANT_OFFSET, timeout_ms=4000, threshold=0.72):
            time.sleep(0.4)
            press_key_smooth('m')
            for _ in range(10):
                if not auto_sell_running or is_dead():
                    return
                check_disconnect(bot_token, chat_id, char_name)
                time.sleep(1.0)
            if find_pic_in_region_sell("kendo_teacher.png", SCREEN_NPC_REGION, threshold=0.72):
                teacher_found = True
                break
        else:
            press_key_smooth('m')
            time.sleep(1.0)

    if not teacher_found:
        signals.log_signal.emit("⚠️ 嘗試 3 次仍未偵測到地圖商人！執行防卡死備援：直接按下前點卡返回...")
        clear_mouse_cursor()
        teleport_and_setup_hunting(log_prefix="[備援程序]")
        return

    if is_dead():
        return

    signals.log_signal.emit("[執行程序] 點擊 NPC 準備進行交易...")
    if wait_and_click_pic_offset("kendo_teacher.png", SCREEN_NPC_REGION, SCREEN_NPC_OFFSET, timeout_ms=5000, threshold=0.72):
        time.sleep(1.0)
        if is_dead():
            return
        if wait_and_click_pic_offset("buy_sell.png", BUY_SELL_REGION, BUY_SELL_OFFSET, timeout_ms=4000, threshold=0.72):
            time.sleep(1.0)
            if is_dead():
                return
            sell_inventory_items(bot_token, chat_id, char_name)
            if is_dead():
                return

    clear_mouse_cursor()
    if is_dead():
        return

    signals.log_signal.emit("[執行程序] 販賣完成，準備返回練功點...")
    teleport_and_setup_hunting(log_prefix="[執行程序]")

def check_and_handle_death(bot_token, chat_id, char_name):
    if wait_and_click_pic_offset("revive.png", REVIVE_REGION, REVIVE_OFFSET, timeout_ms=500, threshold=0.75):
        signals.log_signal.emit("[執行程序] 偵測到角色死亡，正在點擊復活...")
        time.sleep(10.0)
        signals.log_signal.emit("[執行程序] 等待右上角本館圖片出現...")
        
        while not wait_pic_exist_custom("TownFeature.png", (1214, 133, 267, 68), timeout_ms=1000, threshold=0.9):
            if not auto_sell_running:
                return
            check_disconnect(bot_token, chat_id, char_name)
            time.sleep(0.5)

        signals.log_signal.emit("[執行程序] 偵測到本館圖片，等待 6 秒完成畫面與人物載入...")
        time.sleep(6.0)

        clear_mouse_cursor()
        teleport_and_setup_hunting(log_prefix="[執行程序]")

def auto_sell_main_loop(bot_token, chat_id, char_name):
    global auto_sell_running
    signals.log_signal.emit(f"【第三頁】車頭自動賣東西已啟動 (角色: {char_name})，開始巡邏監控...")
    while auto_sell_running:
        if check_disconnect(bot_token, chat_id, char_name):
            time.sleep(1.0)
            continue

        if is_dead():
            check_and_handle_death(bot_token, chat_id, char_name)
            continue

        if is_bag_full():
            return_town_and_sell(bot_token, chat_id, char_name)
            signals.log_signal.emit("【第三頁】車頭自動賣東西恢復巡邏中...")

        time.sleep(0.8)
    signals.log_signal.emit("【第三頁】車頭自動賣東西已停止。")

# ==========================================
# 5. 第四頁：三區自動復活與監控核心邏輯
# ==========================================
revive_mouse_lock = threading.Lock()
revive_action_labels = {}

def update_revive_bot_status(bot_id, text):
    signals.log_signal.emit(f"[三區復活 - 角色 {bot_id}] {text}")
    signals.revive_status_signal.emit(bot_id, f"角色 {bot_id}：{text}")

class ReviveBotThread(threading.Thread):
    def __init__(self, bot_id, bot_token, chat_id, base_x, base_y, char_name="", mode="same"):
        super().__init__()
        self.bot_id = bot_id
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.char_name = char_name
        self.mode = mode
        self.daemon = True
        self.is_notified = False

        self.base_x = base_x
        self.base_y = base_y

        self.DISCONNECT_REGION_REVIVE = (base_x + 602, base_y + 424, 660, 279)
        self.GAME_MAIN_REGION_REVIVE = (base_x + 448, base_y + 134, 1032, 804)
        self.REVIVE_REGION_REVIVE = (base_x + 373, base_y + 371, 296, 80)
        self.REVIVE_OFFSET_REVIVE = (39, 7)
        self.CARD_CD_REGION = (base_x + 1, base_y + 1, 638, 100)

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
        self.focus_this_window()
        pydirectinput.keyDown(key_name)
        time.sleep(hold_time)
        pydirectinput.keyUp(key_name)
        time.sleep(0.1)

    def find_pic_in_region_revive(self, pic_name, region, threshold=0.72, get_center=False):
        return find_pic_in_region_sell(pic_name, region, threshold, get_center)

    def wait_and_click_offset_revive(self, pic_name, region, offset, timeout_ms=3000, threshold=0.72):
        start_time = time.time()
        timeout_sec = timeout_ms / 1000.0
        while time.time() - start_time < timeout_sec:
            if not revive_running:
                return False
            pos = self.find_pic_in_region_revive(pic_name, region, threshold, get_center=False)
            if pos:
                self.click_smooth_revive(pos[0] + offset[0], pos[1] + offset[1])
                return True
            time.sleep(0.2)
        return False

    def wait_and_click_center_revive(self, pic_name, region=None, timeout_ms=3000, threshold=0.72):
        if region is None:
            region = self.GAME_MAIN_REGION_REVIVE
        start_time = time.time()
        timeout_sec = timeout_ms / 1000.0
        while time.time() - start_time < timeout_sec:
            if not revive_running:
                return False
            center_pos = self.find_pic_in_region_revive(pic_name, region, threshold, get_center=True)
            if center_pos:
                self.click_smooth_revive(center_pos[0], center_pos[1])
                return True
            time.sleep(0.2)
        return False

    def check_disconnect_revive(self):
        if self.is_notified:
            return True
        pos = self.find_pic_in_region_revive("disconnect.png", self.DISCONNECT_REGION_REVIVE, threshold=0.72)
        if pos:
            update_revive_bot_status(self.bot_id, "⚠ 偵測到斷線！")
            target_name = self.char_name if self.char_name else f"3區角色{self.bot_id}"
            msg = f"{target_name}斷線"
            send_telegram_notification(self.bot_token, self.chat_id, msg)
            self.is_notified = True
            return True
        return False

    def check_and_handle_death_revive(self):
        """核心監控邏輯：支援多開視窗同時獨立並行復活與重啟掛打流程（完全互不阻塞）"""
        
        if self.mode == "same":
            # ==========================================
            # 【同地圖復活流程（獨立並行版）】
            # ==========================================
            pos = self.find_pic_in_region_revive("revive.png", self.REVIVE_REGION_REVIVE, threshold=0.75)
            if pos:
                if not revive_running:
                    return

                revive_pos = pos

                # 1. 偵測到死亡後，原地獨立等待 7 秒（不影響其他角色執行緒）
                update_revive_bot_status(self.bot_id, "💀 偵測到死亡，獨立等待 7 秒...")
                start_wait = time.time()
                while time.time() - start_wait < 7.0:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    time.sleep(0.05)

                # 2. 7 秒已過，取得焦點並點擊復活按鈕
                update_revive_bot_status(self.bot_id, "7 秒已過，正式點擊復活...")
                click_x = revive_pos[0] + self.REVIVE_OFFSET_REVIVE[0]
                click_y = revive_pos[1] + self.REVIVE_OFFSET_REVIVE[1]
                
                # 瞬間搶佔滑鼠點擊該視窗的復活按鈕
                with revive_mouse_lock:
                    self.focus_this_window()
                    self.click_smooth_revive(click_x, click_y)

                update_revive_bot_status(self.bot_id, "已點擊復活，開始 14 秒獨立計時...")

                # 3. 進入 14 秒計時，在第 2 秒時點擊掛打「停止」按鈕
                start_14s = time.time()
                clicked_stop = False

                while time.time() - start_14s < 14.0:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    
                    elapsed = time.time() - start_14s
                    if elapsed >= 2.0 and not clicked_stop:
                        update_revive_bot_status(self.bot_id, "到達 2 秒，點擊掛打停止按鈕...")
                        target_btn_x = revive_pos[0] + 1
                        target_btn_y = revive_pos[1] + 226
                        
                        with revive_mouse_lock:
                            self.focus_this_window()
                            self.click_smooth_revive(target_btn_x, target_btn_y)
                        clicked_stop = True

                    time.sleep(0.05)

                # 4. 14 秒時間到，按下 's' 鍵（前點卡）傳送
                update_revive_bot_status(self.bot_id, "14 秒到，按下 s 前點卡潛回...")
                self.press_key_smooth_revive('s')

                # 5. 等待 2 秒讓畫面穩定
                start_2s = time.time()
                while time.time() - start_2s < 2.0:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    time.sleep(0.05)

                # 6. 點擊掛打「開始」按鈕恢復掛機
                update_revive_bot_status(self.bot_id, "點擊掛打開始按鈕...")
                target_btn_x = revive_pos[0] + 1 
                target_btn_y = revive_pos[1] + 226
                
                with revive_mouse_lock:
                    self.focus_this_window()
                    self.click_smooth_revive(target_btn_x, target_btn_y)

                update_revive_bot_status(self.bot_id, "同地圖復活流程完成！恢復即時監控。")

        else:
            # ==========================================
            # 【跨地圖復活流程（獨立並行版）】
            # ==========================================
            pos = self.find_pic_in_region_revive("revive.png", self.REVIVE_REGION_REVIVE, threshold=0.75)
            if pos:
                if not revive_running:
                    return

                revive_pos = pos
                update_revive_bot_status(self.bot_id, "💀 偵測死亡！點擊復活 (跨地圖)...")

                click_x = revive_pos[0] + self.REVIVE_OFFSET_REVIVE[0]
                click_y = revive_pos[1] + self.REVIVE_OFFSET_REVIVE[1]
                
                with revive_mouse_lock:
                    self.focus_this_window()
                    self.click_smooth_revive(click_x, click_y)

                update_revive_bot_status(self.bot_id, "跨地圖復活，獨立等待 20 秒...")
                start_20s = time.time()
                while time.time() - start_20s < 20.0:
                    if not revive_running or self.check_disconnect_revive():
                        return
                    time.sleep(0.05)

                self.press_key_smooth_revive('s')
                update_revive_bot_status(self.bot_id, "已按下 s 前點卡，獨立等待 6 秒...")

                start_6s = time.time()
                while time.time() - start_6s < 6.0:
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
                
                with revive_mouse_lock:
                    self.focus_this_window()
                    self.click_smooth_revive(target_btn_x, target_btn_y)

                time.sleep(0.5)
                self.press_key_smooth_revive('f6')
                time.sleep(1.0)

                update_revive_bot_status(self.bot_id, "跨地圖復活流程完成！恢復即時監控。")
    def run(self):
        update_revive_bot_status(self.bot_id, "執行緒啟動，開始巡邏監控...")
        while revive_running:
            self.check_disconnect_revive()
            self.check_and_handle_death_revive()
            time.sleep(0.8)
        update_revive_bot_status(self.bot_id, "執行緒已停止")

# ==========================================
# 6. PyQt5 主介面設計 (全分頁與控制控制台)
# ==========================================
class GameLauncher(QWidget):
    def __init__(self):
        super().__init__()
        self.correct_license = "8888"
        self.init_ui()
        self.load_config()

    def on_revive_mode_changed(self):
        global revive_mode
        if self.rb_same_map.isChecked():
            revive_mode = 'same'
            self.update_log("已切換模式：【同地圖復活 (監控CD)】，按 F7 啟動。")
        else:
            revive_mode = 'cross'
            self.update_log("已切換模式：【跨地圖復活 (延遲15s)】，按 F7 啟動。")

    def update_revive_label(self, bot_id, text):
        if bot_id in revive_action_labels:
            revive_action_labels[bot_id].setText(text)

    def init_ui(self):
        self.setWindowTitle("遊戲自動化總控台")
        self.resize(520, 480)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(4)
        self.btn_page1 = QPushButton("1.序號")
        self.btn_page2 = QPushButton("2.多開登入")
        self.btn_page3 = QPushButton("3.車頭賣物")
        self.btn_page4 = QPushButton("4.三區復活")

        for btn in [self.btn_page1, self.btn_page2, self.btn_page3, self.btn_page4]:
            btn.setFixedHeight(28)

        self.btn_page1.clicked.connect(lambda: self.switch_page(0))
        self.btn_page2.clicked.connect(lambda: self.switch_page(1))
        self.btn_page3.clicked.connect(lambda: self.switch_page(2))
        self.btn_page4.clicked.connect(lambda: self.switch_page(3))

        nav_layout.addWidget(self.btn_page1)
        nav_layout.addWidget(self.btn_page2)
        nav_layout.addWidget(self.btn_page3)
        nav_layout.addWidget(self.btn_page4)
        main_layout.addLayout(nav_layout)

        self.stacked_widget = QStackedWidget()

        # 第一頁：序號驗證
        self.page_auth = QWidget()
        auth_layout = QVBoxLayout()
        lbl_title = QLabel("<h4>請輸入認證序號</h4>")
        lbl_title.setAlignment(Qt.AlignCenter)
        self.txt_license = QLineEdit()
        self.txt_license.setPlaceholderText("")
        self.txt_license.setEchoMode(QLineEdit.Password)
        btn_verify = QPushButton("驗證並進入總控台")
        btn_verify.setFixedHeight(32)
        btn_verify.clicked.connect(self.verify_license)

        auth_layout.addStretch()
        auth_layout.addWidget(lbl_title)
        auth_layout.addWidget(self.txt_license)
        auth_layout.addWidget(btn_verify)
        auth_layout.addStretch()
        self.page_auth.setLayout(auth_layout)

        # 第二頁：多開登入主控面板
        self.page_main = QWidget()
        main_page_layout = QVBoxLayout()
        main_page_layout.setContentsMargins(4, 4, 4, 4)

        path_group = QGroupBox("遊戲路徑設定")
        path_layout = QFormLayout()
        path_layout.setContentsMargins(6, 6, 6, 6)
        self.txt_game_path = QLineEdit(r"d:\Desktop\Rosso RAN Online [ Full Client ]\Rosso-RAN.EXE")
        path_layout.addRow("Exe:", self.txt_game_path)
        path_group.setLayout(path_layout)
        main_page_layout.addWidget(path_group)

        acc_group = QGroupBox("帳號密碼與隊伍設定")
        acc_layout = QFormLayout()
        acc_layout.setContentsMargins(6, 6, 6, 6)

        self.chk_leader = QCheckBox()
        self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_sub1 = QCheckBox()
        self.acc1_user, self.acc1_pass, self.acc1_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_sub2 = QCheckBox()
        self.acc2_user, self.acc2_pass, self.acc2_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_sub3 = QCheckBox()
        self.acc3_user, self.acc3_pass, self.acc3_pin = QLineEdit(), QLineEdit(), QLineEdit()

        self.chk_leader.toggled.connect(self.on_leader_toggled)

        acc_layout.addRow("F1車頭:", self.create_h_layout(self.chk_leader, self.acc_leader_user, self.acc_leader_pass, self.acc_leader_pin))
        acc_layout.addRow("F1打手:", self.create_h_layout(self.chk_sub1, self.acc1_user, self.acc1_pass, self.acc1_pin))
        acc_layout.addRow("F2打手:", self.create_h_layout(self.chk_sub2, self.acc2_user, self.acc2_pass, self.acc2_pin))
        acc_layout.addRow("F3打手:", self.create_h_layout(self.chk_sub3, self.acc3_user, self.acc3_pass, self.acc3_pin))
        acc_group.setLayout(acc_layout)
        main_page_layout.addWidget(acc_group)

        lbl_info = QLabel("<small>• F1-F3: 單開 | F4: 一條龍登入 | F12: 全部中止</small>")
        main_page_layout.addWidget(lbl_info)
        self.page_main.setLayout(main_page_layout)

        # 第三頁：車頭自動賣東西介面
        self.page_sell = QWidget()
        sell_layout = QVBoxLayout()
        sell_layout.setContentsMargins(4, 4, 4, 4)

        tg_group = QGroupBox("Telegram 斷線通知與車頭角色設定")
        tg_form = QFormLayout()
        tg_form.setContentsMargins(6, 6, 6, 6)

        self.txt_tg_token = QLineEdit()
        self.txt_tg_chatid = QLineEdit()
        self.txt_char_name = QLineEdit()

        btn_test_tg = QPushButton("發送測試 Telegram 訊息")
        btn_test_tg.clicked.connect(self.test_telegram_msg)

        tg_form.addRow("Bot Token:", self.txt_tg_token)
        tg_form.addRow("Chat ID:", self.txt_tg_chatid)
        tg_form.addRow("角色名稱:", self.txt_char_name)
        tg_form.addRow("", btn_test_tg)

        tg_group.setLayout(tg_form)
        sell_layout.addWidget(tg_group)

        sell_status_group = QGroupBox("車頭賣東西運行狀態")
        sell_status_layout = QVBoxLayout()
        sell_status_layout.setContentsMargins(6, 6, 6, 6)

        self.lbl_sell_status = QLabel("狀態：已停止 (F12)")
        self.lbl_sell_status.setStyleSheet("font-weight: bold; color: red; font-size: 11pt;")
        sell_status_layout.addWidget(self.lbl_sell_status)

        lbl_sell_help = QLabel("<small>• 按鍵 F7：啟動車頭賣東西 | F12：中止所有動作</small>")
        sell_status_layout.addWidget(lbl_sell_help)
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

        mode_group = QGroupBox("復活模式選擇 (請勾選後按 F7 啟動)")
        mode_box = QHBoxLayout()

        self.rb_same_map = QRadioButton("同地圖復活 (監控CD)")
        self.rb_cross_map = QRadioButton("跨地圖復活 (延遲20s)")
        self.rb_same_map.setChecked(True)

        self.btn_group_mode = QButtonGroup(self)
        self.btn_group_mode.addButton(self.rb_same_map)
        self.btn_group_mode.addButton(self.rb_cross_map)

        self.rb_same_map.toggled.connect(self.on_revive_mode_changed)

        mode_box.addWidget(self.rb_same_map)
        mode_box.addWidget(self.rb_cross_map)
        mode_group.setLayout(mode_box)
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

        lbl_revive_help = QLabel("<small>• 按下 F7：啟動當前勾選之模式監控 | F12：中止所有動作</small>")
        revive_status_layout.addWidget(lbl_revive_help)
        revive_status_group.setLayout(revive_status_layout)
        revive_layout.addWidget(revive_status_group)

        self.page_revive.setLayout(revive_layout)

        self.stacked_widget.addWidget(self.page_auth)
        self.stacked_widget.addWidget(self.page_main)
        self.stacked_widget.addWidget(self.page_sell)
        self.stacked_widget.addWidget(self.page_revive)

        main_layout.addWidget(self.stacked_widget)

        self.txt_log = QLineEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setPlaceholderText("系統訊息提示...")
        main_layout.addWidget(self.txt_log)

        self.setLayout(main_layout)
        signals.log_signal.connect(self.update_log)
        signals.revive_status_signal.connect(self.update_revive_label)

    def switch_page(self, index):
        if index >= 1:
            if self.stacked_widget.currentIndex() == 0:
                QMessageBox.warning(self, "提示", "請先通過第一頁序號驗證！")
                return
        self.stacked_widget.setCurrentIndex(index)

    def create_h_layout(self, chk, w1, w2, w3):
        box = QWidget()
        lay = QHBoxLayout()
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(chk)
        lay.addWidget(w1); lay.addWidget(w2); lay.addWidget(w3)
        box.setLayout(lay)
        return box

    def on_leader_toggled(self, is_checked):
        if is_checked:
            self.chk_sub1.setChecked(False)
            self.chk_sub2.setChecked(False)
            self.chk_sub3.setChecked(False)
            self.chk_sub1.setEnabled(False)
            self.chk_sub2.setEnabled(False)
            self.chk_sub3.setEnabled(False)
            self.update_log("切換為【車頭模式】：已鎖定打手項目。")
        else:
            self.chk_sub1.setEnabled(True)
            self.chk_sub2.setEnabled(True)
            self.chk_sub3.setEnabled(True)
            self.update_log("切換為【打手模式】：打手欄位已解鎖。")

    def verify_license(self):
        entered = self.txt_license.text().strip()
        if entered == self.correct_license:
            self.stacked_widget.setCurrentIndex(1)
            self.update_log("驗證成功！已解鎖各功能分頁。")
        else:
            QMessageBox.warning(self, "錯誤", "序號錯誤！請重新輸入。")

    def update_log(self, text):
        self.txt_log.setText(text)
        print(text)

    def test_telegram_msg(self):
        token = self.txt_tg_token.text().strip()
        chatid = self.txt_tg_chatid.text().strip()
        cname = self.txt_char_name.text().strip()
        if not token or not chatid:
            QMessageBox.warning(self, "提示", "請輸入完整的 Bot Token 與 Chat ID！")
            return
        msg = f"{cname}斷線" if cname else "測試角色斷線"
        send_telegram_notification(token, chatid, msg)
        QMessageBox.information(self, "成功", f"已發送測試 Telegram 訊息：{msg}")

    def get_acc_data(self, key_type):
        if key_type == 'leader':
            acc = [self.acc_leader_user.text(), self.acc_leader_pass.text(), self.acc_leader_pin.text()]
            return acc, 0, 0, self.chk_leader.isChecked(), False
        elif key_type == 'sub1':
            acc = [self.acc1_user.text(), self.acc1_pass.text(), self.acc1_pin.text()]
            return acc, -321, 0, self.chk_sub1.isChecked(), True
        elif key_type == 'sub2':
            acc = [self.acc2_user.text(), self.acc2_pass.text(), self.acc2_pin.text()]
            return acc, 297, 0, self.chk_sub2.isChecked(), True
        elif key_type == 'sub3':
            acc = [self.acc3_user.text(), self.acc3_pass.text(), self.acc3_pin.text()]
            return acc, 896, 0, self.chk_sub3.isChecked(), True

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            self.chk_leader.setChecked(True)
            return
        config = configparser.ConfigParser()
        try:
            config.read(CONFIG_FILE, encoding='utf-8')
            if 'SETTINGS' in config:
                sec = config['SETTINGS']
                self.txt_game_path.setText(sec.get('game_path', self.txt_game_path.text()))
                
                self.chk_leader.setChecked(sec.getboolean('chk_leader', True))
                self.acc_leader_user.setText(sec.get('leader_user', ''))
                self.acc_leader_pass.setText(sec.get('leader_pass', ''))
                self.acc_leader_pin.setText(sec.get('leader_pin', ''))

                self.chk_sub1.setChecked(sec.getboolean('chk_sub1', False))
                self.acc1_user.setText(sec.get('sub1_user', ''))
                self.acc1_pass.setText(sec.get('sub1_pass', ''))
                self.acc1_pin.setText(sec.get('sub1_pin', ''))

                self.chk_sub2.setChecked(sec.getboolean('chk_sub2', False))
                self.acc2_user.setText(sec.get('sub2_user', ''))
                self.acc2_pass.setText(sec.get('sub2_pass', ''))
                self.acc2_pin.setText(sec.get('sub2_pin', ''))

                self.chk_sub3.setChecked(sec.getboolean('chk_sub3', False))
                self.acc3_user.setText(sec.get('sub3_user', ''))
                self.acc3_pass.setText(sec.get('sub3_pass', ''))
                self.acc3_pin.setText(sec.get('sub3_pin', ''))

                self.txt_tg_token.setText(sec.get('tg_token', ''))
                self.txt_tg_chatid.setText(sec.get('tg_chatid', ''))
                self.txt_char_name.setText(sec.get('char_name', ''))
                self.txt_revive_tg_token.setText(sec.get('revive_tg_token', ''))
                self.txt_revive_tg_chatid.setText(sec.get('revive_tg_chatid', ''))
                self.txt_revive_char1.setText(sec.get('revive_char1', ''))
                self.txt_revive_char2.setText(sec.get('revive_char2', ''))
                self.txt_revive_char3.setText(sec.get('revive_char3', ''))
        except Exception:
            pass

    def save_config(self):
        config = configparser.ConfigParser()
        config['SETTINGS'] = {
            'game_path': self.txt_game_path.text(),
            'chk_leader': str(self.chk_leader.isChecked()),
            'leader_user': self.acc_leader_user.text(),
            'leader_pass': self.acc_leader_pass.text(),
            'leader_pin': self.acc_leader_pin.text(),

            'chk_sub1': str(self.chk_sub1.isChecked()),
            'sub1_user': self.acc1_user.text(),
            'sub1_pass': self.acc1_pass.text(),
            'sub1_pin': self.acc1_pin.text(),

            'chk_sub2': str(self.chk_sub2.isChecked()),
            'sub2_user': self.acc2_user.text(),
            'sub2_pass': self.acc2_pass.text(),
            'sub2_pin': self.acc2_pin.text(),

            'chk_sub3': str(self.chk_sub3.isChecked()),
            'sub3_user': self.acc3_user.text(),
            'sub3_pass': self.acc3_pass.text(),
            'sub3_pin': self.acc3_pin.text(),

            'tg_token': self.txt_tg_token.text(),
            'tg_chatid': self.txt_tg_chatid.text(),
            'char_name': self.txt_char_name.text(),
            'revive_tg_token': self.txt_revive_tg_token.text(),
            'revive_tg_chatid': self.txt_revive_tg_chatid.text(),
            'revive_char1': self.txt_revive_char1.text(),
            'revive_char2': self.txt_revive_char2.text(),
            'revive_char3': self.txt_revive_char3.text(),
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
    if not window: return
    stop_flag = False
    window.save_config()
    is_leader = window.chk_leader.isChecked()
    acc, x, y, _, move_win = window.get_acc_data('leader' if is_leader else 'sub1')
    game_path = window.txt_game_path.text()
    if not acc[0]: return
    login_flow(acc, x, y, game_path, move_window=move_win)

def run_single(key_type):
    global stop_flag, window
    if not window: return
    stop_flag = False
    window.save_config()
    if window.chk_leader.isChecked(): return
    acc, x, y, _, move_win = window.get_acc_data(key_type)
    game_path = window.txt_game_path.text()
    if not acc[0]: return
    login_flow(acc, x, y, game_path, move_window=move_win)

def run_all():
    global stop_flag, window
    if not window: return
    stop_flag = False
    window.save_config()
    game_path = window.txt_game_path.text()
    if window.chk_leader.isChecked():
        acc, x, y, _, move_win = window.get_acc_data('leader')
        if acc[0]:
            login_flow(acc, x, y, game_path, move_window=move_win)
        return

    roles = ['sub1', 'sub2', 'sub3']
    for role in roles:
        if stop_flag: break
        acc, x, y, checked, move_win = window.get_acc_data(role)
        if checked and acc[0]:
            success = login_flow(acc, x, y, game_path, move_window=move_win)
            if not success: break

def trigger_start_f7():
    global auto_sell_running, revive_running, IS_NOTIFIED, stop_flag, revive_mode, window
    if not window: return
    stop_flag = False
    current_tab = window.stacked_widget.currentIndex()

    if current_tab == 2:
        if revive_running:
            signals.log_signal.emit("⚠️ 【互斥警告】三區自動復活正在運行中，無法同時啟動車頭賣東西！")
            return
        if not auto_sell_running:
            token = window.txt_tg_token.text().strip()
            chatid = window.txt_tg_chatid.text().strip()
            cname = window.txt_char_name.text().strip()
            if not token or not chatid:
                signals.log_signal.emit("⚠️ 錯誤：請先於第三頁設定 Telegram Token 與 Chat ID！")
                return
            auto_sell_running = True
            IS_NOTIFIED = False
            window.lbl_sell_status.setText("狀態：車頭自動賣東西監控中 (F7)")
            window.lbl_sell_status.setStyleSheet("font-weight: bold; color: green; font-size: 11pt;")
            threading.Thread(target=auto_sell_main_loop, args=(token, chatid, cname), daemon=True).start()
            signals.log_signal.emit("【F7】已啟動第三頁車頭自動賣東西！")

    elif current_tab == 3:
        if auto_sell_running:
            signals.log_signal.emit("⚠️ 【互斥警告】車頭自動賣東西正在運行中，無法同時啟動三區復活！")
            return
        if not revive_running:
            token = window.txt_revive_tg_token.text().strip()
            chatid = window.txt_revive_tg_chatid.text().strip()
            if not token or not chatid:
                signals.log_signal.emit("⚠️ 錯誤：請先於第四頁設定 Telegram Token 與 Chat ID！")
                return

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
                    mode=revive_mode
                )
                revive_thread.start()
            signals.log_signal.emit(f"【F7】已啟動三區【{mode_desc}】自動復活與斷線監控！")

def trigger_stop_all():
    global stop_flag, auto_sell_running, revive_running, window
    stop_flag = True
    auto_sell_running = False
    revive_running = False

    if window:
        window.lbl_sell_status.setText("狀態：已停止 (F12)")
        window.lbl_sell_status.setStyleSheet("font-weight: bold; color: red; font-size: 11pt;")
        window.lbl_revive_status.setText("狀態：已停止 (F12)")
        window.lbl_revive_status.setStyleSheet("font-weight: bold; color: red; font-size: 11pt;")
        for bot_id in [1, 2, 3]:
            signals.revive_status_signal.emit(bot_id, f"角色 {bot_id}：已停止")

    signals.log_signal.emit("【F12】已緊急中止所有登入、車頭賣裝與三區復活動作！")

def pynput_on_press(key):
    global window
    if not window or window.stacked_widget.currentIndex() != 1:
        return
    try:
        if key == pynput_keyboard.Key.f12:
            trigger_stop_all()
        elif key == pynput_keyboard.Key.f1:
            threading.Thread(target=run_f1, daemon=True).start()
        elif key == pynput_keyboard.Key.f2:
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