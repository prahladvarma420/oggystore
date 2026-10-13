"""Supplier placement and status clients; reuse the 16.6 pinned HTTPS stack."""
import re
from fulfillment.external_clients.generic_api_client import (
    GenericApiClient, validate_config, FulfillmentConfigError, _result,
    validate_delivery, ERR_UNKNOWN, ERR_4XX,
)
_REFERENCE = re.compile(r'^[A-Za-z0-9_.:-]{1,200}$')


def validate_reference(value, cfg=None):
    if not isinstance(value,str) or not _REFERENCE.fullmatch(value):
        raise FulfillmentConfigError()
    if cfg and any(cfg.get(k) and cfg[k] in value for k in ('api_key','hmac_secret')):
        raise FulfillmentConfigError()
    return value


def validate_supplier_config(cfg, *, resolve=True):
    clean = validate_config(cfg,resolve=resolve)
    if not clean.get('api_key') or not clean.get('supplier_product_id'):
        raise FulfillmentConfigError()
    return clean


class SupplierApiClient(GenericApiClient):
    # fulfill signature/interface and transport are inherited unchanged.
    def _build_payload(self, order_id, cfg, quantity):
        validate_supplier_config(cfg,resolve=False)
        return super()._build_payload(order_id,cfg,quantity)

    def _parse_response(self, data, http_status, quantity, cfg):
        if not isinstance(data,dict):
            return self._malformed_result()
        status = data.get('status')
        if status in ('failed','cancelled','terminal_error'):
            return _result('terminal_error',ERR_4XX)
        if status not in ('pending','processing','fulfilled'):
            return self._malformed_result()
        reference = validate_reference(data.get('reference'),cfg)
        # Even immediate supplier completion is reconciled out of band. Never
        # wait for supplier completion in the API; retain only its reference.
        return _result('pending',reference=reference)


class StatusClient(GenericApiClient):
    def __init__(self, config, reference=None, *, supplier=False):
        super().__init__(config)
        self.reference = reference
        self.supplier = supplier
        self.sensitive_values = (reference,) if reference else ()

    def _idempotency_key(self, cfg, order_id):
        # Read-only polls must not replay a cached placement/pending response.
        return None

    def _build_payload(self, order_id, cfg, quantity):
        if self.supplier:
            validate_supplier_config(cfg,resolve=False)
        payload = super()._build_payload(order_id,cfg,quantity)
        payload['action'] = 'status'
        if self.reference:
            payload['reference'] = validate_reference(self.reference,cfg)
        return payload

    def _http_error_result(self, http_status, category):
        if http_status==404:
            return _result('pending','not_found')
        return super()._http_error_result(http_status,category)

    def _malformed_result(self):
        # Do not falsely complete or immediately refund on an ambiguous poll.
        return _result('retryable_error',ERR_UNKNOWN)

    def _parse_response(self, data, http_status, quantity, cfg):
        if not isinstance(data,dict):
            return self._malformed_result()
        status = data.get('status')
        if status=='not_found':
            return _result('pending','not_found')
        if status in ('pending','processing'):
            reference = data.get('reference')
            if reference is not None:
                return _result('pending',reference=validate_reference(reference,cfg))
            return _result('pending')
        if status in ('failed','cancelled','terminal_error'):
            return _result('terminal_error',ERR_4XX)
        if status in ('fulfilled','completed'):
            delivery = validate_delivery(data,quantity)
            if any(cfg.get(k) and cfg[k] in delivery for k in ('api_key','hmac_secret')):
                return self._malformed_result()
            return _result('fulfilled',delivery=delivery)
        return self._malformed_result()
