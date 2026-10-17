web: gunicorn --workers 1 --threads 8 --timeout 120 -b 0.0.0.0:$PORT web_admin.app:app
worker: python bot.py
webhook-worker: python -m reseller_api.workers.webhook_worker
fulfillment-worker: python -m reseller_api.workers.fulfillment_worker
reseller-deposit-worker: python -m reseller_api.workers.reseller_deposit_worker
