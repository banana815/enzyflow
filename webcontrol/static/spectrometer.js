/* UVC requests are independent of motor ownership, polling and stop controls. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="enzyflow-token"]').content;
  let savedLampId = null, ratioRecord = null, archiveSignature = '';
  let lampLive = null, lampFrames = 0, lamp = null, lampSelected = 436, darkReady = false, zeroActive = false;
  const lampWaves = [404,436,546,610];
  let processingDirty = false;
  let darkCollecting = false, darkPrevious = 'none', manualExposure = false, isRecording = false;
  let devices = [], lastSeq = null, pending = false, connected = false, fresh = false;
  let roiReady = false, roiScanning = false;
  let exposureDirty = false, lastFiles = '', localError = '', lifecycle = 'disconnected';
  const paintError = msg => { $('sp-error').textContent = msg || ''; $('sp-error').hidden = !msg; };
  const error = msg => { localError = msg || ''; paintError(localError); };
  async function request(path, body) {
    const response = await fetch(path, {cache:'no-store', signal:AbortSignal.timeout(7000), ...(body ? {
      method:'POST', headers:{'Content-Type':'application/json','X-Enzyflow-Token':token},body:JSON.stringify(body)} : {})});
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || '光谱仪请求失败');
    return data;
  }
  function buttons() {
    document.querySelectorAll('.sp-live').forEach(b => { b.disabled = !connected || !fresh || pending || (darkCollecting && b.id !== 'sp-dark-clear' && b.dataset.spOp !== 'record_stop') || (b.dataset.spOp === 'save' && (!roiReady || roiScanning)); });
    $('sp-dark-start').disabled ||= !$('sp-dark-confirm').checked || !manualExposure || isRecording || !roiReady || roiScanning;
    $('sp-lamp-capture').disabled ||= !darkReady || zeroActive || roiScanning || lampFrames < 32 || !$('sp-lamp-confirm').checked;
    $('sp-lamp-calibrate').disabled ||= !lamp || !darkReady || isRecording || roiScanning;
    $('sp-halogen-save').disabled ||= !darkReady || zeroActive || roiScanning || lampFrames<32 || !$('sp-halogen-confirm').checked;
    $('sp-record').disabled ||= isRecording || !roiReady || roiScanning;
    $('sp-auto-roi').disabled ||= isRecording || roiScanning;
    $('sp-connect').disabled = connected || pending || ['connecting','stopping'].includes(lifecycle);
  }
  async function command(op,args={}) {
    if (pending) return;
    pending = true; buttons();
    try { await request('/api/spectrometer',{op,...args}); error(''); }
    catch(e) { error(e.message); }
    finally { pending=false; buttons(); }
  }
  async function scan() {
    try {
      const data=await request('/api/spectrometer-devices'); devices=data.devices;
      $('sp-device').replaceChildren();
      devices.forEach((d,i) => {
        const option=document.createElement('option'); option.value=String(i);
        option.textContent=`${d.name} · ${d.path} · ${d.vid_pid}${d.eligible?'':'（已排除）'}`;
        option.disabled=!d.eligible; $('sp-device').append(option);
      });
      const first=devices.findIndex(d=>d.eligible); $('sp-device').value=first<0?'':String(first);
      if(first<0) error('未发现匹配的光谱仪采集接口；不会改用内置摄像头或串口。'); else error('');
    } catch(e) { error(e.message); }
  }
  $('sp-halogen-confirm').onchange=buttons;
  $('sp-halogen-save').onclick=()=>command('halogen_capture',{confirmed:$('sp-halogen-confirm').checked});
  $('sp-ratio-mode').onchange=drawRatio;
  function drawRatio() {
    const mode=$('sp-ratio-mode').value;
    chart(ratioRecord?.photometry?.active?{pixel:ratioRecord.pixel,wavelength_nm:ratioRecord.wavelength_nm,display:ratioRecord.photometry[mode]}:null,'sp-ratio-chart');
  }
  $('sp-profile-restore').onclick=()=>{exposureDirty=processingDirty=false;command('profile_restore');};
  for(const k of ['average','smooth','hold']) $('sp-'+k).onchange=()=>{processingDirty=true;};
  $('sp-dark-confirm').onchange=buttons;
  $('sp-dark-start').onclick=()=>command('dark_start',{frames:Number($('sp-dark-frames').value),confirmed:$('sp-dark-confirm').checked});
  $('sp-dark-clear').onclick=()=>command('dark_clear');
  $('sp-scan').onclick=scan;
  $('sp-connect').onclick=()=>{
    const d=devices[Number($('sp-device').value)];
    if($('sp-device').value==='' || !d?.eligible) return error('请先扫描并选择光谱仪');
    lastSeq=null; exposureDirty=false;
    command('connect',{device:{vid_pid:d.vid_pid,by_id:d.by_id,name:d.name}});
  };
  $('sp-disconnect').onclick=()=>command('disconnect');
  $('sp-exposure').onchange=()=>{exposureDirty=true;};
  $('sp-auto-roi').onclick=()=>command('auto_roi');
  $('sp-expose').onclick=()=>command('exposure',{auto:false,value:Number($('sp-exposure').value)});
  $('sp-process').onclick=()=>command('processing',{average:Number($('sp-average').value),smooth:$('sp-smooth').checked,hold:$('sp-hold').checked});
  $('sp-record').onclick=()=>command('record_start',{interval:Number($('sp-interval').value)});
  $('sp-calibrate').onclick=()=>command('calibrate',{points:$('sp-points').value.split(/[,，\n]+/).filter(s=>s.trim()).map(s=>s.split(/[:：]/).map(Number))});
  document.querySelectorAll('[data-sp-op]').forEach(b=>b.onclick=()=>command(b.dataset.spOp));
  function chart(record, canvasId='sp-chart') {
    const canvas=$(canvasId), c=canvas.getContext('2d'), w=canvas.width, h=canvas.height;
    c.clearRect(0,0,w,h); c.font='18px sans-serif'; c.fillStyle='#bacbd9';
    if(!record) { c.fillText('等待光谱数据',45,60);return; }
    const xs=record.wavelength_nm || record.pixel, ys=record.display;
    const valid=ys.filter(v=>v!==null && Number.isFinite(v));
    const min=Math.min(0,...valid), max=Math.max(1,...valid), xlo=Math.min(...xs), xhi=Math.max(...xs);
    const left=65, top=25, gw=w-95, gh=h-90;
    for(let i=0;i<=4;i++) {
      const y=top+gh*i/4; c.strokeStyle='#2b3a47'; c.beginPath();c.moveTo(left,y);c.lineTo(w-30,y);c.stroke();
      c.fillStyle='#bacbd9';c.fillText((max-(max-min)*i/4).toFixed(canvasId==='sp-ratio-chart'?2:0),8,y+6);
      c.fillText((xlo+(xhi-xlo)*i/4).toFixed(record.wavelength_nm?1:0),left+gw*i/4-20,h-36);
    }
    c.strokeStyle='#55d6bd';c.lineWidth=2;c.beginPath();
    let started=false;
    ys.forEach((v,i)=>{if(v===null || !Number.isFinite(v)){started=false;return;} const x=left+gw*(xs[i]-xlo)/(xhi-xlo||1),y=top+gh*(max-v)/(max-min);started?c.lineTo(x,y):c.moveTo(x,y);started=true;});c.stroke();
    c.fillStyle='#bacbd9';c.fillText(record.wavelength_nm?'波长 / nm':'绝对像素 / px（未标定）',left,h-8);
  }
  function drawLamp() {
    const view=lamp || savedLampId;
    chart(view?{pixel:view.pixels,display:view.intensity,wavelength_nm:null}:lampLive,'sp-lamp-chart');
    if(!lamp) return;
    const c=$('sp-lamp-chart').getContext('2d'), first=lamp.pixels[0], last=lamp.pixels.at(-1);
    for(const nm of lampWaves) {
      const text=$('sp-lamp-pixel-'+nm).value;
      if(text==='') continue;
      const x=65+805*(Number(text)-first)/(last-first||1);
      if(x<65 || x>870) continue;
      c.strokeStyle=nm===lampSelected?'#ffd46a':'#7995aa';c.beginPath();c.moveTo(x,25);c.lineTo(x,290);c.stroke();
      c.fillStyle='#ffd46a';c.fillText(nm+' nm',Math.min(x+3,805),46+lampWaves.indexOf(nm)*22);
    }
  }
  function updateLamp(s) {
    const next=s.lamp;
    savedLampId=s.saved_lamp || null;
    lampFrames=s.record?.stats.buffer_len || 0;
    lampLive=s.record?{pixel:s.record.pixel,display:s.record.intensity,wavelength_nm:null}:null;
    if((next?.id||null)!==(lamp?.id||null)) {
      lamp=next;
      for(const nm of lampWaves) $('sp-lamp-pixel-'+nm).value='';
      $('sp-lamp-confirm').checked=false;
      drawLamp();
    }
    if(!lamp) {
      if(s.calibration_report) {
        for(const p of [...s.calibration_report.points,...(s.calibration_report.checks||[])]) {
          if(lampWaves.includes(p.reference_nm)) $('sp-lamp-pixel-'+p.reference_nm).value=p.pixel;
        }
        $('sp-lamp-degree').value=s.calibration_report.degree || 2;
      }
      drawLamp();
    }
    $('sp-lamp-status').textContent=lamp?`已冻结第 ${lamp.seq} 帧 · ${lamp.created} · 当前选择 ${lampSelected} nm；实时采集仍继续`:
      (lampLive?`实时预览（尚未冻结） · 已平均 ${lampFrames} 帧 · `:'等待实时光谱 · ')+(darkReady?(lampFrames<32?'请点击“准备32帧平均”，等待平均完成后确认灯光来源并冻结':'暗场已就绪；确认灯光来源后冻结，再选峰'):'请先完成暗场校正，或恢复已保存的校准设置');
    if(!lamp && savedLampId) $('sp-lamp-status').textContent='已恢复保存的荧光灯谱和标定。重新选峰前请重新冻结当前灯谱；日常测量无需重复校准。';
    const r=s.calibration_report;
    if(r) {
      const points=r.points.map(p=>`${p.reference_nm} nm ← 像素 ${p.pixel}；拟合误差 ${p.error_nm.toFixed(3)} nm${Math.abs(p.error_nm)>3?'（偏差较大，请检查峰对应）':''}`).join('\n');
      const checks=r.checks.length?r.checks.map(p=>`独立验证 ${p.reference_nm} nm：预测 ${p.fitted_nm.toFixed(2)} nm，差值 ${p.error_nm.toFixed(2)} nm${Math.abs(p.error_nm)>3?'（差异较大，请重新检查选峰）':''}`).join('\n'):'未提供独立验证峰。';
      $('sp-lamp-report').textContent=`已保存${r.degree===1?'线性':'二次'}标定 · 拟合 RMS ${r.rms_nm.toFixed(3)} nm\n${points}\n${checks}\n已覆盖像素 ${r.pixel_range.join('–')}，范围外为外推。\n${r.warning}`;
    } else $('sp-lamp-report').textContent='尚未进行皓宇灯标定';
  }
  $('sp-lamp-prepare').onclick=()=>{ processingDirty=false;command('processing',{average:32,smooth:$('sp-smooth').checked,hold:false}); };
  $('sp-lamp-confirm').onchange=buttons;
  $('sp-lamp-capture').onclick=()=>command('lamp_capture',{confirmed:$('sp-lamp-confirm').checked});
  document.querySelectorAll('[data-lamp-select]').forEach(b=>{
    b.setAttribute('aria-pressed',String(Number(b.dataset.lampSelect)===lampSelected));
    b.onclick=()=>{
      lampSelected=Number(b.dataset.lampSelect);
      document.querySelectorAll('[data-lamp-select]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));
      $('sp-lamp-status').textContent=`当前选择 ${lampSelected} nm，请点击冻结曲线的对应峰顶`;
      drawLamp();
    };
  });
  for(const nm of lampWaves) $('sp-lamp-pixel-'+nm).oninput=drawLamp;
  $('sp-lamp-chart').onclick=e=>{
    if(!lamp) return error('请先冻结皓宇灯谱');
    const rect=e.currentTarget.getBoundingClientRect(), x=(e.clientX-rect.left)*900/rect.width;
    if(x<65 || x>870) return;
    const pixel=lamp.pixels[0]+(x-65)/805*(lamp.pixels.at(-1)-lamp.pixels[0]);
    const nearest=lamp.peaks.reduce((a,b)=>Math.abs(a.pixel-pixel)<Math.abs(b.pixel-pixel)?a:b);
    if(Math.abs(nearest.pixel-pixel)>12) return error('此处没有附近候选峰，请点击峰顶或手工填写像素');
    $('sp-lamp-pixel-'+lampSelected).value=nearest.pixel;error('');drawLamp();
  };
  $('sp-lamp-calibrate').onclick=()=>{
    if(!lamp) return error('请先冻结皓宇灯谱');
    const points=[],check_points=[];
    for(const nm of lampWaves) {
      const value=$('sp-lamp-pixel-'+nm).value, used=$('sp-lamp-use-'+nm).checked;
      if(value==='') { if(used) return error(`请先填写 ${nm} nm 对应的实测像素`); continue; }
      (used?points:check_points).push([Number(value),nm]);
    }
    command('lamp_calibrate',{snapshot_id:lamp.id,points,check_points,degree:Number($('sp-lamp-degree').value)});
  };
  async function poll() {
    try {
      const s=await request('/api/spectrometer-state'); connected=s.connected; fresh=s.age_ms<3000; lifecycle=s.status;
      $('sp-status').textContent=({connecting:'连接中',connected:fresh?'采集中':'等待新帧',stopping:'正在断开',error:'采集异常',disconnected:'未连接'})[s.status]||s.status;
      $('sp-message').textContent=s.message+(s.demo?' · 合成演示数据':''); paintError(s.error || localError);
      if(s.device) $('sp-identity').textContent=`${s.device.name} · ${s.device.vid_pid} · ${s.device.path} · ${s.device.width} × ${s.device.height}`;
      $('sp-record-state').textContent=s.recording?'正在记录 JSONL':'未记录';
      $('sp-profile-state').textContent=s.profile?.message || '校准完成后自动保存，重连无需重复校准。';
      if(s.processing && !processingDirty) { $('sp-average').value=s.processing.buffer_size; $('sp-smooth').checked=s.processing.savgol_enabled; $('sp-hold').checked=s.processing.hold_peaks; }
      const r=s.record;
      ratioRecord=r;drawRatio();
      $('sp-halogen-state').textContent=(s.halogen?.saved?`空白 I₀已保存 · ${s.halogen.created} · `:'')+(s.halogen?.message || '尚未保存卤素灯参考');
      $('sp-file-directory').textContent='记录目录：'+(s.capture_directory || 'spectrometer/runtime/web/captures');
      const archive=JSON.stringify(s.archive || []);
      if(archive!==archiveSignature) {
        archiveSignature=archive;$('sp-archive').replaceChildren();
        for(const f of s.archive || []) {const a=document.createElement('a');a.href=f.url;a.download='';a.textContent=f.name;$('sp-archive').append(a);}
      }
      roiReady=!!s.roi_selection?.ready;roiScanning=!!s.roi_selection?.scanning;
      $('sp-roi-state').textContent=s.roi_selection?.message || '等待光谱范围';
      $('sp-roi-bounds').textContent=r?`当前范围：横向 ${r.roi.x}–${r.roi.x+r.roi.w-1} px，纵向 ${r.roi.y}–${r.roi.y+r.roi.h-1} px` : '';
      manualExposure=s.demo || (r?.exposure.auto === false && r?.exposure.value != null);
      isRecording=!!s.recording;
      const dark=s.dark || {status:'none',message:'当前服务尚未加载暗场功能'};
      darkCollecting=dark.status==='collecting';
      darkReady=dark.status==='ready'; zeroActive=!!r?.stats.zero_reference;
      updateLamp(s);
      if(dark.status!==darkPrevious) $('sp-dark-confirm').checked=false;
      darkPrevious=dark.status;
      $('sp-dark-progress').hidden=!darkCollecting;
      $('sp-dark-progress').max=dark.total || 32; $('sp-dark-progress').value=dark.count || 0;
      $('sp-dark-state').textContent=dark.message+(darkCollecting?` · ${dark.count}/${dark.total} 帧`:'')+
        (dark.metadata?` · 平均底噪 ${dark.metadata.mean_counts.toFixed(3)} · 帧间标准差均值 ${dark.metadata.temporal_std_mean.toFixed(3)} · ${dark.metadata.frames} 帧`:'')+
        (!manualExposure && connected?' · 手动曝光尚未就绪，请应用曝光设置':'');
      if(r && r.seq!==lastSeq) {
        lastSeq=r.seq;chart(r); $('sp-preview').src='/api/spectrometer-preview?t='+Date.now();
        $('sp-preview').hidden=false; $('sp-empty').hidden=true;
        if(!exposureDirty) { if(r.exposure.value!==null) $('sp-exposure').value=r.exposure.value; }
        const st=r.stats;
        $('sp-stats').textContent=`${r.t_wall} · 平均 ${st.buffer_len} 帧 · 峰值 ${st.intensity_max.toFixed(1)} · 峰位置 ${st.peak_wavelength_nm===null ? st.peak_pixel+' px':st.peak_wavelength_nm.toFixed(2)+' nm'}${st.low_light?' · 低光':''}${st.saturated?' · 饱和':''}${st.zero_reference?' · 零参考已启用':''}${st.dark_corrected?' · 暗场已扣除':''}`;
        $('sp-cal-state').textContent=r.calibration?`${r.calibration.points.length} 点标定 · R² ${r.calibration.r_squared?.toFixed(6)} · ${r.calibration.calibration_id}`:'未标定 · 横轴为像素';
      }
      if(!r) {lastSeq=null;chart(null);$('sp-preview').hidden=true;$('sp-empty').hidden=false;}
      if(!connected && r) $('sp-stats').textContent='采集已停止，显示最后一帧 · '+r.t_wall;
      const signature=JSON.stringify(s.files);
      if(signature!==lastFiles) {
        lastFiles=signature; $('sp-files').replaceChildren();
        for(const [kind,url] of Object.entries(s.files||{})) { const a=document.createElement('a');a.href=url;a.download='';a.textContent={csv:'CSV',json:'JSON',graph_png:'光谱图',frame_png:'原始帧',jsonl:'连续记录 JSONL'}[kind]||kind;$('sp-files').append(a); }
      }
    } catch(e) {connected=false;fresh=false;paintError('光谱仪服务不可用：'+e.message);$('sp-status').textContent='服务离线';}
    finally {buttons();setTimeout(poll,500);}
  }
  chart(null);drawLamp();buttons();poll();
})();
