# OWNER: P1
"""bootstrap.py: Imports every client module so their registrations run.

Contract section G. Plain static imports (PyInstaller cannot see dynamic
imports), in the contract order: infrastructure first, then renderers,
canvas and tools, then panels, the look (icons, theme: painted icons and
the style sheet; theme registers View > Toggle dark theme), then
main_window, which sees every registration, and shell (it arranges the
window main_window builds and registers View > Reset layout).
"""

from client import registry  # noqa: F401
from client import app_state  # noqa: F401
from client import net_client  # noqa: F401
from client import board_model  # noqa: F401
from client import render  # noqa: F401
from client import render_draw  # noqa: F401
from client import render_text  # noqa: F401
from client import render_image  # noqa: F401
from client import canvas  # noqa: F401
from client import toolbar  # noqa: F401
from client.tools import pen_tool  # noqa: F401
from client.tools import shape_tools  # noqa: F401
from client.tools import select_tool  # noqa: F401
from client.tools import text_tool  # noqa: F401
from client.tools import image_tool  # noqa: F401
from client import shortcuts  # noqa: F401
from client import draw_sync  # noqa: F401
from client import chat_panel  # noqa: F401
from client import users_panel  # noqa: F401
from client import stats_panel  # noqa: F401
from client import history_panel  # noqa: F401
from client import cursor_overlay  # noqa: F401
from client import toasts  # noqa: F401
from client import export  # noqa: F401
from client import icons  # noqa: F401
from client import theme  # noqa: F401
from client import connect_dialog  # noqa: F401
from client import main_window  # noqa: F401
from client import shell  # noqa: F401
