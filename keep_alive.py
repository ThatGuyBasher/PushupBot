import threading
from flask import Flask, jsonify
import os

# Create the Flask application instance
app = Flask(__name__)

# --- Health Check Route ---
@app.route('/')
def home():
    """
    A simple health check endpoint. Render will access this route periodically.
    """
    # Check if the MongoDB connection is alive (a simple check based on the global state)
    # The MONGO_CLIENT is defined in bot.py, but we can't easily access it here.
    # We will just return a simple status message.
    
    # You can check your bot's status on the Render dashboard/logs.
    return jsonify({
        "status": "Online",
        "service": "PushupBot Keep-Alive",
        "note": "This is a health check endpoint."
    })

def run_server():
    """Starts the Flask web server."""
    # Render automatically provides a PORT environment variable.
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    """
    Starts the web server in a separate thread to avoid blocking the Discord bot.
    """
    server = threading.Thread(target=run_server)
    server.daemon = True # Makes the thread exit when the main program exits
    server.start()
    print("Keep-alive server started in background thread.")

# --- If you run this file directly, it will start the web server ---
if __name__ == "__main__":
    run_server()