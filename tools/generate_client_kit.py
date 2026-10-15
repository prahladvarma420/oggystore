#!/usr/bin/env python3
"""Deterministically generate Postman artifacts and the code-derived error catalog.

No application imports, database access, server URL copying or network requests.
Install PyYAML (already part of the documented test dependencies). --check fails
on drift. Unknown dynamic error-code producers fail closed, requiring inspection.
"""
from __future__ import annotations
import argparse
import ast
from collections import defaultdict
import copy
import json
from pathlib import Path
import re
import sys
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]
METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace'}
CATALOG_START = '<!-- BEGIN GENERATED ERROR CATALOG -->'
CATALOG_END = '<!-- END GENERATED ERROR CATALOG -->'


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def deref(spec, value):
    seen = set()
    while isinstance(value, dict) and '$ref' in value:
        ref = value['$ref']
        if not ref.startswith('#/') or ref in seen:
            raise ValueError('Only non-cyclic local OpenAPI references are supported')
        seen.add(ref)
        value = spec
        for part in ref[2:].split('/'):
            value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def request_sample(spec, schema, variables, field='value', depth=0):
    if depth > 20:
        raise ValueError('Request schema recursion limit exceeded')
    schema = deref(spec, schema)
    for key in ('oneOf', 'anyOf'):
        if schema.get(key):
            return request_sample(spec, schema[key][0], variables, field, depth+1)
    if schema.get('allOf'):
        result = {}
        for item in schema['allOf']:
            part = request_sample(spec, item, variables, field, depth+1)
            if not isinstance(part, dict):
                raise ValueError('Cannot merge non-object allOf request schema')
            result.update(part)
        return result
    typ = schema.get('type', 'object' if 'properties' in schema else 'string')
    if isinstance(typ, list): typ = next((x for x in typ if x != 'null'), 'null')
    if typ == 'object':
        return {key: request_sample(spec, schema['properties'][key], variables, key, depth+1)
                for key in schema.get('required', [])}
    if typ == 'array':
        return [request_sample(spec, schema.get('items', {}), variables, field, depth+1)] if schema.get('minItems', 0) else []
    if 'enum' in schema: return schema['enum'][0]
    if typ in {'integer', 'number'}: return schema.get('default', max(1, schema.get('minimum', 1)))
    if typ == 'boolean': return schema.get('default', True)
    if typ == 'null': return None
    variables.add(field)
    return '{{' + field + '}}'


def sanitize_example(value, variables, field='value'):
    if isinstance(value, dict):
        return {key: sanitize_example(item, variables, key) for key, item in value.items()}
    if isinstance(value, list): return [sanitize_example(x, variables, field) for x in value]
    if isinstance(value, str):
        # User must supply URLs and identifiers/credentials locally. Never copy
        # a spec's production host, supplier credential or email into the kit.
        if re.search(r'https?://', value) or any(x in field.lower() for x in ('secret', 'token', 'key', 'email')) or field.endswith('_id'):
            variables.add(field)
            return '{{' + field + '}}'
    return value


def postman_artifacts(spec):
    variables = {'base_url', 'key_id', 'secret'}
    groups = defaultdict(list)
    for path, raw_path in sorted(spec['paths'].items()):
        path_item = deref(spec, raw_path)
        for method, operation in sorted(path_item.items()):
            if method not in METHODS: continue
            params = [deref(spec, p) for p in path_item.get('parameters', []) + operation.get('parameters', [])]
            route = re.sub(r'\{([^{}]+)\}', lambda m: '{{' + m[1] + '}}', path)
            variables.update(re.findall(r'\{([^{}]+)\}', path))
            url = {'raw': '{{base_url}}' + route, 'host': ['{{base_url}}'],
                   'path': route.lstrip('/').split('/') if route != '/' else ['']}
            query=[]
            for p in params:
                if p['in'] != 'query': continue
                variables.add(p['name'])
                query.append({'key':p['name'], 'value':'{{' + p['name'] + '}}', 'disabled':not p.get('required',False),
                              'description':'Set according to the OpenAPI parameter schema.'})
            if query:
                url['query']=query
                # Disabled query parameters intentionally omitted from raw URL.
                required=[p for p in query if not p['disabled']]
                if required: url['raw'] += '?' + '&'.join(p['key']+'='+p['value'] for p in required)
            req={'method':method.upper(),'header':[{'key':'Accept','value':'application/json'}],'url':url,
                 'description':f'Generated from docs/openapi.yaml: {method.upper()} {path}.'}
            if operation.get('security') == []: req['auth']={'type':'noauth'}
            if 'requestBody' in operation:
                body=deref(spec,operation['requestBody'])
                media=body.get('content',{}).get('application/json')
                if media is None: raise ValueError(f'Unsupported request media: {method} {path}')
                explicit=media.get('example')
                if explicit is None and media.get('examples'):
                    example=deref(spec,next(iter(media['examples'].values())))
                    if 'externalValue' in example: raise ValueError('External request examples are not fetched')
                    explicit=example.get('value')
                schema=deref(spec,media.get('schema',{}))
                if explicit is None: explicit=schema.get('example')
                if explicit is None:
                    sample=request_sample(spec,schema,variables)
                    req['description'] += ' Body derived from required schema fields (no explicit example); edit optional fields using the API guide.'
                else: sample=sanitize_example(copy.deepcopy(explicit),variables)
                req['header'].append({'key':'Content-Type','value':'application/json'})
                req['body']={'mode':'raw','raw':json_text(sample).rstrip(),'options':{'raw':{'language':'json'}}}
            groups[(operation.get('tags') or ['API'])[0]].append({'name':method.upper()+' '+path,'request':req,'response':[]})
    collection={
        'info':{'_postman_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'oggy-reseller-client-kit-v1')),
                'name':'OGGY Reseller API','schema':'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
                'description':'Generated by tools/generate_client_kit.py. Import a blank test or production environment, then fill its verified base_url (including /api/v1), key_id and secret locally. Never export filled environments.'},
        'auth':{'type':'bearer','bearer':[{'key':'token','value':'{{key_id}}.{{secret}}','type':'string'}]},
        'event':[{'listen':'prerequest','script':{'type':'text/javascript','exec':[
            "for (const key of ['base_url', 'key_id', 'secret']) {",
            "  if (!pm.variables.get(key)) throw new Error('Select and configure an environment before sending requests.');",
            "}"]}}],
        'variable':[{'key':k,'value':'','type':'string'} for k in sorted(variables)],
        'item':[{'name':key,'item':groups[key]} for key in sorted(groups)]}
    result={'examples/OGGY-Reseller-API.postman_collection.json':json_text(collection)}
    for environment in ('test','production'):
        value={'id':str(uuid.uuid5(uuid.NAMESPACE_URL,'oggy-reseller-'+environment)),
               'name':'OGGY '+environment+' (configure locally)',
               'values':[{'key':k,'value':'','enabled':True,'type':'secret' if k in {'key_id','secret'} else 'default'} for k in ('base_url','key_id','secret')],
               '_postman_variable_scope':'environment'}
        result[f'examples/OGGY-{environment}.postman_environment.json']=json_text(value)
    return result


def literal(node):
    if isinstance(node, ast.Constant): return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(x.value if isinstance(x,ast.Constant) and isinstance(x.value,str) else '{value}' for x in node.values)
    return None


def error_inventory(root):
    """All code-bearing API/service exception classes, emits and HTTP mappings.

    Dynamic transparent adapters are explicitly inspected below; a new dynamic
    producer is an error, never silently skipped. No code is imported/executed.
    """
    trees={str(p.relative_to(root)):ast.parse(p.read_text(encoding='utf-8')) for p in sorted((root/'reseller_api').rglob('*.py'))}
    classes={};defaults={};records=defaultdict(lambda:{'statuses':set(),'messages':set(),'sources':set()})
    for path,tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node,ast.ClassDef): continue
            if not any(isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name) and n.value.id=='self' and n.attr=='code' for n in ast.walk(node)):continue
            init=next((n for n in node.body if isinstance(n,ast.FunctionDef) and n.name=='__init__'),None)
            if init is None: raise ValueError('Unresolved error constructor: '+node.name)
            args=init.args.args
            named={a.arg:d for a,d in zip(args[-len(init.args.defaults):],init.args.defaults)} if init.args.defaults else {}
            if not {'code','message'} <= {a.arg for a in args}: raise ValueError('Unresolved error signature: '+node.name)
            if node.name in classes: raise ValueError('Ambiguous error class: '+node.name)
            classes[node.name]=path
            defaults[node.name]=literal(named['status']) if 'status' in named else None
            if 'status' in named and defaults[node.name] is None:
                raise ValueError('Unresolved error status default: '+node.name)
    def add(code,message,status,path,line):
        if not isinstance(code,str) or not re.fullmatch('[a-z][a-z0-9_]*',code): raise ValueError(f'Invalid error code at {path}:{line}')
        record=records[code]
        record['statuses'].add(status if status is not None else 'not defined by service; caller maps transport')
        record['messages'].add(message if isinstance(message,str) else 'Message depends on the validated request or operation.')
        record['sources'].add(f'{path}:{line}')
    for path,tree in trees.items():
        aliases={n.asname or n.name:n.name for i in ast.walk(tree) if isinstance(i,ast.ImportFrom) for n in i.names}
        for node in ast.walk(tree):
            # Inspect the HTTP fallback before skipping unrelated call names.
            if (path=='reseller_api/errors.py' and isinstance(node,ast.Call)
                    and isinstance(node.func,ast.Attribute) and node.func.attr=='get'
                    and len(node.args)>1 and isinstance(node.args[1],ast.Tuple)):
                vals=node.args[1].elts
                if len(vals)==2:add(literal(vals[0]),literal(vals[1]),'HTTPException status (fallback)',path,node.lineno)
            if isinstance(node,ast.Call):
                name=node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
                name=aliases.get(name,name)
                if name not in classes and name!='error_response': continue
                positional=dict(zip(('code','message','status') if name!='WalletError' else ('code','message'),node.args))
                values={**positional,**{k.arg:k.value for k in node.keywords if k.arg}}
                code=literal(values.get('code'));message=literal(values.get('message'))
                if code is None:
                    normalized=ast.unparse(node)
                    # Transparent code/message/status forwarding, not new values.
                    forwarded=(isinstance(values.get('code'),ast.Attribute) and values['code'].attr=='code'
                        and isinstance(values.get('message'),ast.Attribute) and values['message'].attr=='message'
                        and ast.dump(values['code'].value)==ast.dump(values['message'].value)
                        and isinstance(values['code'].value,ast.Name) and values['code'].value.id in {'exc','self'})
                    metadata_adapter=(path=='reseller_api/middleware/security.py' and normalized=='ApiError(code, message, status)')
                    http_adapter=(path=='reseller_api/errors.py' and normalized=='error_response(code, message, exc.code or 500)')
                    if not (forwarded or metadata_adapter or http_adapter):
                        raise ValueError(f'Unresolved dynamic error producer {path}:{node.lineno}: {normalized}')
                    continue
                status=literal(values['status']) if 'status' in values else defaults.get(name,400)
                if 'status' in values and status is None:
                    raise ValueError(f'Unresolved error status {path}:{node.lineno}')
                add(code,message,status,path,node.lineno)
            # Metadata validator produces literal (code, message, HTTP) tuples.
            if path=='reseller_api/services/request_security.py' and isinstance(node,ast.Return) and isinstance(node.value,ast.Tuple):
                vals=node.value.elts
                if len(vals)!=3 or any(literal(v) is None for v in vals):raise ValueError('Unresolved metadata error tuple')
                add(*(literal(v) for v in vals),path,node.lineno)
            if path=='reseller_api/errors.py' and isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(literal(key),int) and isinstance(value,ast.Tuple) and len(value.elts)==2:
                        add(literal(value.elts[0]),literal(value.elts[1]),literal(key),path,value.lineno)
            if path=='reseller_api/errors.py' and isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='get' and len(node.args)>1 and isinstance(node.args[1],ast.Tuple):
                vals=node.args[1].elts
                if len(vals)==2:add(literal(vals[0]),literal(vals[1]),'HTTPException status (fallback)',path,node.lineno)
    return [{'code':code,**{key:sorted(value,key=str) for key,value in record.items()}} for code,record in sorted(records.items())]


def guidance(record):
    code=record['code'];statuses=record['statuses']
    if code=='idempotency_conflict':return 'Inspect the existing financial reference; do not force another debit. Order client_order_id replays return the original order.'
    if code in {'internal_error','order_state_unavailable','service_unavailable','api_unavailable'}:return 'Check service status and retain request_id for support. For order retries preserve client_order_id.'
    if code=='authentication_failed':return 'Check environment and active credentials privately; do not infer why authentication failed.'
    if code=='authorization_denied' or 403 in statuses:return 'Use the authorized account/operation or contact the owner; do not probe other accounts.'
    if 429 in statuses:return 'Respect Retry-After when supplied; consult quota/rate settings before retrying.'
    if 404 in statuses:return 'Check your own public identifier and access; foreign and absent resources may be indistinguishable.'
    if 409 in statuses:return 'Read the current resource state before retrying; do not repeat financial effects blindly.'
    if code in {'insufficient_balance','insufficient_funds'}:return 'Fund the reseller wallet through an approved path, then retry the intended order safely.'
    if any(isinstance(x,int) and x>=500 for x in statuses):return 'Retain request_id; inspect status/support guidance before retrying with the same logical operation key.'
    return 'Correct the request or service precondition using the operation documentation; do not retry unchanged input.'


def catalog_markdown(records):
    text=CATALOG_START+'\n\nGenerated by `python tools/generate_client_kit.py`; verify with `--check`.\n'
    text+='Sources: code-bearing exceptions and error responses throughout `reseller_api`, request-metadata validation tuples, and HTTP exception mappings.\n'
    text+='Includes internal service errors for completeness; a service-only entry is **not** a promise of a public route or HTTP response. Status values are those explicitly defined at source sites; adapters can sanitize/remap them.\n'
    text+='Dynamic request values are shown as `{value}`. `examples/error_codes.json` records every source location and message variant.\n\n'
    text+='| Code | Defined HTTP / service status | Meaning (source message) | Action |\n| --- | --- | --- | --- |\n'
    escape=lambda s:str(s).replace('|','\\|').replace('\n',' ').replace('\r',' ')
    for row in records:
        message=' / '.join(row['messages'][:2])
        if len(row['messages'])>2:message+=' (varies by operation; see generated JSON)'
        text+=f"| `{row['code']}` | {escape(', '.join(map(str,row['statuses'])))} | {escape(message)} | {escape(guidance(row))} |\n"
    return text+'\n'+CATALOG_END


def artifacts(root):
    spec=yaml.safe_load((root/'docs/openapi.yaml').read_text())
    result=postman_artifacts(spec)
    records=error_inventory(root)
    result['examples/error_codes.json']=json_text(records)
    guide=(root/'RESELLER_API.md').read_text()
    replacement=catalog_markdown(records)
    if CATALOG_START in guide:
        start=guide.index(CATALOG_START);end=guide.index(CATALOG_END,start)+len(CATALOG_END)
        guide=guide[:start]+replacement+guide[end:]
    else:
        start=guide.index('## 17. Error codes');end=guide.index('## 18.',start)
        guide=guide[:start]+'## 17. Error codes\n\nV1 business API errors use `{"ok": false, "error": {"code": "...", "message": "..."}, "request_id": "..."}`.\nOperational liveness/readiness probes retain their separate status-only bodies and do not define application error codes.\n\n'+replacement+'\n\n'+guide[end:]
    result['RESELLER_API.md']=guide
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args(argv)
    expected=artifacts(args.root.resolve());drift=[]
    for relative,text in expected.items():
        path=args.root/relative
        if not path.exists() or path.read_text()!=text:
            drift.append(relative)
            if not args.check:
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    print(('Client kit drift: '+', '.join(drift)) if args.check and drift else f'Client kit {"checked" if args.check else "generated"}: {len(expected)} artifacts.')
    return 1 if args.check and drift else 0


if __name__=='__main__':sys.exit(main())
