"""Process-local background jobs for bounded hotspot exports.

Local file generation runs off the request thread. Earth Engine work inside a
detector may continue remotely after its client-side timeout; therefore this
manager never reports that cancelling a running local job cancels Earth Engine.
"""
from __future__ import annotations
import csv, hashlib, json, logging, threading, uuid
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.config import EXPORTS_DIR, EXPORT_JOB_WORKERS, MAX_HOTSPOTS_LIMIT

log=logging.getLogger(__name__)
ROOT=Path(__file__).resolve().parents[2]
EXPORT_ROOT=(ROOT/EXPORTS_DIR).resolve() if not Path(EXPORTS_DIR).is_absolute() else Path(EXPORTS_DIR).resolve()
_pool=ThreadPoolExecutor(max_workers=max(1,EXPORT_JOB_WORKERS),thread_name_prefix='fire-export')
_lock=threading.RLock(); _jobs:dict[str,dict[str,Any]]={}; _futures:dict[str,Future]={}; _keys:dict[str,str]={}

def _now(): return datetime.now(timezone.utc).isoformat()
def _job_file(job_id): return EXPORT_ROOT/f'{job_id}.job.json'
def _safe_write(job):
    EXPORT_ROOT.mkdir(parents=True,exist_ok=True)
    temp=_job_file(job['id']).with_suffix('.tmp')
    temp.write_text(json.dumps(job,default=str),encoding='utf-8'); temp.replace(_job_file(job['id']))
def _load_jobs():
    try:
        files=EXPORT_ROOT.glob('EXP-*.job.json')
        for file in files:
            try:
                j=json.loads(file.read_text(encoding='utf-8'))
                if not isinstance(j,dict) or not j.get('id'):
                    raise ValueError('invalid job record')
            except (OSError,ValueError) as exc:
                log.warning('Skipping unreadable export-job record %s: %s',file.name,exc)
                continue
            if j.get('status') in ('queued','running'):
                j.update(status='failed',error='Application restarted before this local job completed; submit a new job after reviewing Earth Engine task history.',updated_at=_now())
                _safe_write(j)
            _jobs[j['id']]=j
            if j.get('dedupe_key'): _keys[j['dedupe_key']]=j['id']
    except OSError: log.exception('Could not restore saved export jobs')
_load_jobs()

def _rows(result):
    return list(result.get('hotspots_coordinates') or [])

def _write_output(job_id, request, result):
    EXPORT_ROOT.mkdir(parents=True,exist_ok=True)
    fmt=request['file_format'].upper(); rows=_rows(result)
    path=(EXPORT_ROOT/f'{job_id}.{("geojson" if fmt=="GEOJSON" else "csv")}').resolve()
    if path.parent!=EXPORT_ROOT: raise ValueError('Export path escapes the configured export directory.')
    metadata={k:result.get(k) for k in ('state','district','date_range','dataset','resolution','pixel_resolution_meters','nominal_footprint_hectares','area_disclaimer')}
    if fmt=='GEOJSON':
        features=[]
        for r in rows:
            props={k:v for k,v in r.items() if k not in ('latitude','longitude')}
            features.append({'type':'Feature','geometry':{'type':'Point','coordinates':[r['longitude'],r['latitude']]},'properties':props})
        path.write_text(json.dumps({'type':'FeatureCollection','metadata':metadata,'features':features},indent=2),encoding='utf-8')
    else:
        out=path.open('w',newline='',encoding='utf-8-sig')
        try:
            fields=sorted(set(metadata)|{k for row in rows for k in row})
            writer=csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');writer.writeheader()
            for row in rows: writer.writerow({**metadata,**row})
        finally: out.close()
    return path

def _run(job_id,request):
    from app.api.fires import DETECTORS
    with _lock:
        job=_jobs[job_id]
        if job['status']=='cancelled': return
        job.update(status='running',started_at=_now(),updated_at=_now());_safe_write(job)
    try:
        detector,_=DETECTORS[request['product']]
        result=detector(request['state'],request['district'],request['start_date'],request['end_date'],limit=min(MAX_HOTSPOTS_LIMIT,request['limit']))
        if not isinstance(result,dict) or str(result.get('status','')).lower().startswith('error'):
            raise RuntimeError(str(result.get('status','Invalid detector response')) if isinstance(result,dict) else 'Invalid detector response')
        path=_write_output(job_id,request,result)
        with _lock:
            job.update(status='completed',output_file=path.name,download_url=f"/api/exports/{job_id}/download",record_count=len(_rows(result)),completed_at=_now(),updated_at=_now(),error=None)
            _safe_write(job)
    except Exception as exc:
        log.exception('Export job %s failed',job_id)
        with _lock:
            job.update(status='failed',error=str(exc),updated_at=_now(),completed_at=_now())
            _safe_write(job)

def create_job(payload:dict):
    request=dict(payload); request['file_format']=request.get('file_format','CSV').upper()
    if request.get('export_destination')!='local':
        raise ValueError('This build supports local exports only. Drive and Cloud Storage destinations need Earth Engine export-task credentials and a configured bucket/folder; no remote export was started.')
    if request['file_format'] not in ('CSV','GEOJSON'): raise ValueError('file_format must be CSV or GeoJSON.')
    if request.get('scale') is not None: raise ValueError('Do not set a single export scale: fused inputs are sampled at their distinct native resolutions (375 m and 1 km).')
    canonical=json.dumps(request,sort_keys=True); key=hashlib.sha256(canonical.encode()).hexdigest()
    with _lock:
        existing=_keys.get(key)
        if existing and _jobs.get(existing,{}).get('status') in ('queued','running','completed'):
            return dict(_jobs[existing],deduplicated=True)
        job_id='EXP-'+uuid.uuid4().hex[:12].upper()
        job={'id':job_id,'status':'queued','created_at':_now(),'updated_at':_now(),'request':request,'dedupe_key':key,'progress':None,'progress_note':'Progress percentage is not available from this detector.'}
        _jobs[job_id]=job;_keys[key]=job_id;_safe_write(job)
        _futures[job_id]=_pool.submit(_run,job_id,request)
        return dict(job)

def list_jobs():
    with _lock: return sorted((dict(j) for j in _jobs.values()),key=lambda x:x.get('created_at',''),reverse=True)
def get_job(job_id):
    with _lock: return dict(_jobs[job_id]) if job_id in _jobs else None
def cancel_job(job_id):
    with _lock:
        job=_jobs.get(job_id)
        if not job:return None
        if job['status']=='queued':
            future=_futures.get(job_id)
            if future and future.cancel(): job.update(status='cancelled',updated_at=_now(),completed_at=_now());_safe_write(job);return dict(job)
            # Worker may have started between state inspection and cancel.
            job=_jobs[job_id]
        if job['status']=='running':
            raise RuntimeError('Job is already running. A local worker cannot be force-stopped safely; any Earth Engine request already submitted may continue remotely.')
        return dict(job)

def retry_job(job_id):
    """Create a separately tracked retry for failed jobs, preserving idempotency.

    Timed-out Earth Engine requests are not retried automatically because the
    server cannot prove that an upstream computation stopped after the timeout.
    """
    job=get_job(job_id)
    if not job: return None
    if job.get('status')!='failed': raise RuntimeError('Only failed export jobs can be retried.')
    error=str(job.get('error') or '').lower()
    if 'timed out' in error or 'timeout' in error:
        raise RuntimeError('This job timed out; Earth Engine work may still be running. Review Earth Engine task history before creating another export.')
    request=dict(job['request']); request['retry_of']=job_id
    return create_job(request)
def output_path(job_id):
    job=get_job(job_id)
    if not job or job.get('status')!='completed' or not job.get('output_file'): return None
    p=(EXPORT_ROOT/job['output_file']).resolve()
    return p if p.parent==EXPORT_ROOT and p.is_file() else None
