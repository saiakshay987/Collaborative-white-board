LAN WHITEBOARD - HOW TO RUN IT (no Python needed)
=================================================
One laptop runs WhiteboardServer. Every laptop (the server laptop too) runs WhiteboardClient.

1) Put all laptops on the SAME network.
   A phone hotspot is easiest. A home router also works. Internet is NOT needed.
   Campus/guest Wi-Fi often blocks laptops from seeing each other ("client isolation"),
   so use a hotspot or a router with client isolation turned off.

2) Server laptop only, once: open the firewall.
   Windows: Start > type PowerShell > right-click > "Run as administrator", go to this folder:
       cd "C:\path\to\this\folder"
       powershell -ExecutionPolicy Bypass -File .\open_firewall.ps1
   Linux: see FIREWALL_LINUX.txt (sudo ufw allow 5050/tcp).

3) Double-click WhiteboardServer. A black window opens and shows lines like:
       Clients should connect to: 192.168.43.57 : 5050
   That number is the server's IP address: tell it to everyone. Keep this window open;
   closing it stops the whiteboard. Rooms are saved in the server_data folder next to it.
   If Windows asks "Allow access?", tick Private AND Public networks and click Allow.

4) On every laptop, double-click WhiteboardClient.
   - Host: create a new room. You get a 6-letter room code: tell it to the others.
   - Join: type the server IP from step 3, the room code and your name.
   (Linux: if double-click does nothing, run ./WhiteboardClient in a terminal.)

5) If it does not work
   Problem                         What to do
   ------------------------------  ------------------------------------------------------
   "Timed out" when joining        Wrong network, or client isolation: check both laptops
                                   are on the same hotspot. Then check the firewall (step 2).
   "Connection refused"            The IP is right but the server is not running, or uses
                                   another port: start WhiteboardServer and look at its window.
   Works on the server laptop      Firewall on the server laptop. Run step 2 again as
   but not on the others           administrator; on the "Allow access?" pop-up choose Allow.
   Wrong IP                        Use the line "Clients should connect to: ..." of the server,
                                   not 127.0.0.1 and not a "virtual adapter" address.
   Server: "Cannot listen on port" Another server already uses port 5050. Close it, or start
                                   from a terminal: WhiteboardServer --port 5051 (and join with IP:5051).
   Antivirus / SmartScreen warning The program is not signed. Windows SmartScreen: "More info" >
                                   "Run anyway". If the antivirus deleted it, unzip it again and
                                   add the folder as an exception.
   Something else is wrong         Run WhiteboardClient-console instead: its black window shows
                                   the error message. Send a screenshot of it.
