"""
GOT_server.py - Game of Thrones: The White Wolf
================================================
Server application for a 2-player cooperative maze game.

Accepts 2 TCP client connections on port 5555.
Manages all game logic including player movement, collision detection,
artifact collection, dragon AI, and win/lose conditions.
Sends the full game state back to both clients after every update.

Architecture:
    - 1 main thread       : accepts incoming connections
    - 2 client threads    : one per player, handles input and state broadcast
    - 1 dragon AI thread  : moves the dragon toward players using BFS pathfinding
    - 1 shared lock       : protects all mutable game state across threads

Protocol:
    Client sends : { "dx": int, "dy": int, "ready": bool }
    Server sends : { full game state as JSON, newline-terminated }
"""
import socket
import threading
import json
import time
import random
import math
import logging
from collections import deque

# ─── NETWORK CONFIG ───────────────────────────────────────────────────────────
HOST = "0.0.0.0"
PORT = 5555

# ─── MAP DEFINITION ───────────────────────────────────────────────────────────
MAP_GRID = [
    [1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],
    [1,0,0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0,0,1],
    [1,0,1,1,0,1,1,0,1,1,1,1,0,1,1,0,1,1,0,1],
    [1,0,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,1],
    [1,0,1,0,1,1,0,1,1,0,1,1,1,0,1,1,0,1,0,1],
    [1,0,0,0,1,0,0,0,1,0,0,0,1,0,0,1,0,0,0,1],
    [1,1,1,0,1,0,1,0,0,0,1,0,0,0,1,0,1,1,1,1],
    [1,0,0,0,0,0,1,1,1,0,0,1,1,0,1,0,0,0,0,1],
    [1,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,1,1,0,1],
    [1,0,1,0,0,0,1,0,1,1,0,1,1,0,1,0,0,1,0,1],
    [1,0,0,0,1,0,1,0,0,0,0,0,0,0,1,0,1,0,0,1],
    [1,0,1,0,1,0,0,0,1,0,1,0,1,0,0,0,1,0,1,1],
    [1,0,1,0,0,0,1,0,1,0,1,0,1,0,1,0,0,0,1,1],
    [1,0,0,0,1,0,0,0,0,0,0,0,0,0,0,0,1,0,0,1],
    [1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1],
]

TILE  = 48
ROWS  = len(MAP_GRID)
COLS  = len(MAP_GRID[0])
PSIZE = 32
DSIZE = 48
ASIZE = 24

# ─── SHARED GAME STATE ────────────────────────────────────────────────────────
lock = threading.Lock()

players = {
    0: {"x": 1*TILE+8, "y": 1*TILE+8, "score": 0, "is_dead": False},
    1: {"x": 18*TILE+8, "y": 13*TILE+8, "score": 0, "is_dead": False},
}

dragon = {
    "x": float(10*TILE), "y": float(7*TILE),
    "vx": 2.0, "vy": 1.5,
}


# ─── LOGGING ─────────────────────────────────────────────────────────────────

def init_server_logs():
    """
    Initialize the logging system and create a log file for the server.

    Configures the logger to capture all debug information and save it
    to 'GOT_server.log' in overwrite mode ('w').
    """
    logging.basicConfig(
        filename='GOT_server.log',
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        filemode='w'
    )
    logging.info("--- GOT Server Logging Initialized ---")

init_server_logs()

# ─── ARTIFACT PLACEMENT ──────────────────────────────────────────────────────

def random_floor_tiles(n):
    """
    Generate n artifact positions placed randomly on floor tiles.
    Excludes tiles that are too close to either player's spawn point.

    Args:
        n (int): Number of artifacts to place.

    Returns:
        list[dict]: Each dict contains x, y (pixel position) and collected (bool).
    """
    tiles = []
    for r in range(ROWS):
        for c in range(COLS):
            if MAP_GRID[r][c] == 0:
                tiles.append((c*TILE + TILE//2 - ASIZE//2,
                               r*TILE + TILE//2 - ASIZE//2))

    # Filter out coordinates that fall within the starting safe zones of both players
    tiles = [(x,y) for (x,y) in tiles if
             not (x < 3*TILE and y < 3*TILE) and
             not (x > 16*TILE and y > 12*TILE)]
    random.shuffle(tiles)

    # Slice the top 'n' shuffled coordinates and format them into game state dictionaries
    result = [{"x": x, "y": y, "collected": False} for (x, y) in tiles[:n]]
    logging.info(f"Placed {n} artifacts on floor tiles.")
    return result


artifacts       = random_floor_tiles(20)
game_over       = False
victory         = False
game_abandoned  = False  # עדכון: הוספת המשתנה הגלובלי למצב נטישה
connected       = [None, None]
connected_count = 0
players_ready   = set()


# ─── COLLISION HELPERS ────────────────────────────────────────────────────────

def is_wall_rect(rx, ry, rw, rh):
    """
    Check whether a rectangle overlaps any wall tile in the map.

    Args:
        rx (float): Left edge of the rectangle in pixels.
        ry (float): Top edge of the rectangle in pixels.
        rw (int):   Width of the rectangle in pixels.
        rh (int):   Height of the rectangle in pixels.

    Returns:
        bool: True if the rectangle overlaps a wall tile, False otherwise.
    """
    left  = int(rx // TILE)             # Calculate leftmost grid column index
    right = int((rx + rw - 1) // TILE)  # Calculate rightmost grid column index
    top   = int(ry // TILE)             # Calculate topmost grid row index
    bot   = int((ry + rh - 1) // TILE)  # Calculate bottommost grid row index

    # Iterate only over the grid cells that intersect with the rectangle's bounding box
    for r in range(max(0,top), min(ROWS, bot+1)):
        for c in range(max(0,left), min(COLS, right+1)):
            if MAP_GRID[r][c] == 1:
                return True
    return False


# ─── BFS PATHFINDING ─────────────────────────────────────────────────────────

def bfs_next_step(start_tile, goal_tile):
    """
    Find the next tile to move into on the shortest path from start to goal.
    Uses Breadth-First Search on the map grid, only crossing floor tiles.

    Args:
        start_tile (tuple): (row, col) of the starting tile.
        goal_tile  (tuple): (row, col) of the destination tile.

    Returns:
        tuple: (row, col) of the next tile to step into.
        None:  if already at the goal or no path exists.
    """
    sr, sc = start_tile
    gr, gc = goal_tile
    if (sr, sc) == (gr, gc):
        return None

    visited = {(sr, sc)}
    queue   = deque()

    for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
        nr, nc = sr+dr, sc+dc
        if 0 <= nr < ROWS and 0 <= nc < COLS and MAP_GRID[nr][nc] == 0:
            # Store (current_row, current_col, initial_first_step) so the path remembers its origin
            queue.append((nr, nc, (nr, nc)))
            visited.add((nr, nc))

    while queue:
        r, c, first = queue.popleft()
        if (r, c) == (gr, gc):
            return first
        for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
            nr, nc = r+dr, c+dc
            if 0 <= nr < ROWS and 0 <= nc < COLS and MAP_GRID[nr][nc] == 0 and (nr,nc) not in visited:
                visited.add((nr, nc))
                # Propagate the 'first' step variable forward to the next nodes in the path
                queue.append((nr, nc, first))

    logging.warning(f"BFS found no path from {start_tile} to {goal_tile}.")
    return None


# ─── DRAGON AI THREAD ─────────────────────────────────────────────────────────

def dragon_ai():
    """
    Background thread that moves the dragon at ~60 fps.
    Uses BFS pathfinding to chase the nearest living player through the maze.
    Marks a player as dead on contact. Sets game_over or victory when
    the respective end condition is reached.
    Does not move until both players are connected and at least one is
    on the gameplay screen.
    """
    global game_over, victory
    logging.info("Dragon AI thread started — waiting for players to be ready.")

    while True:
        time.sleep(1/60)
        with lock:
            if connected_count < 2 or len(players_ready) < 1:
                continue
            # עדכון: אם המשחק ננטש, הדרקון יעצור מיד בדיוק כמו ב-game_over
            if game_over or victory or game_abandoned:
                continue

            d = dragon
            dragon_speed = 2.0

            alive = [players[i] for i in (0, 1) if not players[i]["is_dead"]]
            if alive:
                # Find the living player with the shortest Euclidean distance from the dragon
                target = min(alive, key=lambda player: math.hypot(player["x"] - d["x"], player["y"] - d["y"]))

                # Calculate center points in grid indices for the BFS algorithm
                d_tile = (int(d["y"] + DSIZE//2) // TILE, int(d["x"] + DSIZE//2) // TILE)
                p_tile = (int(target["y"] + PSIZE//2) // TILE, int(target["x"] + PSIZE//2) // TILE)

                next_tile = bfs_next_step(d_tile, p_tile)

                if next_tile:
                    next_nr, next_nc = next_tile
                    # Convert the grid destination back to absolute top-left pixel coordinates
                    target_px = next_nc * TILE + TILE // 2 - DSIZE // 2
                    target_py = next_nr * TILE + TILE // 2 - DSIZE // 2

                    dx = target_px - d["x"]
                    dy = target_py - d["y"]
                    # Calculate vector magnitude, defaulting to 1 to prevent division by zero
                    dist = math.hypot(dx, dy) or 1

                    if dist <= dragon_speed:
                        d["x"] = float(target_px)
                        d["y"] = float(target_py)
                    else:
                        # Normalize the directional vector and multiply by speed
                        d["x"] += (dx / dist) * dragon_speed
                        d["y"] += (dy / dist) * dragon_speed

            # Check dragon-player collision
            for pid, p in players.items():
                if p["is_dead"]:
                    continue
                # Center-based Axis-Aligned Bounding Box (AABB) overlap check
                if (abs(p["x"] - d["x"]) < (PSIZE + DSIZE) // 2 and
                        abs(p["y"] - d["y"]) < (PSIZE + DSIZE) // 2):
                    p["is_dead"] = True
                    logging.info(f"Dragon killed Player {pid}!")

            # Check end conditions
            if all(p["is_dead"] for p in players.values()):
                game_over = True
                logging.info("GAME OVER — both knights have fallen.")

            if all(a["collected"] for a in artifacts):
                victory = True
                logging.info("VICTORY — all artifacts have been collected!")


# ─── CLIENT HANDLER ───────────────────────────────────────────────────────────

def recv_exact(sock, num_bytes):
    """
    Helper function to ensure exact byte reading from the TCP stream.
    """
    data = bytearray()
    while len(data) < num_bytes:
        packet = sock.recv(num_bytes - len(data))
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)


def handle_client(conn, player_id):
    """
    Handles communication and game logic for a single connected client.
    Updated to use 4-byte length prefix protocol.
    """
    global connected_count, game_over, game_abandoned
    logging.info(f"Player {player_id} connected.")

    while True:
        try:
            # --- 1. Inbound Deframer (Receive Data) ---

            # Read exactly 4 bytes for the Header
            header_data = recv_exact(conn, 4)
            if not header_data:
                logging.warning(f"Player {player_id} sent empty data — disconnecting.")
                break

            # Decode the length from the 4-byte Header
            msg_length = int.from_bytes(header_data, byteorder='big')

            # Read the exact payload based on the length we just received
            payload_data = recv_exact(conn, msg_length)
            if not payload_data:
                logging.warning(f"Player {player_id} payload incomplete — disconnecting.")
                break

            # Parse the JSON data
            data = json.loads(payload_data.decode('utf-8'))

            # --- 2. Game Logic (Shared State Update) ---
            with lock:
                if not game_over and not victory and not game_abandoned:
                    p = players[player_id]

                    if data.get("ready"):
                        if player_id not in players_ready:
                            players_ready.add(player_id)
                            logging.info(f"Player {player_id} is on the game screen. Ready: {players_ready}")

                    if not p["is_dead"]:
                        dx = data.get("dx", 0)
                        dy = data.get("dy", 0)
                        nx = p["x"] + dx
                        ny = p["y"] + dy

                        if not is_wall_rect(nx, p["y"], PSIZE, PSIZE):
                            p["x"] = max(0, min(nx, COLS * TILE - PSIZE))
                        if not is_wall_rect(p["x"], ny, PSIZE, PSIZE):
                            p["y"] = max(0, min(ny, ROWS * TILE - PSIZE))

                        for art in artifacts:
                            if art["collected"]:
                                continue
                            if (abs(p["x"] - art["x"]) < PSIZE and
                                    abs(p["y"] - art["y"]) < PSIZE):
                                art["collected"] = True
                                p["score"] += 1
                                logging.info(f"Player {player_id} collected an artifact! Score: {p['score']}")

                state = {
                    "your_id": player_id,
                    "total_connected": connected_count,
                    "players": {str(i): {
                        "x": players[i]["x"],
                        "y": players[i]["y"],
                        "score": players[i]["score"],
                        "is_dead": players[i]["is_dead"],
                    } for i in (0, 1)},
                    "dragon": {"x": dragon["x"], "y": dragon["y"]},
                    "artifacts": [{"x": a["x"], "y": a["y"],
                                   "collected": a["collected"]} for a in artifacts],
                    "game_over": game_over,
                    "victory": victory,
                    "game_abandoned": game_abandoned,
                    "map": MAP_GRID,
                }

            # --- 3. Outbound Serializer (Send Data) ---

            # Encode JSON to bytes
            payload_out = json.dumps(state).encode('utf-8')

            # Calculate length and pack into a 4-byte header
            header_out = len(payload_out).to_bytes(4, byteorder='big')

            # Send Header followed immediately by the payload
            conn.sendall(header_out + payload_out)

        except ConnectionResetError:
            logging.warning(f"Player {player_id} connection reset.")
            break
        except BrokenPipeError:
            logging.warning(f"Player {player_id} connection broken.")
            break
        except Exception as e:
            logging.error(f"Player {player_id} network or JSON error: {e}")
            break

    # Cleanup: When the loop breaks, the player has left.
    logging.info(f"Player {player_id} disconnected.")
    conn.close()

    with lock:
        connected_count -= 1
        if not game_over and not victory and not game_abandoned:
            game_abandoned = True
            players[player_id]["is_dead"] = True
            logging.info(f"Player {player_id} abandoned the match. Global game_abandoned set to True.")

        logging.info(f"Connected count: {connected_count}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    """
    Entry point for the server.
    Binds to HOST:PORT, starts the dragon AI thread, accepts exactly
    2 player connections, and keeps the process alive until interrupted.
    """
    logging.info(f"Starting GOT Server on {HOST}:{PORT}")

    global connected_count
    # AF_INET = IPv4, SOCK_STREAM = TCP (reliable connection protocol)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # OS setting: Without this, if you stop and restart the server quickly,
    # the OS will block the port for a minute. This bypasses that lock.
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(2)  # The server queue: allows up to 2 players to connect
    logging.info(f"Listening on {HOST}:{PORT} — waiting for 2 Knights...")

    # Start the "Brain" of the game: A background task (thread)
    # that handles the Dragon's AI independently of player connections.
    t_dragon = threading.Thread(target=dragon_ai, daemon=True)
    t_dragon.start()
    logging.info("Dragon AI thread launched.")

    # Connection Loop: The server pauses at 'server.accept()' until someone joins.
    # We repeat this exactly twice to form our team of 2 players.
    player_id = 0
    while player_id < 2:
        conn, addr = server.accept()  # Pauses here until a client connects
        with lock:
            connected_count += 1
        logging.info(f"Connection from {addr} — assigned as Player {player_id}. Total connected: {connected_count}")

        # Every player gets their own dedicated thread ('handle_client').
        # This allows the server to talk to Player A and Player B at the exact same time.
        t = threading.Thread(target=handle_client, args=(conn, player_id), daemon=True)
        t.start()
        player_id += 1

    logging.info("Both Knights connected. The quest begins!")

    # Keep-Alive: If this main thread finishes, the whole program closes.
    # This loop keeps the server alive indefinitely while the other threads do the work.
    try:
        while True:
            time.sleep(1)  # Sleep to avoid eating up CPU power unnecessarily
    except KeyboardInterrupt:
        # Graceful shutdown: triggered when you press Ctrl+C in the console
        logging.info("Shutting down server.")
        server.close()


if __name__ == "__main__":
    main()
