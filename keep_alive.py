from flask import Flask
from threading import Thread

app = Flask(__name__)


@app.route("/")
def home():
    return {
        "status": "online",
        "service": "Telegram Subscription Bot",
        "version": "2.0"
    }


@app.route("/health")
def health():
    return {
        "status": "healthy"
    }


def run():
    app.run(
        host="0.0.0.0",
        port=10000,
        debug=False,
        use_reloader=False
    )


def keep_alive():
    Thread(target=run, daemon=True).start()
