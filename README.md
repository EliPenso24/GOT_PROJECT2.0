# GOT_PROJECT2.0
Dragon Knight: Network Game
This is a multiplayer network game where players compete to collect artifacts while avoiding an AI-controlled dragon. The game uses a client-server architecture where the server handles all logic and synchronization to ensure a fair game for everyone.

🎮 Game Features
Real-time Multiplayer: Supports two simultaneous players connected to a central server.

Server-Side AI: The dragon moves automatically using logic calculated on the server.

Collision Detection: The server strictly validates player movement to prevent walking through walls.

State Synchronization: All players see the same game world, updated in real-time.

🛠 Technical Overview
Architecture: Client-Server model using TCP sockets.

Threading: The server uses threading to manage multiple player connections and the dragon AI independently.

Concurrency: Uses threading.Lock to ensure data integrity and prevent race conditions when multiple players interact with the game state.

Data Flow: The server processes input streams, validates movement, and broadcasts the current game state as JSON.

🚀 How to Run
Requirements: Python 3.x and pygame.

Bash
pip install pygame
Launch Server:

Bash
python server.py
Launch Clients:
Open a separate terminal for each player and run:

Bash
python client.py
⚙️ How it Works
Communication: The server listens on a specific port. Clients connect via TCP, sending their movement input (dx, dy) as JSON objects.

Input Validation: The server calculates the potential new position (nx, ny). It checks the map for obstacles before confirming the player's new location.

State Sync: After processing moves and artifact collections, the server broadcasts the full state (player positions, scores, and artifact status) back to all connected clients.

📝 Key Implementation Details
Buffer Management: Since TCP is a stream-based protocol, the server uses a buffer to accumulate incoming bytes and splits them by \n to reconstruct complete JSON messages.

Atomicity: The with lock: block ensures that game state updates are atomic, preventing conflicts if multiple players perform actions at the exact same time.
