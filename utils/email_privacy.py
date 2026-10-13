"""Thread-local API invite privacy; normal Telegram logging remains unchanged."""
import contextlib
import logging
import threading
_local=threading.local()
_original=logging.getLogRecordFactory()
def _factory(*args,**kwargs):
    record=_original(*args,**kwargs)
    if active():
        record.msg='API email fulfillment: diagnostic suppressed';record.args=()
        record.exc_info=None;record.exc_text=None;record.stack_info=None
    return record

def active():return bool(getattr(_local,'active',False))
def safe_message(message):return 'API email fulfillment: diagnostic suppressed' if active() else message
@contextlib.contextmanager
def protected():
    previous=active();_local.active=True
    try:yield
    finally:_local.active=previous
logging.setLogRecordFactory(_factory)

# Logger extra fields are attached AFTER LogRecordFactory. Protect structured
# handlers too, without changing records on normal/customer threads.
_original_make_record=logging.Logger.makeRecord
_standard=frozenset(logging.LogRecord('',0,'',0,'',(),None).__dict__)
def _make_record(self,*args,**kwargs):
    record=_original_make_record(self,*args,**kwargs)
    if active():
        for key in tuple(record.__dict__):
            if key not in _standard:record.__dict__[key]='[REDACTED]'
    return record
logging.Logger.makeRecord=_make_record
