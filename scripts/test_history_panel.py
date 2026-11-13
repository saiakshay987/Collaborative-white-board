"""test_history_panel.py: Tests for client/history_panel.py (version list, buttons, footer, Ctrl+S).

Run from the project root:  python scripts/test_history_panel.py

Needs PyQt5. Runs with Qt's offscreen platform (no window appears) and a fake
NetClient that only records what would have been sent.
"""
import os
import sys
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from common import protocol  # noqa: E402
from client import history_panel as hp, registry  # noqa: E402
from client.app_state import AppState  # noqa: E402
from client.board_model import BoardModel, board_checksum  # noqa: E402

QAPP = QApplication.instance() or QApplication([])


class FakeNet:
    """Stands in for NetClient: records send() calls."""

    def __init__(self):
        """Connected, nothing sent."""
        self.is_connected = True
        self.sent = []

    def send(self, type, payload=None, op_id=None):
        """Record the message; returns False when 'not connected' like the real one."""
        if not self.is_connected:
            return False
        self.sent.append((type, payload, op_id))
        return True


def rect(oid):
    """A rect object (what the server would have stored)."""
    return {"id": oid, "kind": "rect", "x": 0, "y": 0, "w": 40, "h": 40, "color": "#ff0000",
            "width": 2, "fill": None, "owner": "x", "z": 1}


def version(vid, name="v", by="alice", ts=None, count=3, seq=5):
    """A version metadata dict as the server sends it."""
    return {"id": vid, "name": name, "by": by, "ts": time.time() if ts is None else ts, "seq": seq, "count": count}


def make_app(role="host"):
    """A real AppState with a real BoardModel and a fake net, joined with `role`."""
    app = AppState()
    app.net = FakeNet()
    app.me.username, app.me.role, app.me.room = "hana", role, "ABCDEF"
    BoardModel().attach(app)
    return app


class HelperTests(unittest.TestCase):
    """The pure helper functions."""

    def test_clean_versions_keeps_only_usable_entries(self):
        good = version(1, "ok")
        payload = {"versions": [good, {"id": "2", "name": "bad id"}, {"id": True, "name": "bool"},
                                {"id": 3}, "junk", {"id": 4, "name": "sparse"}]}
        out = hp.clean_versions(payload)
        self.assertEqual([v["id"] for v in out], [1, 4])
        self.assertEqual((out[1]["by"], out[1]["ts"], out[1]["count"]), ("?", None, None))
        for bad in (None, {}, {"versions": "x"}, []):
            self.assertEqual(hp.clean_versions(bad), [])

    def test_format_time(self):
        now = time.mktime((2026, 11, 13, 15, 30, 0, 0, 0, -1))
        self.assertEqual(hp.format_time(now - 60, now), "15:29:00")
        self.assertEqual(hp.format_time(now - 86400 * 3, now), time.strftime("%d %b %H:%M", time.localtime(now - 86400 * 3)))
        for bad in (None, "x", True):
            self.assertEqual(hp.format_time(bad), "-")

    def test_default_name(self):
        t = time.mktime((2026, 11, 13, 9, 5, 7, 0, 0, -1))
        self.assertEqual(hp.default_name(t), "Version 09:05:07")

    def test_footer_text(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(hp.footer_text(7, 42, sha, 0), "Objects: 7   |   seq 42   |   checksum: 0123456789ab")
        self.assertIn("waiting for 2 pending", hp.footer_text(7, 42, sha, 2))
        self.assertNotIn("0123456789ab", hp.footer_text(7, 42, sha, 2))


class ModelTests(unittest.TestCase):
    """VersionsModel and the toasts."""

    def test_first_list_is_not_announced_later_ones_are(self):
        m = hp.VersionsModel()
        self.assertEqual(m.apply({"versions": [version(1), version(2)]}), [])        # what was already there
        new = m.apply({"versions": [version(1), version(2), version(3, "fresh")]})
        self.assertEqual([v["id"] for v in new], [3])
        m.reset()
        self.assertEqual((m.versions, m.known), ([], False))

    def test_updated_signal(self):
        m, hits = hp.VersionsModel(), []
        m.updated.connect(lambda: hits.append(1))
        m.apply({"versions": []})
        m.reset()
        self.assertEqual(len(hits), 2)

    def test_announce(self):
        app = make_app()
        toasts = []
        app.toast = toasts.append
        hp.announce(app, [version(1, "mine", by="hana"), version(2, "theirs", by="bob"),
                          version(3, "auto", by="server"), version(4, "before restore old", by="bob"),
                          version(5, "before clear", by="bob")])
        self.assertEqual(len(toasts), 3)
        self.assertEqual(toasts[0], "Version saved: mine")
        self.assertIn("bob restored a version", toasts[1])
        self.assertIn("Board cleared by bob", toasts[2])


class RegistrationTests(unittest.TestCase):
    """What the module registered."""

    def test_panel_menu_and_handlers(self):
        panel = registry.PANELS["history"]
        self.assertEqual((panel["area"], panel["factory"]), ("bottom", hp.HistoryPanel))
        entry = [e for e in registry.MENU_ACTIONS if e["label"] == "Save version"][0]
        self.assertEqual((entry["menu"], entry["shortcut"], entry["fn"]), ("File", "Ctrl+S", hp.save_version_quick))
        self.assertIn(hp._on_versions, registry.MESSAGE_HANDLERS[protocol.VERSIONS])
        self.assertIn(hp._on_join_ok, registry.MESSAGE_HANDLERS[protocol.JOIN_OK])


class ActionTests(unittest.TestCase):
    """save_version, restore_version, history_step, JOIN_OK."""

    def test_save_sends_version_save(self):
        for role in ("host", "editor"):
            app = make_app(role)
            self.assertTrue(hp.save_version(app, "draft"))
            self.assertEqual(app.net.sent, [(protocol.VERSION_SAVE, {"name": "draft"}, None)])

    def test_ctrl_s_uses_the_default_name(self):
        app = make_app("editor")
        hp.save_version_quick(app)
        (type_, payload, _), = app.net.sent
        self.assertEqual(type_, protocol.VERSION_SAVE)
        self.assertTrue(payload["name"].startswith("Version "))

    def test_viewer_and_unjoined_cannot_save(self):
        for role in ("viewer", None):
            app, toasts = make_app(role), []
            app.toast = toasts.append
            self.assertFalse(hp.save_version(app, "x"))
            self.assertEqual((app.net.sent, toasts), ([], ["Viewers cannot save versions"]))

    def test_save_when_disconnected(self):
        app, toasts = make_app("host"), []
        app.toast = toasts.append
        app.net.is_connected = False
        self.assertFalse(hp.save_version(app, "x"))
        self.assertEqual(toasts, ["Not connected"])

    def test_only_host_restores(self):
        for role in ("editor", "viewer"):
            app, toasts = make_app(role), []
            app.toast = toasts.append
            self.assertFalse(hp.restore_version(app, version(4)))
            self.assertEqual((app.net.sent, toasts), ([], ["Only the host can restore a version"]))
        app = make_app("host")
        self.assertTrue(hp.restore_version(app, version(4)))
        self.assertEqual(app.net.sent, [(protocol.VERSION_RESTORE, {"id": 4}, None)])

    def test_undo_and_redo_go_through_send_op(self):
        app = make_app("editor")
        hp.history_step(app, protocol.UNDO)
        hp.history_step(app, protocol.REDO)
        self.assertEqual([t for t, _, _ in app.net.sent], [protocol.UNDO, protocol.REDO])
        self.assertTrue(all(op_id and op_id.startswith("hana-") for _, _, op_id in app.net.sent))   # op_id set by send_op

    def test_viewer_cannot_undo(self):
        app, toasts = make_app("viewer"), []
        app.toast = toasts.append
        hp.history_step(app, protocol.UNDO)
        self.assertEqual((app.net.sent, toasts), ([], ["Viewers cannot undo or redo"]))

    def test_join_ok_asks_for_the_list_and_forgets_the_old_one(self):
        app = make_app("host")
        hp.model.apply({"versions": [version(1)]})
        hp._on_join_ok(app, {"type": protocol.JOIN_OK, "payload": {}})
        self.assertEqual(app.net.sent, [(protocol.VERSION_LIST, {}, None)])
        self.assertEqual((hp.model.versions, hp.model.known), ([], False))


class PanelTests(unittest.TestCase):
    """The widget itself."""

    def setUp(self):
        """A host app with a panel and an empty model."""
        hp.model.reset()
        self.app = make_app("host")
        self.panel = hp.HistoryPanel(self.app)

    def tearDown(self):
        """Stop the timer and forget the model."""
        self.panel._timer.stop()
        hp.model.reset()

    def cells(self):
        """The table as a list of rows of strings."""
        t = self.panel.table
        return [[t.item(r, c).text() for c in range(t.columnCount())] for r in range(t.rowCount())]

    def test_list_shows_name_author_time_and_count_newest_first(self):
        hp.model.apply({"versions": [version(1, "old", "alice", count=2), version(2, "new", "bob", count=9)]})
        rows = self.cells()
        self.assertEqual([r[0] for r in rows], ["new", "old"])
        self.assertEqual((rows[0][1], rows[0][3], rows[1][1], rows[1][3]), ("bob", "9", "alice", "2"))
        self.assertRegex(rows[0][2], r"^\d\d:\d\d:\d\d$")
        self.assertEqual([self.panel.table.horizontalHeaderItem(i).text() for i in range(4)],
                         ["Name", "By", "Time", "Objects"])

    def test_selection_survives_a_new_list(self):
        hp.model.apply({"versions": [version(1, "a"), version(2, "b")]})
        self.panel.table.selectRow(1)                            # version 1 (the older one, second row)
        self.assertEqual(self.panel.selected_version()["id"], 1)
        hp.model.apply({"versions": [version(1, "a"), version(2, "b"), version(3, "c")]})
        self.assertEqual(self.panel.selected_version()["id"], 1)

    def test_buttons_follow_the_role(self):
        hp.model.apply({"versions": [version(1)]})
        self.panel.table.selectRow(0)
        expected = {"host": (True, True, True, True, True, True), "editor": (True, False, False, False, True, True),
                    "viewer": (False, False, False, False, False, False),
                    None: (False, False, False, False, False, False)}
        for role, want in expected.items():
            self.app.set_role(role) if role else setattr(self.app.me, "role", None)
            self.panel.refresh_buttons()
            p = self.panel
            self.assertEqual((p.save_btn.isEnabled(), p.restore_btn.isEnabled(), p.rename_btn.isEnabled(),
                              p.delete_btn.isEnabled(), p.undo_btn.isEnabled(), p.redo_btn.isEnabled()), want, role)

    def test_disabled_version_buttons_say_why(self):
        hp.model.apply({"versions": [version(1)]})
        self.panel.table.selectRow(0)
        self.app.set_role("editor")
        for button, verb in ((self.panel.restore_btn, "restore"), (self.panel.rename_btn, "rename"),
                             (self.panel.delete_btn, "delete")):
            self.assertEqual(button.toolTip(), "Only the host can %s a version" % verb)
        self.app.set_role("host")
        self.assertNotIn("Only the host", self.panel.delete_btn.toolTip())

    def test_rename_button_prefills_and_sends_the_trimmed_name(self):
        hp.model.apply({"versions": [version(5, "old name")]})
        self.panel.table.selectRow(0)
        with mock.patch.object(QInputDialog, "getText", return_value=("  New name ", True)) as ask:
            self.panel.rename_btn.click()
        self.assertEqual(ask.call_args[1]["text"], "old name")
        self.assertEqual(self.app.net.sent, [(protocol.VERSION_RENAME, {"id": 5, "name": "New name"}, None)])

    def test_rename_sends_nothing_for_cancel_blank_same_or_too_long(self):
        hp.model.apply({"versions": [version(5, "old name")]})
        self.panel.table.selectRow(0)
        toasts = []
        self.app.toast = toasts.append
        for answer in (("x", False), ("   ", True), ("old name", True), ("x" * 61, True)):
            with mock.patch.object(QInputDialog, "getText", return_value=answer):
                self.panel.rename_btn.click()
        self.assertEqual(self.app.net.sent, [])
        self.assertEqual(len(toasts), 1)                         # only the too-long name explains itself

    def test_delete_button_confirms_first(self):
        hp.model.apply({"versions": [version(7, "good one")]})
        self.panel.table.selectRow(0)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.panel.delete_btn.click()
        self.assertEqual(self.app.net.sent, [])
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as ask:
            self.panel.delete_btn.click()
        self.assertEqual(self.app.net.sent, [(protocol.VERSION_DELETE, {"id": 7}, None)])
        self.assertIn("good one", ask.call_args[0][2])

    def test_non_host_helpers_send_nothing(self):
        for role in ("editor", "viewer"):
            app, toasts = make_app(role), []
            app.toast = toasts.append
            self.assertFalse(hp.rename_version(app, version(4), "new"))
            self.assertFalse(hp.delete_version(app, version(4)))
            self.assertEqual((app.net.sent, toasts), ([], ["Only the host can rename a version",
                                                           "Only the host can delete a version"]))

    def test_right_click_menu_selects_the_row_and_is_disabled_for_non_hosts(self):
        hp.model.apply({"versions": [version(1, "a"), version(2, "b")]})
        row_pos = self.panel.table.visualItemRect(self.panel.table.item(1, 0)).center()    # row 1 is version 1
        for role, enabled in (("host", True), ("editor", False)):
            self.app.set_role(role)
            with mock.patch.object(hp.QMenu, "exec_"):
                self.panel.on_context_menu(row_pos)
            self.assertEqual(self.panel.selected_version()["id"], 1)
            actions = self.panel.context_menu.actions()
            self.assertEqual([a.text() for a in actions], ["Restore", "Rename", "Delete"])
            self.assertEqual([a.isEnabled() for a in actions], [enabled] * 3)
            if not enabled:
                self.assertEqual(actions[2].toolTip(), "Only the host can delete a version")

    def test_right_click_menu_actions_send_the_messages(self):
        hp.model.apply({"versions": [version(3, "c")]})
        row_pos = self.panel.table.visualItemRect(self.panel.table.item(0, 0)).center()
        with mock.patch.object(hp.QMenu, "exec_"):
            self.panel.on_context_menu(row_pos)
        rename, delete = self.panel.context_menu.actions()[1:]
        with mock.patch.object(QInputDialog, "getText", return_value=("d", True)):
            rename.trigger()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            delete.trigger()
        self.assertEqual(self.app.net.sent, [(protocol.VERSION_RENAME, {"id": 3, "name": "d"}, None),
                                             (protocol.VERSION_DELETE, {"id": 3}, None)])

    def test_selection_survives_a_rename_push(self):
        """The server answers a rename with a new VERSIONS list: the same version stays selected, with its new name."""
        hp.model.apply({"versions": [version(1, "a"), version(2, "b")]})
        self.panel.table.selectRow(1)                            # version 1
        hp.model.apply({"versions": [version(1, "renamed"), version(2, "b")]})
        self.assertEqual(self.panel.selected_version()["name"], "renamed")
        self.assertTrue(self.panel.rename_btn.isEnabled())

    def test_role_change_signal_updates_the_buttons(self):
        hp.model.apply({"versions": [version(1)]})
        self.panel.table.selectRow(0)
        self.assertTrue(self.panel.restore_btn.isEnabled())
        self.app.set_role("viewer")                              # emits role_changed
        self.assertFalse(self.panel.restore_btn.isEnabled())

    def test_restore_needs_a_selection(self):
        hp.model.apply({"versions": [version(1)]})
        self.assertFalse(self.panel.restore_btn.isEnabled())

    def test_save_button_asks_for_a_name(self):
        with mock.patch.object(QInputDialog, "getText", return_value=("  My draft ", True)):
            self.panel.save_btn.click()
        self.assertEqual(self.app.net.sent, [(protocol.VERSION_SAVE, {"name": "My draft"}, None)])
        self.app.net.sent.clear()
        for answer in (("x", False), ("   ", True)):             # cancelled, or only blanks
            with mock.patch.object(QInputDialog, "getText", return_value=answer):
                self.panel.save_btn.click()
        self.assertEqual(self.app.net.sent, [])

    def test_restore_button_confirms_first(self):
        hp.model.apply({"versions": [version(7, "good one")]})
        self.panel.table.selectRow(0)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.panel.restore_btn.click()
        self.assertEqual(self.app.net.sent, [])
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes) as ask:
            self.panel.restore_btn.click()
        self.assertEqual(self.app.net.sent, [(protocol.VERSION_RESTORE, {"id": 7}, None)])
        self.assertIn("before restore good one", ask.call_args[0][2])    # the dialog names the safety copy

    def test_undo_redo_buttons(self):
        self.panel.undo_btn.click()
        self.panel.redo_btn.click()
        self.assertEqual([t for t, _, _ in self.app.net.sent], [protocol.UNDO, protocol.REDO])

    def test_footer_shows_count_seq_and_the_checksum(self):
        board = self.app.board
        board.objects["a"] = rect("a")
        board.objects["b"] = rect("b")
        board.last_seq = 12
        board.changed.emit(None)
        self.panel.refresh_footer()
        want = board_checksum(board.objects)[:12]
        self.assertEqual(self.panel.footer.text(), "Objects: 2   |   seq 12   |   checksum: " + want)

    def test_two_boards_with_the_same_content_show_the_same_checksum(self):
        """The point of the footer: identical boards give identical text, whatever the insertion order."""
        other = make_app("editor")
        panel2 = hp.HistoryPanel(other)
        for oid in ("a", "b", "c"):
            self.app.board.objects[oid] = rect(oid)
        for oid in ("c", "a", "b"):
            other.board.objects[oid] = rect(oid)
        for app, panel in ((self.app, self.panel), (other, panel2)):
            app.board.last_seq = 3
            app.board.changed.emit(None)
            panel.refresh_footer()
        self.assertEqual(self.panel.footer.text(), panel2.footer.text())
        panel2._timer.stop()

    def test_checksum_waits_while_operations_are_pending(self):
        board = self.app.board
        board.objects["a"] = rect("a")
        board.pending["hana-1"] = {"type": "OP_ADD", "sent": 0, "obj_id": "a", "gen": 0, "asked": False}
        board.changed.emit(None)
        self.panel.refresh_footer()
        self.assertIn("waiting for 1 pending", self.panel.footer.text())
        del board.pending["hana-1"]
        self.panel.refresh_footer()                              # the board is dirty, so it is computed now
        self.assertIn(board_checksum(board.objects)[:12], self.panel.footer.text())

    def test_checksum_is_not_recomputed_when_nothing_changed(self):
        board = self.app.board
        board.objects["a"] = rect("a")
        board.changed.emit(None)
        self.panel.refresh_footer()
        with mock.patch.object(BoardModel, "checksum", side_effect=AssertionError("recomputed")):
            self.panel.refresh_footer()
            self.panel.refresh_footer()


if __name__ == "__main__":
    unittest.main()
