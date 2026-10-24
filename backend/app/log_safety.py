"""Redact request queries even when the development server logs sockets."""
import logging,re
_PATTERN=re.compile(r'((?:https?://[^\s?"<>]+|/[^\s?"<>]*))\?[^\s"<>]*')
class QueryFilter(logging.Filter):
 def filter(self,record):
  safe=lambda v:_PATTERN.sub(r'\1?[redacted]',v) if isinstance(v,str) else v
  record.msg=safe(record.msg)
  if isinstance(record.args,tuple):record.args=tuple(safe(v) for v in record.args)
  elif isinstance(record.args,dict):record.args={k:safe(v) for k,v in record.args.items()}
  return True
def install():
 for name in ('uvicorn.error','uvicorn.access','gunicorn.error','gunicorn.access'):
  logger=logging.getLogger(name)
  if not any(isinstance(f,QueryFilter) for f in logger.filters):logger.addFilter(QueryFilter())
