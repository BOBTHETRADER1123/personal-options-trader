import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, CloudUpload, HardDrive, Loader2, RefreshCw, ShieldCheck } from 'lucide-react';
import { api, time } from './api';
import { Badge, Empty, Help, Panel } from './components';
import './CloudServices.css';

type Artifact = {kind:string; filename:string; size:number; drive_id?:string; verification?:string};
type BackupJob = {id:string; trade_id:string; status:string; attempts:number; verified_at?:string|null; last_error?:string|null; artifacts:Artifact[]};
type DriveStatus = {enabled:boolean; configured:boolean; connected:boolean; authorization_required:boolean; last_error?:string|null;
  counts:Record<string,number>; jobs:BackupJob[]; retention:string; last_success?:string|null};
type ResourceSample = {sampled_at:string; uptime_seconds:number;
  cpu:{percent:number|null; capacity_vcpu:number; source:string};
  ram:{used_bytes:number|null; limit_bytes:number|null; percent:number|null; source:string};
  disk:{used_bytes:number|null; total_bytes:number|null; free_bytes:number|null; percent:number|null; source:string};
  neon_usage:{available:boolean; source:string; message:string}};
type Resources = {sample:ResourceSample|null; active_alerts:{metric:string; severity:string; message:string; sample_unavailable?:boolean}[];
  notifications:{metric:string; severity:string; timestamp:string; message:string}[];
  thresholds:Record<string,{warning:number; critical:number}>};
const bytes = (value:number|null|undefined) => value == null ? 'Unavailable' : value >= 1024**3 ? `${(value/1024**3).toFixed(2)} GB` : `${(value/1024**2).toFixed(1)} MB`;

export function CloudServicesPanel({notify}:{notify:(message:string,error?:boolean)=>void}) {
  const [drive,setDrive] = useState<DriveStatus|null>(null), [resources,setResources] = useState<Resources|null>(null);
  const [error,setError] = useState(''), [busy,setBusy] = useState(''), [refreshing,setRefreshing] = useState(false);
  const alive = useRef(true), pending = useRef(false), notice = useRef(notify);
  notice.current = notify;
  const refresh = useCallback(async (manual=false) => {
    if (pending.current || document.hidden) return;
    pending.current = true;
    if (alive.current) setRefreshing(true);
    try {
      const results = await Promise.allSettled([api<DriveStatus>('/api/cloud/drive/status'), api<Resources>('/api/system/resources')]);
      if (!alive.current) return;
      const failures:string[] = [];
      if (results[0].status === 'fulfilled') setDrive(results[0].value);
      else failures.push(`Backups: ${results[0].reason instanceof Error ? results[0].reason.message : 'status unavailable'}`);
      if (results[1].status === 'fulfilled') setResources(results[1].value);
      else failures.push(`Server: ${results[1].reason instanceof Error ? results[1].reason.message : 'status unavailable'}`);
      setError(failures.join(' '));
      if (manual) notice.current(failures.length ? failures.join(' ') : 'Cloud status refreshed.', Boolean(failures.length));
    } finally {
      pending.current = false;
      if (alive.current) setRefreshing(false);
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 30000);
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', visible);
    return () => { alive.current = false; window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [refresh]);

  async function connect() {
    setBusy('connect');
    try {
      const result = await api<{authorization_url:string}>('/api/cloud/drive/connect', {method:'POST'});
      const target = new URL(result.authorization_url);
      if (target.protocol !== 'https:' || target.hostname !== 'accounts.google.com' || target.username || target.password || target.port) {
        throw new Error('The Google connection address could not be verified.');
      }
      window.location.assign(target.href);
    } catch (problem) { notice.current((problem as Error).message, true); setBusy(''); }
  }
  async function action(path:string, name:string) {
    setBusy(name);
    try { await api(path, {method:'POST'}); notice.current('Backup request queued. Completed trade records remain protected.'); await refresh(); }
    catch (problem) { notice.current((problem as Error).message, true); }
    finally { setBusy(''); }
  }
  const sample = resources?.sample;
  const pendingCount = drive ? Object.entries(drive.counts).reduce((total,[status,count]) => total + (status === 'verified' ? 0 : count),0) : null;
  return <div className="cloud-services">
    <div className="cloud-services-toolbar"><div><strong>Cloud backups & server health</strong><p>Recording and backups run on the backend, independently of this page.</p></div>
      <button className="btn secondary" disabled={refreshing} onClick={()=>void refresh(true)}><RefreshCw size={15} className={refreshing?'spin':''}/>Refresh</button></div>
    {error && <div className="inline-error" role="alert">{error} Last successful samples remain visible below.</div>}
    <Panel title="Google Drive archives" subtitle="Server-to-Drive copies · choose your separate 15 GB Google account">
      {!drive ? <div className="cloud-wait">{refreshing?<Loader2 size={20} className="spin"/>:<CloudUpload size={20}/>}Backup status is unavailable.</div> : <>
        <div className="cloud-connection"><div><Badge tone={drive.connected&&drive.enabled?'green':drive.authorization_required?'red':'amber'}>
          {drive.authorization_required?'Reconnect Google Drive':drive.connected&&drive.enabled?'Cloud backups enabled':drive.connected?'Connected · backups paused':drive.configured?'Not connected':'Server setup required'}</Badge>
          <p>{drive.configured ? 'Choose your separate 15 GB Google account. Offline authorization is stored securely on the server; your laptop may be off.' : 'The server needs its Google OAuth configuration before you can connect your Drive.'}</p></div>
          <button className="btn secondary" disabled={Boolean(busy)||!drive.configured} onClick={()=>void connect()}>
            {busy==='connect'?<Loader2 size={15} className="spin"/>:<ShieldCheck size={15}/>} {drive.connected?'Renew Drive permission':'Connect Google Drive'}</button></div>
        {!drive.enabled && <Help>Automatic cloud backups are disabled in this server’s configuration. Existing files and trading records are retained.</Help>}
        {drive.last_error && <div className="inline-error" role="alert">{drive.last_error}</div>}
        <div className="cloud-summary"><div><span>Verified backups</span><strong>{drive.counts.verified||0}</strong></div><div><span>Queued / retrying</span><strong>{pendingCount}</strong></div><div><span>Latest verified upload</span><strong>{time(drive.last_success||drive.jobs.find(j=>j.verified_at)?.verified_at,true)}</strong></div></div>
        <div className="cloud-actions"><button className="btn secondary" disabled={Boolean(busy)||!drive.enabled||!drive.connected} onClick={()=>void action('/api/cloud/drive/queue','queue')}>
          {busy==='queue'?<Loader2 size={15} className="spin"/>:<CloudUpload size={15}/>}Back up completed reports</button><small>Folder: Paper Trading Records → year → month → broker</small></div>
        {drive.jobs.length ? <div className="table-scroll"><table><thead><tr><th>Trade</th><th>Cloud backup</th><th>Files</th><th>Verified · IST</th><th>Action</th></tr></thead><tbody>{drive.jobs.map(job=><tr key={job.id}>
          <td>{job.trade_id.slice(0,18)}{job.last_error&&<small className="cell-note negative">{job.last_error}</small>}</td>
          <td><Badge tone={job.status==='verified'?'green':job.status==='retry'?'red':'amber'}>{job.status==='verified'?'Verified':job.status==='retry'?'Waiting to retry':job.status==='uploading'?'Uploading':'Queued'}</Badge></td>
          <td>{job.artifacts.length ? job.artifacts.map(a=><small className="cell-note" key={a.kind}>{a.kind==='xlsx'?'Excel summary':a.kind==='csv_gz'?'Compressed CSV history':a.kind} · {bytes(a.size)}</small>) : 'Preparing'}</td>
          <td>{time(job.verified_at,true)}</td><td>{job.status!=='verified'&&<button className="btn secondary small-btn" disabled={Boolean(busy)||job.status==='uploading'||!drive.enabled||!drive.connected}
            onClick={()=>void action(`/api/cloud/drive/retry/${encodeURIComponent(job.id)}`,job.id)}><RefreshCw size={14} className={busy===job.id?'spin':''}/>Retry</button>}</td>
        </tr>)}</tbody></table></div> : <Empty title="No cloud backups yet" description="Completed reports will enter the cloud queue after Drive is connected and server backups are enabled."/>}
        <Help>{drive.retention} Excel stays compact; a separate compressed CSV contains available recorded history. Upload verification checks file size, type, folder and checksums.</Help>
      </>}
    </Panel>
    <Panel title="Server resources" subtitle={sample?`Latest sample ${time(sample.sampled_at,true)} IST · uptime ${(sample.uptime_seconds/3600).toFixed(1)} hours`:'Waiting for the first resource sample'}>
      {resources?.active_alerts.map(alert=><div className="inline-error cloud-resource-alert" role="alert" key={alert.metric}><AlertTriangle size={16}/>{alert.message}{alert.sample_unavailable?' Latest sample unavailable.':''}</div>)}
      <div className="cloud-resource-grid">{(['cpu','ram','disk'] as const).map(metric=> {
        const value = sample?.[metric], percent = value?.percent;
        return <div className="cloud-resource-card" key={metric}><span>{metric==='cpu'?'CPU':metric==='ram'?'Memory':'Persistent disk'}</span><strong>{percent == null ? 'Unavailable' : `${percent.toFixed(1)}%`}</strong>
          <progress max="100" value={percent??undefined} aria-label={`${metric} usage`} />
          <small>{metric==='cpu' ? `${sample?.cpu.capacity_vcpu??'—'} vCPU capacity` : metric==='ram' ? `${bytes(sample?.ram.used_bytes)} / ${bytes(sample?.ram.limit_bytes)}` : `${bytes(sample?.disk.free_bytes)} free`}</small>
          <small>Warning {resources?.thresholds[metric]?.warning??'—'}% · Critical {resources?.thresholds[metric]?.critical??'—'}%</small></div>;
      })}</div>
      <Help><HardDrive size={14}/>{sample?.neon_usage?.message||'Server measurements describe this hosting container and its persistent disk.'} Unavailable values are not zero. Alerts appear here and in server logs; external email or SMS delivery is not configured.</Help>
      {resources?.notifications.length ? <details className="cloud-notifications"><summary>Recent resource notifications ({resources.notifications.length})</summary>{resources.notifications.slice(-10).reverse().map((item,index)=><p key={`${item.timestamp}-${item.metric}-${index}`}><Badge tone={item.severity==='recovered'?'green':'amber'}>{item.severity}</Badge> {item.message}<small>{time(item.timestamp,true)} IST</small></p>)}</details> : null}
    </Panel>
  </div>;
}
