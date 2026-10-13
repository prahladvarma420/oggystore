"""Bounded, signed HTTPS supplier transport. Never exposes upstream errors."""
import hashlib
import hmac
import ipaddress
import json
import logging
import random
import socket
import threading
import time
from urllib.parse import urlsplit
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.connection import HTTPSConnection as _HTTPSConnection
from fulfillment.external_clients.base import ExternalFulfillmentClient
from database.reseller_db import validate_webhook_url
from reseller_api.services.webhook_delivery import (
    _classify, ERR_TIMEOUT, ERR_CONNECTION, ERR_TLS, ERR_DNS,
    ERR_4XX, ERR_5XX, ERR_UNKNOWN,
)

MAX_RESPONSE_BYTES = 65536
MAX_DELIVERY_CHARS = 10000
_ALLOWED = {'endpoint', 'api_key', 'hmac_secret', 'connect_timeout', 'read_timeout',
            'timeout', 'retries', 'dispatch_budget', 'supplier_product_id', 'idempotency_prefix'}


_LOG_CONTEXT = threading.local()


class _TransportLogFilter(logging.Filter):
    def filter(self, record):
        redactions = getattr(_LOG_CONTEXT, 'redactions', ())
        if redactions:
            try:
                text = record.getMessage()
            except Exception:
                text = "[REDACTED fulfillment transport log]"
            for value in redactions:
                text = text.replace(value, '[REDACTED]')
            record.msg, record.args = text, ()
            # Raw exceptions can contain URL/header material. Categorized
            # client errors never need transport tracebacks in logs.
            record.exc_info = record.exc_text = record.stack_info = None
        return True


for _logger_name in ('urllib3.connectionpool','urllib3.connection','urllib3.util.retry',
                     'requests.packages.urllib3.connectionpool'):
    logging.getLogger(_logger_name).addFilter(_TransportLogFilter())


class FulfillmentConfigError(ValueError):
    def __init__(self):
        super().__init__('Invalid fulfillment configuration.')


def resolve_public_ips(endpoint):
    """Validate ALL A/AAAA answers; caller pins one validated address to TLS."""
    parsed = urlsplit(validate_webhook_url(endpoint))
    host, port = parsed.hostname, parsed.port or 443
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = tuple(dict.fromkeys(answer[4][0] for answer in answers))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise FulfillmentConfigError()
    return ips


def validate_config(config, *, resolve=True):
    """Save-time and send-time policy; errors never embed config/credentials."""
    try:
        if not isinstance(config, dict) or set(config)-_ALLOWED:
            raise ValueError()
        endpoint = validate_webhook_url(config.get('endpoint', ''))
        # Credentials belong in headers, never query strings/debug URL logs.
        if urlsplit(endpoint).query:
            raise ValueError()
        clean = dict(config, endpoint=endpoint)
        for key in ('api_key', 'hmac_secret', 'supplier_product_id', 'idempotency_prefix'):
            value = clean.get(key, '')
            if not isinstance(value, str) or len(value)>4096 or any(ord(c)<32 for c in value):
                raise ValueError()
        for key, default in (('connect_timeout',2), ('read_timeout',2)):
            value = clean.get(key, clean.get('timeout',default))
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 1<=value<=60:
                raise ValueError()
            clean[key] = float(value)
        if any(ord(c)>126 for c in clean.get('api_key','')):
            raise ValueError()
        prefix = clean.get('idempotency_prefix','')
        if len(prefix)>128 or any(not (c.isascii() and (c.isalnum() or c in '_.:-')) for c in prefix):
            raise ValueError()
        retries = clean.get('retries',2)
        budget = clean.get('dispatch_budget',5)
        if isinstance(retries,bool) or not isinstance(retries,int) or not 0<=retries<=3:
            raise ValueError()
        if isinstance(budget,bool) or not isinstance(budget,(int,float)) or not 1<=budget<=10:
            raise ValueError()
        clean.update(retries=retries,dispatch_budget=float(budget))
        if resolve:
            resolve_public_ips(endpoint)
        return clean
    except Exception:
        raise FulfillmentConfigError() from None


class QuietHTTPSConnection(_HTTPSConnection):
    # Even global http.client debugging must not print Authorization/body.
    debuglevel = 0


class PinnedHTTPSAdapter(HTTPAdapter):
    """Connect to a checked IP while keeping HTTPS Host/SNI/cert validation.

    Overrides both requests adapter entry points (2.31 and 2.32+). Proxies
    and redirects are disabled by the session; TLS verification stays on.
    """
    def __init__(self, host, ip, port):
        self.host, self.ip, self.port = host, ip, port
        super().__init__(max_retries=0)

    def _pool(self):
        pool = self.poolmanager.connection_from_host(self.ip, self.port, scheme='https',
            pool_kwargs={'assert_hostname':self.host, 'server_hostname':self.host})
        pool.ConnectionCls = QuietHTTPSConnection
        return pool

    def get_connection(self, url, proxies=None):
        return self._pool()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self._pool()


def new_session():
    session = requests.Session()
    session.trust_env = False   # no ambient proxy or netrc credential forwarding
    return session


def _result(status, category=None, **extra):
    value = {'status':status}
    if category:
        value['category'] = category
    value.update(extra)
    return value


def validate_delivery(data, quantity):
    if not isinstance(data,dict):
        raise ValueError()
    items = data.get('items')
    if items is None and quantity==1:
        items = [data.get('delivery')]
    if not isinstance(items,list) or len(items)!=quantity:
        raise ValueError()
    if any(not isinstance(item,str) or not item.strip() for item in items):
        raise ValueError()
    delivery = '\n\n'.join(items)
    if not 1<=len(delivery)<=MAX_DELIVERY_CHARS:
        raise ValueError()
    return delivery


class GenericApiClient(ExternalFulfillmentClient):
    def __init__(self, config: dict):
        self.config = config  # internal only; never serialize/log this object
        self.stop = threading.Event()
        self.deadline = None

    def fulfill(self, order_id: int, product_config: dict, quantity: int) -> dict:
        # Thread-local redaction avoids changing unrelated request logging.
        values = []
        if isinstance(product_config,dict):
            for key in ('endpoint','api_key','hmac_secret'):
                value = product_config.get(key)
                if isinstance(value,str) and value:
                    values.append(value)
            endpoint = product_config.get('endpoint')
            if isinstance(endpoint,str):
                try:
                    path = urlsplit(endpoint).path
                    if path:
                        values.append(path)
                except Exception:
                    pass
        _LOG_CONTEXT.redactions = tuple(values) + tuple(getattr(self, "sensitive_values", ()))
        try:
            return self._fulfill(order_id,product_config,quantity)
        finally:
            _LOG_CONTEXT.redactions = ()

    def _idempotency_key(self, cfg, order_id):
        return f"{cfg.get('idempotency_prefix') or 'reseller-order'}:{int(order_id)}"

    def _build_payload(self, order_id, cfg, quantity):
        payload = {'order_id':f'ORD-{int(order_id):08d}', 'quantity':quantity}
        if cfg.get('supplier_product_id'):
            payload['product_id'] = cfg['supplier_product_id']
        return payload

    def _http_error_result(self, http_status, category):
        transient = category==ERR_5XX or http_status in (408,429)
        return _result('retryable_error' if transient else 'terminal_error',category or ERR_UNKNOWN)

    def _malformed_result(self):
        return _result('terminal_error',ERR_UNKNOWN)

    def _parse_response(self, data, http_status, quantity, cfg):
        status = data.get('status') if isinstance(data,dict) else None
        if http_status==202 and status is None and isinstance(data,dict):
            status = 'pending'
        if status=='pending':
            return _result('pending')
        if status in ('fulfilled',None) and http_status!=202:
            delivery = validate_delivery(data,quantity)
            if any(cfg.get(k) and cfg[k] in delivery for k in ('api_key','hmac_secret')):
                return _result('terminal_error',ERR_UNKNOWN)
            return _result('fulfilled',delivery=delivery)
        if status=='terminal_error':
            return _result('terminal_error',ERR_4XX)
        if status=='retryable_error':
            return _result('retryable_error',ERR_5XX)
        return self._malformed_result()

    def _fulfill(self, order_id, product_config, quantity):
        try:
            cfg = validate_config(product_config, resolve=False)
            if isinstance(quantity,bool) or not isinstance(quantity,int) or not 1<=quantity<=10000:
                return _result('terminal_error',ERR_UNKNOWN)
            deadline = self.deadline or time.monotonic()+cfg['dispatch_budget']
            payload = self._build_payload(order_id,cfg,quantity)
            body = json.dumps(payload,separators=(',',':'),ensure_ascii=False).encode('utf-8')
            key = self._idempotency_key(cfg,order_id)
            endpoint = cfg['endpoint'];parsed = urlsplit(endpoint)
            for attempt in range(cfg['retries']+1):
                left = deadline-time.monotonic()
                if self.stop.is_set() or left<2:
                    return _result('pending','dispatch_budget')
                try:
                    # Re-resolve immediately before EACH connection, then pin.
                    ips = resolve_public_ips(endpoint)
                except FulfillmentConfigError:
                    return _result('terminal_error','ssrf_blocked')
                except Exception:
                    result = _result('retryable_error',ERR_DNS)
                else:
                    left = deadline-time.monotonic()
                    if self.stop.is_set() or left<2:
                        return _result('pending','dispatch_budget')
                    headers = {'Content-Type':'application/json',
                        'Host':parsed.netloc,'User-Agent':'OggyStore-Fulfillment/1.0'}
                    if key:
                        headers['Idempotency-Key'] = key
                    if cfg.get('api_key'):
                        headers['Authorization'] = 'Bearer '+cfg['api_key']
                    if cfg.get('hmac_secret'):
                        stamp = str(int(time.time()))
                        signature = hmac.new(cfg['hmac_secret'].encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
                        headers.update({'X-Fulfillment-Timestamp':stamp,'X-Fulfillment-Signature':'sha256='+signature})
                    try:
                        with new_session() as session:
                            session.mount('https://',PinnedHTTPSAdapter(parsed.hostname,ips[0],parsed.port or 443))
                            with session.post(endpoint,data=body,headers=headers,
                                timeout=(min(cfg['connect_timeout'],left/2),min(cfg['read_timeout'],left/2)),
                                allow_redirects=False,stream=True) as response:
                                ok,category = _classify(response.status_code)
                                if not ok:
                                    result = self._http_error_result(response.status_code,category)
                                else:
                                    raw = bytearray()
                                    for chunk in response.iter_content(4096):
                                        if self.stop.is_set() or time.monotonic()>=deadline:
                                            return _result('pending','dispatch_budget')
                                        raw.extend(chunk)
                                        if len(raw)>MAX_RESPONSE_BYTES:
                                            return _result('terminal_error',ERR_UNKNOWN)
                                    try:
                                        data = json.loads(raw)
                                        result = self._parse_response(data,response.status_code,quantity,cfg)
                                        if result['status']!='retryable_error':
                                            return result
                                    except Exception:
                                        return self._malformed_result()
                    except requests.exceptions.SSLError:
                        return _result('terminal_error',ERR_TLS)
                    except requests.exceptions.Timeout:
                        result = _result('retryable_error',ERR_TIMEOUT)
                    except requests.exceptions.ConnectionError:
                        result = _result('retryable_error',ERR_CONNECTION)
                    except Exception:
                        return _result('terminal_error',ERR_UNKNOWN)
                if result['status']!='retryable_error':
                    return result
                if attempt==cfg['retries']:
                    return {**result,'exhausted':True}
                delay = random.uniform(.02,min(.5,.1*2**attempt))
                if deadline-time.monotonic()<delay+2:
                    return _result('pending','dispatch_budget')
                if self.stop.wait(delay):
                    return _result('pending','dispatch_budget')
            return _result('terminal_error',ERR_UNKNOWN)
        except Exception:
            return _result('terminal_error',ERR_UNKNOWN)


def load_config(raw):
    """Server-side only; accepts legacy JSON and encrypted new admin saves."""
    try:
        if isinstance(raw,str) and raw.startswith('enc:'):
            from utils.crypto import _fernet
            cipher = _fernet()
            if cipher is None:
                raise ValueError()
            raw = cipher.decrypt(raw[4:].encode('ascii')).decode('utf-8')
        cfg = json.loads(raw) if raw else {}
        if not isinstance(cfg,dict):
            raise ValueError()
        return cfg
    except Exception:
        raise FulfillmentConfigError() from None
