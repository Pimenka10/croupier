import os
import subprocess
import threading
from flask import Flask

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health_check():
    return "Bot is running", 200

def run_bot():
    subprocess.run(["python", "ot.py"])

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)