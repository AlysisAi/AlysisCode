"""Read only the exact synthetic HWND supplied by the qualification owner."""

import ctypes
import json
import sys
from ctypes import wintypes

user = ctypes.WinDLL("user32", use_last_error=True)
user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user.GetWindowTextW.restype = ctypes.c_int
text = ctypes.create_unicode_buffer(1024)
ctypes.set_last_error(0)
length = user.GetWindowTextW(int(sys.argv[1]), text, len(text))
print(
    json.dumps(
        {"length": length, "exact": text.value == sys.argv[2], "error": ctypes.get_last_error()}
    )
)
