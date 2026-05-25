"""
GOT_client.py - Game of Thrones: The White Wolf
================================================
Client application for a 2-player cooperative maze game.

Connects to the game server over TCP and handles all rendering and input.
Manages 5 game screens: Home, Narrative, Waiting, Gameplay, and End.
Sends player movement to the server every frame and renders the game
state received back from the server.

Dependencies:
    pygame    — rendering, input, and game loop
    socket    — TCP connection to the server
    threading — background network thread

Protocol:
    Client sends : { "dx": int, "dy": int, "ready": bool }
    Server sends : { full game state as JSON, newline-terminated }
"""

import pygame
import socket
import json
import threading
import sys
import math
import logging

# ─── NETWORK CONFIG ───────────────────────────────────────────────────────────
SERVER_IP   = "127.0.0.1"
SERVER_PORT = 5555

# ─── DISPLAY / TILE CONSTANTS ─────────────────────────────────────────────────
TILE   = 48
ROWS   = 15
COLS   = 20
W      = COLS * TILE        # 960
H      = ROWS * TILE        # 720
FPS    = 60
SPEED  = 3                  # pixels per frame

PSIZE  = 32
DSIZE  = 48
ASIZE  = 24

# ─── COLOURS ──────────────────────────────────────────────────────────────────
BLACK   = (0,   0,   0)
GOLD    = (212, 175, 55)
WHITE   = (255, 255, 255)
DARK    = (15,  15,  30)
RED     = (180, 30,  30)
GREEN   = (80,  200, 80)
OVERLAY = (0,   0,   0,  160)

# ─── LOGGING ─────────────────────────────────────────────────────────────────

def init_client_logs():
    """
    Initialize the logging system and create a log file for the server.

    Configures the logger to capture all debug information and save it
    to 'GOT_client1.log' in overwrite mode ('w').
    """
    logging.basicConfig(
        filename='GOT_client2.log',
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filemode='w'
    )
    logging.info("--- GOT Client 1 Logging Initialized ---")


# ─── ASSET HELPERS ────────────────────────────────────────────────────────────

def make_floor_tile(size):
    """
    Create a floor tile surface with a subtle cross-hatch pattern.

    Args:
        size (int): Width and height of the tile in pixels.

    Returns:
        pygame.Surface: Rendered floor tile.
    """
    surf = pygame.Surface((size, size))
    surf.fill((180, 185, 175))
    pygame.draw.line(surf, (160,165,155), (0,0),    (size,size), 1)
    pygame.draw.line(surf, (160,165,155), (size,0), (0,size),    1)
    pygame.draw.rect(surf, (170,175,165), surf.get_rect(), 1)
    return surf


def make_wall_tile(size):
    """
    Create a wall tile surface with a dark bordered appearance.

    Args:
        size (int): Width and height of the tile in pixels.

    Returns:
        pygame.Surface: Rendered wall tile.
    """
    surf = pygame.Surface((size, size))
    surf.fill((40, 45, 65))
    pygame.draw.rect(surf, (55, 60, 80), surf.get_rect(), 2)
    return surf


def make_knight(size, color):
    """
    Draw a simple knight sprite using basic shapes.
    Used as a fallback when no PNG asset is found.

    Args:
        size  (int):   Width and height of the sprite in pixels.
        color (tuple): RGB color for the knight's body.

    Returns:
        pygame.Surface: Rendered knight sprite with transparency.
    """
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = size // 2
    s = pygame.Surface((size, 8), pygame.SRCALPHA)
    pygame.draw.ellipse(s, (0,0,0,80), (0,0,size,8))
    surf.blit(s, (0, size-8))
    pygame.draw.polygon(surf, color, [(cx,4),(cx-12,size-10),(cx+12,size-10)])
    pygame.draw.circle(surf, (200,200,220), (cx, size//3), 8)
    return surf


def make_dragon(size):
    """
    Draw a simple dragon sprite using basic shapes.
    Used as a fallback when no PNG asset is found.

    Args:
        size (int): Width and height of the sprite in pixels.

    Returns:
        pygame.Surface: Rendered dragon sprite with transparency.
    """
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    cx, cy = size//2, size//2
    s = pygame.Surface((size, 10), pygame.SRCALPHA)
    pygame.draw.ellipse(s, (0,0,0,80), (0,0,size,10))
    surf.blit(s, (0, size-10))
    pygame.draw.circle(surf, (20, 20, 20), (cx, cy+4), size//3)
    pygame.draw.polygon(surf, (10,10,10), [(cx-4,cy),(cx-size//2+2,cy-10),(cx-2,cy+8)])
    pygame.draw.polygon(surf, (10,10,10), [(cx+4,cy),(cx+size//2-2,cy-10),(cx+2,cy+8)])
    pygame.draw.circle(surf, (255,80,0), (cx, cy+2), 4)
    return surf


def make_artifact(size):
    """
    Draw a simple gold triangle artifact sprite.
    Used as a fallback when no PNG asset is found.

    Args:
        size (int): Width and height of the sprite in pixels.

    Returns:
        pygame.Surface: Rendered artifact sprite with transparency.
    """
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = size // 2
    pygame.draw.polygon(surf, GOLD, [(cx, 2),(2, size-4),(size-2, size-4)])
    pygame.draw.polygon(surf, (255,220,100), [(cx,6),(5,size-7),(size-5,size-7)], 1)
    return surf


def load_font(size, bold=False):
    """
    Load the default Pygame font directly and apply styling.
    Uses cached font objects to prevent performance degradation.

    Args:
        size (int): Font size in points.
        bold (bool, optional): Whether to apply a bold style. Defaults to False.

    Returns:
        pygame.font.Font: The styled default Pygame font object.
    """
    font = pygame.font.Font(None, size)
    font.set_bold(bold)
    return font

# ─── NETWORK STATE ────────────────────────────────────────────────────────────
net_state   = {}
net_lock    = threading.Lock()
send_queue  = []
send_lock   = threading.Lock()


def network_thread(sock):
    """
    Background thread that handles all network communication.
    Sends queued movement packets to the server and receives
    the latest game state, updating net_state on every frame.

    Args:
        sock (socket.socket): The connected TCP socket.
    """
    buf = ""
    while True:
        try:
            with send_lock:
                # Create a shallow copy to quickly release the lock before blocking on network I/O
                packets = send_queue[:]
                send_queue.clear()
            for pkt in packets:
                sock.sendall((json.dumps(pkt) + "\n").encode())

            if not packets:
                # Send an empty movement packet to keep the connection alive if no input is queued
                sock.sendall((json.dumps({"dx": 0, "dy": 0}) + "\n").encode())

            data = sock.recv(8192).decode()
            buf += data
            # Process all complete newline-terminated JSON payloads in the TCP stream buffer
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    parsed = json.loads(line)
                    with net_lock:
                        net_state.clear()
                        net_state.update(parsed)
        except Exception as e:
            logging.error(f"Network thread error: {e}")
            break


# ─── DRAW HELPERS ─────────────────────────────────────────────────────────────

def draw_shadow(surf, cx, cy, rx, ry):
    """
    Draw a soft elliptical shadow beneath a sprite.

    Args:
        surf (pygame.Surface): Surface to draw on.
        cx   (int): Center X of the shadow in pixels.
        cy   (int): Center Y of the shadow in pixels.
        rx   (int): Horizontal radius of the shadow.
        ry   (int): Vertical radius of the shadow.
    """
    s = pygame.Surface((rx*2, ry*2), pygame.SRCALPHA)
    pygame.draw.ellipse(s, (0,0,0,70), (0,0,rx*2,ry*2))
    surf.blit(s, (cx-rx, cy-ry))


def draw_label(surf, text, x, y, font, color=WHITE):
    """
    Draw a centered text label above a sprite.

    Args:
        surf  (pygame.Surface):   Surface to draw on.
        text  (str):              Text to display.
        x     (int):              Left edge of the sprite in pixels.
        y     (int):              Top edge of the sprite in pixels.
        font  (pygame.font.Font): Font to use.
        color (tuple):            RGB text color. Defaults to WHITE.
    """
    label = font.render(text, True, color)
    rect  = label.get_rect(center=(x + PSIZE//2, y - 10))
    surf.blit(label, rect)


def draw_map(surf, map_grid, floor_tile, wall_tile):
    """
    Render the full tile map to the screen.

    Args:
        surf       (pygame.Surface):    Surface to draw on.
        map_grid   (list[list[int]]):   2D grid where 1=wall, 0=floor.
        floor_tile (pygame.Surface):    Pre-rendered floor tile surface.
        wall_tile  (pygame.Surface):    Pre-rendered wall tile surface.
    """
    for r, row in enumerate(map_grid):
        for c, cell in enumerate(row):
            tile = wall_tile if cell == 1 else floor_tile
            surf.blit(tile, (c*TILE, r*TILE))


def draw_dark_overlay(surf, alpha=160):
    """
    Draw a semi-transparent black overlay over the entire screen.
    Used for end screens drawn on top of the game world.

    Args:
        surf  (pygame.Surface): Surface to draw on.
        alpha (int):            Opacity from 0 (transparent) to 255 (opaque).
    """
    ov = pygame.Surface((W, H), pygame.SRCALPHA)
    ov.fill((0, 0, 0, alpha))
    surf.blit(ov, (0, 0))


# ─── SCREENS ──────────────────────────────────────────────────────────────────

def home_screen(surf, tick):
    """
    Render the animated home/title screen.
    Shows the game title with sparkling background dots.
    Displays a blinking "Press SPACE to Start" prompt.

    Args:
        surf (pygame.Surface): Surface to draw on.
        tick (int):            Current frame count used for animations.
    """
    surf.fill(DARK)
    for i in range(40):
        # Calculate a pseudo-random X coordinate that scrolls slowly based on the frame tick
        x = (i * 137 + tick//3) % W
        # Calculate a pseudo-random Y coordinate that scrolls at a different speed for a simple parallax effect
        y = (i * 89  + tick//5) % H
        # Use a sine wave to create a pulsing opacity/brightness effect for each background star
        a = int(128 + 127 * math.sin(tick * 0.04 + i))
        pygame.draw.circle(surf, (a, a//2, 0), (x, y), 1)

    title_font = load_font(72, bold=True)
    sub_font   = load_font(26)
    hint_font  = load_font(20)

    title = title_font.render("THE WHITE WOLF", True, GOLD)
    surf.blit(title, title.get_rect(center=(W//2, H//2 - 80)))

    sub = sub_font.render("Game of Thrones: The White Wolf", True, (200, 200, 200))
    surf.blit(sub, sub.get_rect(center=(W//2, H//2 - 20)))

    # Toggle rendering every 30 frames to create a blinking text effect
    if (tick // 30) % 2 == 0:
        hint = hint_font.render("Press SPACE to Start", True, WHITE)
        surf.blit(hint, hint.get_rect(center=(W//2, H//2 + 60)))


NARRATIVE = [
    "THE WHITE WOLF",
    "",
    "The Kingdom is in shadows.",
    "The dead march south.",
    "",
    "The stolen North Artifacts — forged in the fires",
    "of the First Men — have been scattered across",
    "the ancient fortress of Harrenhal.",
    "",
    "Two brave Knights must venture into the labyrinth",
    "and retrieve every Artifact before",
    "the Dragon Balerion the Black Dread wakes and hunts them down.",
    "",
    "Should both Knights fall, the Kingdom is lost.",
    "Should they succeed, the North shall remember.",
    "",
    "— Press SPACE to begin the quest —",
]


def narrative_screen(surf, tick):
    """
    Render the story screen with a fade-in text effect.
    Each line fades in sequentially based on the current tick.

    Args:
        surf (pygame.Surface): Surface to draw on.
        tick (int):            Current frame count used for the fade-in timing.
    """
    surf.fill(DARK)
    font      = load_font(22)
    title_fnt = load_font(36, bold=True)
    y = 60
    for i, line in enumerate(NARRATIVE):
        # Calculate staggered fade-in opacity: delays start by 'i*6' frames, and clamps between 0-255
        alpha = min(255, max(0, (tick - i*6) * 8))
        if i == 0:
            txt = title_fnt.render(line, True, GOLD)
        else:
            txt = font.render(line, True, (alpha, alpha, alpha))
        surf.blit(txt, txt.get_rect(center=(W//2, y)))
        y += 38


def waiting_screen(surf, tick):
    """
    Render the waiting screen shown until both players are connected.
    Displays an animated ellipsis to indicate the game is waiting.

    Args:
        surf (pygame.Surface): Surface to draw on.
        tick (int):            Current frame count used for the dot animation.
    """
    surf.fill(DARK)
    font = load_font(28)
    msg  = "Waiting for the second Knight to join the quest..."
    # Cycle through 0 to 3 dots appended to the string every 20 frames
    dots = "." * ((tick // 20) % 4)
    txt  = font.render(msg + dots, True, GOLD)
    surf.blit(txt, txt.get_rect(center=(W//2, H//2)))


def victory_screen(surf, p0score, p1score):
    """
    Render the victory end screen with both players' final scores.

    Args:
        surf     (pygame.Surface): Surface to draw on.
        p0score  (int):            Final score of Player 0.
        p1score  (int):            Final score of Player 1.
    """
    draw_dark_overlay(surf, 200)
    f1 = load_font(64, bold=True)
    f2 = load_font(30)
    f3 = load_font(22)
    t1 = f1.render("VICTORY!", True, GOLD)
    t2 = f2.render("The Kingdom is Saved.", True, WHITE)
    t3 = f3.render(f"Knight 1: {p0score} Artifacts    Knight 2: {p1score} Artifacts", True, (200,200,200))
    t4 = f3.render("Press ESC to quit.", True, (160,160,160))
    surf.blit(t1, t1.get_rect(center=(W//2, H//2-100)))
    surf.blit(t2, t2.get_rect(center=(W//2, H//2-30)))
    surf.blit(t3, t3.get_rect(center=(W//2, H//2+30)))
    surf.blit(t4, t4.get_rect(center=(W//2, H//2+80)))


def gameover_screen(surf):
    """
    Render the game over end screen.

    Args:
        surf (pygame.Surface): Surface to draw on.
    """
    draw_dark_overlay(surf, 200)
    f1 = load_font(64, bold=True)
    f2 = load_font(28)
    t1 = f1.render("GAME OVER", True, RED)
    t2 = f2.render("Both Knights have fallen. The Kingdom is lost.", True, (200,200,200))
    t3 = f2.render("Press ESC to quit.", True, (160,160,160))
    surf.blit(t1, t1.get_rect(center=(W//2, H//2-80)))
    surf.blit(t2, t2.get_rect(center=(W//2, H//2-10)))
    surf.blit(t3, t3.get_rect(center=(W//2, H//2+50)))


def sacrifice_overlay(surf):
    """
    Render a red overlay shown when the local player has died
    but the other player is still alive.

    Args:
        surf (pygame.Surface): Surface to draw on.
    """
    ov = pygame.Surface((W, H), pygame.SRCALPHA)
    ov.fill((80, 0, 0, 140))
    surf.blit(ov, (0,0))
    f1 = load_font(34, bold=True)
    f2 = load_font(22)
    lines = [
        "You have sacrificed yourself heroically!",
        "You are now depending on the other Knight",
        "to save the Kingdom...",
    ]
    y = H//2 - 60
    for line in lines:
        t = f1.render(line, True, GOLD) if y == H//2-60 else f2.render(line, True, WHITE)
        surf.blit(t, t.get_rect(center=(W//2, y)))
        y += 44


# ─── HUD ──────────────────────────────────────────────────────────────────────

def draw_hud(surf, state, my_id):
    """
    Render the heads-up display bar at the top of the screen.
    Shows both players' scores, death status, and artifact progress.
    Marks the local player's knight with a star symbol.

    Args:
        surf   (pygame.Surface): Surface to draw on.
        state  (dict):           Latest game state snapshot from the server.
        my_id  (int):            The local player's ID (0 or 1).
    """
    f = load_font(20, bold=True)
    ps = state.get("players", {})
    p0 = ps.get("0", {})
    p1 = ps.get("1", {})

    # Count how many artifacts have their 'collected' boolean set to True
    total = sum(1 for a in state.get("artifacts",[]) if a["collected"])
    arts  = len(state.get("artifacts", []))

    hud = pygame.Surface((W, 36), pygame.SRCALPHA)
    hud.fill((0,0,0,140))
    surf.blit(hud, (0, 0))

    tag0 = " ★" if my_id == 0 else ""
    tag1 = " ★" if my_id == 1 else ""

    t0 = f.render(f"Knight 1{tag0}: {p0.get('score',0)} pts {'☠' if p0.get('is_dead') else ''}", True,
                  RED if p0.get("is_dead") else (150,220,255))
    t1 = f.render(f"Knight 2{tag1}: {p1.get('score',0)} pts {'☠' if p1.get('is_dead') else ''}", True,
                  RED if p1.get("is_dead") else (255,220,150))
    ta = f.render(f"Artifacts: {total}/{arts}", True, GOLD)
    surf.blit(t0, (10, 8))
    surf.blit(t1, (W//2 - 80, 8))
    surf.blit(ta, (W - 170, 8))


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    """
    Entry point for the client.
    Initialises pygame, connects to the server, starts the network thread,
    and runs the main game loop. Manages the 5 screen state machine and
    handles keyboard input, rendering, and server communication every frame.

    Screen states:
        0 — Home screen
        1 — Narrative screen
        2 — Waiting for second player
        3 — Gameplay
        4 — End screen (victory or game over)
    """
    init_client_logs()
    logging.info("GOT Client starting.")

    pygame.init()
    logging.info("Pygame initialised.")

    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("GOT: The White Wolf")
    clock  = pygame.time.Clock()

    floor_tile = make_floor_tile(TILE)
    wall_tile  = make_wall_tile(TILE)
    knight0    = make_knight(PSIZE, (200,200,230))
    knight1    = make_knight(PSIZE, (100,100,140))
    dragon_spr = make_dragon(DSIZE)
    artifact_s = make_artifact(ASIZE)
    logging.info("Assets loaded.")

    label_font = load_font(14)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((SERVER_IP, SERVER_PORT))
        sock.setblocking(True)
        logging.info(f"Connected to server at {SERVER_IP}:{SERVER_PORT}")
    except ConnectionRefusedError:
        logging.error(f"Cannot connect to server at {SERVER_IP}:{SERVER_PORT}. Is GOT_server.py running?")
        sys.exit(1)

    t = threading.Thread(target=network_thread, args=(sock,), daemon=True)
    t.start()
    logging.info("Network thread started.")

    state_id   = 0
    tick       = 0
    my_id      = None
    cached_map = None
    death_logged = False

    while True:
        clock.tick(FPS)
        tick += 1

        # ── Events ──────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                logging.info("Window closed by user.")
                pygame.quit(); sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    logging.info("ESC pressed — exiting.")
                    pygame.quit(); sys.exit()
                if event.key == pygame.K_SPACE:
                    if state_id == 0:
                        state_id = 1
                        tick = 0
                        logging.info("Moved to narrative screen.")
                    elif state_id == 1:
                        state_id = 2
                        logging.info("Moved to waiting screen.")

        # ── Get latest network state ──────────────────────────────────────
        with net_lock:
            snap = dict(net_state)

        if snap:
            if my_id is None:
                my_id = snap.get("your_id")
                logging.info(f"Assigned as Player {my_id}.")
            if cached_map is None and "map" in snap:
                cached_map = snap["map"]
                logging.info("Map cached from server.")

        # ── State transitions ─────────────────────────────────────────────
        if state_id >= 2 and snap:
            total_conn = snap.get("total_connected", 0)
            if state_id == 2 and total_conn >= 2:
                state_id = 3
                logging.info("Both players connected — entering gameplay.")
            if state_id == 3:
                if snap.get("victory"):
                    state_id = 4
                    logging.info("VICTORY — transitioning to end screen.")
                elif snap.get("game_over"):
                    state_id = 4
                    logging.info("GAME OVER — transitioning to end screen.")

        # ── Player input (only in gameplay) ──────────────────────────────
        if state_id == 3 and snap:
            ps   = snap.get("players", {})
            my_p = ps.get(str(my_id), {})
            if not my_p.get("is_dead", True):
                keys = pygame.key.get_pressed()
                dx, dy = 0, 0

                # Map directional keys (Arrows or WASD) to movement vectors
                if keys[pygame.K_LEFT]  or keys[pygame.K_a]: dx = -SPEED
                if keys[pygame.K_RIGHT] or keys[pygame.K_d]: dx =  SPEED
                if keys[pygame.K_UP]    or keys[pygame.K_w]: dy = -SPEED
                if keys[pygame.K_DOWN]  or keys[pygame.K_s]: dy =  SPEED

                with send_lock:
                    send_queue.append({"dx": dx, "dy": dy, "ready": True})
            elif my_p.get("is_dead") and not death_logged:
                logging.warning(f"Player {my_id} is dead.")
                death_logged = True

        # ── Render ────────────────────────────────────────────────────────
        screen.fill(DARK)

        if state_id == 0:
            home_screen(screen, tick)

        elif state_id == 1:
            narrative_screen(screen, tick)

        elif state_id == 2:
            waiting_screen(screen, tick)

        elif state_id in (3, 4):
            if cached_map:
                draw_map(screen, cached_map, floor_tile, wall_tile)

            if snap:
                for art in snap.get("artifacts", []):
                    if not art["collected"]:
                        screen.blit(artifact_s, (art["x"], art["y"]))

                ps      = snap.get("players", {})
                sprites = {0: knight0, 1: knight1}
                names   = {0: "Knight 1", 1: "Knight 2"}
                for pid_str, p in ps.items():
                    pid = int(pid_str)
                    spr = sprites[pid]
                    px, py = int(p["x"]), int(p["y"])
                    if p.get("is_dead"):
                        # Render dead players as semi-transparent 'ghosts'
                        ghost = spr.copy()
                        ghost.set_alpha(60)
                        screen.blit(ghost, (px, py))
                    else:
                        draw_shadow(screen, px+PSIZE//2, py+PSIZE-4, PSIZE//2, 5)
                        screen.blit(spr, (px, py))
                        draw_label(screen, names[pid], px, py, label_font,
                                   (150,220,255) if pid==0 else (255,220,150))

                dr = snap.get("dragon", {})
                if dr:
                    dx_pos = int(dr["x"])
                    dy_pos = int(dr["y"])
                    draw_shadow(screen, dx_pos+DSIZE//2, dy_pos+DSIZE-4, DSIZE//2, 7)
                    screen.blit(dragon_spr, (dx_pos, dy_pos))
                    draw_label(screen, "Balerion", dx_pos, dy_pos, label_font, BLACK)

                draw_hud(screen, snap, my_id)

                if my_id is not None:
                    my_data = ps.get(str(my_id), {})
                    if my_data.get("is_dead") and state_id == 3:
                        sacrifice_overlay(screen)

            if state_id == 4:
                if snap.get("victory"):
                    ps = snap.get("players", {})
                    victory_screen(screen,
                                   ps.get("0",{}).get("score",0),
                                   ps.get("1",{}).get("score",0))
                else:
                    gameover_screen(screen)

        pygame.display.flip()


if __name__ == "__main__":
    main()