# Whiteboard project rules (read before every task)
LAN-only multi-user collaborative whiteboard for a Computer Networks lab.
Python 3.10+, PyQt5,
socket + threading, TCP, frames = 4-byte big-endian length + UTF-8 JSON.
Deadline 13 Nov 2026.
- docs/CONTRACT.md is the single source of truth (message types, fields,
module APIs,
registries, file map). Never invent message types or fields. If something
is missing,
stop and tell me to ask P1 for a contract change.
- I will say which person I am (P1, P2, P3 or P4). Edit ONLY files whose line
1 is
'# OWNER: P<my number>'. If a task needs another owner's file, stop and
tell me.
- Extend the app only through the registries: register_tool, register_panel,
register_menu_action, register_renderer, register_editor,
register_shortcut,
on_message, @handler and the rooms hooks.
- Server: never trust the client. The server sets user, role, seq and ts and
calls
permissions.can(role, action) before changing anything. Room state changes
happen
under room.lock.
- Client: never block the GUI thread. Network data reaches widgets only
through Qt
signals and on_message handlers.
- No dependency except PyQt5. Only static imports (PyInstaller needs them).
- Docstring on every class and function; comment networking logic well. The code is
graded and every owner explains it in a viva.
- Team: P1 = Sharwan, P2 = Akshay, P3 = Janith, P4 = Harshith.
- Before committing, check git config user.email is set (it must be my GitHub email). The person's name goes in the commit prefix "[P<n> <Name>]", so user.name can be anything.
- Each task gets its own branch named <myname>/<topic> in lowercase (e.g. sharwan/net-client,
  akshay/pen-tool), created from origin/main. If my previous PR is not merged yet, branch from
  my previous branch instead and tell me.
- After each task: tell me how to test it, then commit with prefix "[P<n> <Name>]"
  (e.g. "[P1 Sharwan] add net client"); git fetch origin; git rebase origin/main;
  run python run_server.py and python run_client.py; git push -u origin HEAD;
  open a PR into main with gh pr create, title starting with the same prefix.
- main is protected: never push to main. Never force-push.
- Never add Co-Authored-By, Claude-Session or any AI attribution to commits or PR text.
