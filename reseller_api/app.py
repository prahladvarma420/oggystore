"""Gunicorn entry point: gunicorn reseller_api.app:app"""

from reseller_api import create_app

app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
