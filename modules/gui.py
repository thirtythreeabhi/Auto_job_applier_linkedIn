"""Small, optional GUI helpers for environments without X11 authorization.

PyAutoGUI imports MouseInfo on Linux, which opens the X display at import time.
The control panel can be running when that display authorization is unavailable.
The bot's web automation still works without desktop dialogs, so keep startup
available and make GUI-only actions harmless in that case.
"""

try:
    import pyautogui as pyautogui
except Exception as import_error:
    class _UnavailablePyAutoGUI:
        FAILSAFE = False

        @staticmethod
        def alert(text="", title="", *args, **kwargs):
            print(f"[GUI alert unavailable] {title}: {text}")
            return None

        @staticmethod
        def confirm(text="", title="", *args, **kwargs):
            print(f"[GUI confirmation unavailable] {title}: {text}")
            return None

        @staticmethod
        def press(*args, **kwargs):
            return None

    pyautogui = _UnavailablePyAutoGUI()
    print(f"[GUI automation unavailable; continuing without desktop dialogs] {import_error}")


alert = pyautogui.alert
confirm = pyautogui.confirm
