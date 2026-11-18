/* UVC requests are independent of motor ownership, polling and stop controls. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="enzyflow-token"]').content;
  let absorbanceSignature = '';
  let saturationDirty = false, saturationPolicy = {block:false,threshold:250};
  let savedLampId = null, ratioRecord = null, archiveSignature = '';
  let historySignature = '', activeHistory = null, availableHistory = [];
  let darkSavable = false, camera = null, exposureDefault = 300;
  let referenceJob = null, preparingExposure = false, halogenCollecting = false;
  const left = 75, right = 26, top = 36, bottom = 62, chartWidth = 900;
  let lampLive = null, lampFrames = 0, lamp = null, lampSelected = 436, darkReady = false, zeroActive = false;
  const lampWaves = [404,436,546,610];
  let processingDirty = false;
  let darkCollecting = false, darkPrevious = 'none', manualExposure = false, isRecording = false;
  let devices = [], lastSeq = null, pending = false, connected = false, fresh = false;
  let roiReady = false, roiScanning = false, currentCalibration = null;
  let exposureDirty = false, exposureChoicesKey = '', roiDirty = false, lastFiles = '', localError = '', lifecycle = 'disconnected';
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
    document.querySelectorAll('.sp-live').forEach(b => { b.disabled = !connected || !fresh || pending || preparingExposure || halogenCollecting || (darkCollecting && b.id !== 'sp-dark-clear' && b.dataset.spOp !== 'record_stop') || (b.dataset.spOp === 'save' && (!roiReady || roiScanning)); });
    const canSaveAbsorbance = connected && fresh && !pending && !darkCollecting && !halogenCollecting && roiReady && !roiScanning
      && ratioRecord?.photometry?.active && ratioRecord.photometry.absorbance?.some(v=>typeof v==='number' && Number.isFinite(v));
    $('sp-absorbance-save').disabled = !canSaveAbsorbance;
    $('sp-absorbance-status').textContent = !connected || !fresh ? '请连接光谱仪并等待新帧。'
      : !roiReady || roiScanning ? '请先确定采样范围。'
      : !ratioRecord?.photometry?.active ? (ratioRecord?.photometry?.message || '请先采集暗场并保存空白 I₀。')
      : !canSaveAbsorbance ? '等待有效吸光度数据或当前操作完成。' : '可保存当前平均后的单幅吸光度曲线。';
    $('sp-dark-start').disabled ||= !$('sp-dark-confirm').checked || !manualExposure || !roiReady || roiScanning || isRecording;
    $('sp-dark-save').disabled ||= !darkSavable;
    $('sp-history-load').disabled ||= !$('sp-history').value || isRecording;
    $('sp-roi-load').disabled ||= !$('sp-roi-history').value || isRecording;
    $('sp-peak-load').disabled ||= !$('sp-peak-history').value || isRecording;
    $('sp-roi-save').disabled ||= !activeHistory || !roiReady || roiScanning || isRecording;
    $('sp-lamp-capture').disabled ||= zeroActive || !roiReady || roiScanning || !!referenceJob || !$('sp-lamp-confirm').checked;
    $('sp-lamp-calibrate').disabled ||= (!lamp && !currentCalibration) || !roiReady || isRecording || roiScanning;
    $('sp-halogen-save').disabled ||= isRecording || !manualExposure || !darkReady || zeroActive || !roiReady || roiScanning || !!referenceJob || !$('sp-halogen-confirm').checked;
    for (const id of ['sp-exposure-auto','sp-exposure-default','sp-expose']) {
      $(id).disabled ||= isRecording || preparingExposure || camera?.kind==='synthetic';
    }
    const cannotEdit = !connected || !fresh || isRecording || darkCollecting || halogenCollecting || preparingExposure;
    for (const id of ['sp-exposure','sp-exposure-step']) $(id).disabled = cannotEdit || camera?.kind==='synthetic';
    for (const input of roiInputs) input.disabled = cannotEdit;
    $('sp-roi-apply').disabled = cannotEdit || pending;
    $('sp-exposure-auto').disabled ||= !roiReady || roiScanning;
    $('sp-record').disabled ||= isRecording || !roiReady || roiScanning;
    $('sp-auto-roi').disabled ||= isRecording || roiScanning;
    $('sp-connect').disabled = connected || pending || ['connecting','stopping'].includes(lifecycle);
    const showModes = !!devices[Number($('sp-device').value)]?.capture_modes?.length;
    $('sp-capture-mode-field').hidden = $('sp-capture-mode-note').hidden = !showModes;
    $('sp-capture-mode').disabled = $('sp-connect').disabled;
  }
  async function command(op,args={}) {
    if (pending) return;
    pending = true; buttons();
    try { await request('/api/spectrometer',{op,...args}); error(''); return true; }
    catch(e) { error(e.message); return false; }
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
      buttons();
      if(first<0) error('未发现匹配的光谱仪采集接口；不会改用内置摄像头或串口。'); else error('');
    } catch(e) { error(e.message); }
  }
  $('sp-saturation-threshold').oninput=()=>{saturationDirty=true;};
  $('sp-saturation-block').onchange=()=>{saturationDirty=true;};
  $('sp-saturation-apply').onclick=async()=>{
    if (!$('sp-saturation-threshold').reportValidity()) return;
    const settings={threshold:Number($('sp-saturation-threshold').value),block:$('sp-saturation-block').checked};
    if(await command('saturation_settings',settings)) saturationDirty=false;
  };
  $('sp-halogen-confirm').onchange=buttons;
  $('sp-halogen-save').onclick=()=>command('halogen_capture',{confirmed:$('sp-halogen-confirm').checked});
  $('sp-ratio-mode').onchange=drawRatio;
  $('sp-absorbance-save').onclick=()=>{
    if (!$('sp-absorbance-name').reportValidity()) return;
    $('sp-ratio-mode').value='absorbance';drawRatio();
    return command('absorbance_save',{name:$('sp-absorbance-name').value.trim()});
  };
  function drawRatio() {
    const mode=$('sp-ratio-mode').value;
    chart(ratioRecord?.photometry?.active?{pixel:ratioRecord.pixel,wavelength_nm:ratioRecord.wavelength_nm,display:ratioRecord.photometry[mode]}:null,'sp-ratio-chart');
  }
  $('sp-history-load').onclick=()=>{
    exposureDirty=processingDirty=roiDirty=false;
    command('history_load',{id:$('sp-history').value});
  };
  $('sp-roi-load').onclick=()=>{
    roiDirty=false;
    return command('history_roi',{id:$('sp-roi-history').value});
  };
  $('sp-roi-save').onclick=()=>command('roi_save');
  $('sp-roi-reset').onclick=()=>{
    roiDirty=false;
    return command('roi_reset');
  };
  $('sp-peak-load').onclick=()=>command('history_peaks',{id:$('sp-peak-history').value});
  $('sp-peak-reset').onclick=()=>{
    for(const nm of lampWaves) $('sp-lamp-pixel-'+nm).value='';
    drawLamp();
    command('peaks_reset');
  };
  $('sp-dark-save').onclick=()=>command('dark_save');
  $('sp-exposure-auto').onclick=async()=>{
    exposureDirty=false;
    preparingExposure=true;
    if (!await command('exposure_auto')) preparingExposure=false;
  };
  $('sp-exposure-default').onclick=()=>{
    exposureDirty=false;
    command('exposure',{auto:false,value:exposureDefault,draft:true});
  };
  for (const id of ['sp-history','sp-roi-history','sp-peak-history']) $(id).onchange=buttons;

  function updateHistory(s) {
    const previous=activeHistory;
    activeHistory=s.active_history || null;
    availableHistory=s.history || [];
    const signature=JSON.stringify([activeHistory,availableHistory]);
    if(signature===historySignature) return;
    historySignature=signature;
    for (const [id, stage] of [['sp-history',null],['sp-roi-history','roi'],['sp-peak-history','peaks']]) {
      const select=$(id), selected=select.value;
      select.replaceChildren();
      const entries=availableHistory.filter(entry=>!stage || entry[stage]);
      for(const entry of entries) {
        const detail=[entry.dark?(entry.dark_compatible===false?'暗场条件不匹配':'暗场'):'',entry.roi?'范围':'',entry.peaks?'峰位':'',entry.reference?'I₀':''].filter(Boolean);
        select.add(new Option(entry.name + (stage || !detail.length ? '' : ' · '+detail.join(' / ')),entry.id));
      }
      if(!entries.length) select.add(new Option('暂无记录',''));
      const preferred=activeHistory!==previous && entries.some(e=>e.id===activeHistory) ? activeHistory : selected;
      if(entries.some(e=>e.id===preferred)) select.value=preferred;
    }
  }

  async function prepareReference(op, confirmId) {
    if (lampFrames >= 32) return command(op,{confirmed:$(confirmId).checked});
    referenceJob={op, confirmId, started:Date.now()};
    processingDirty=false;
    if (!await command('processing',{average:32,smooth:$('sp-smooth').checked,hold:false})) referenceJob=null;
    buttons();
  }
  for(const k of ['average','smooth','hold']) $('sp-'+k).onchange=()=>{processingDirty=true;};
  $('sp-dark-confirm').onchange=buttons;
  $('sp-dark-start').onclick=()=>command('dark_start',{frames:Number($('sp-dark-frames').value),confirmed:$('sp-dark-confirm').checked,full_frame:true});
  $('sp-dark-clear').onclick=()=>command('dark_reset');
  $('sp-scan').onclick=scan;
  $('sp-device').onchange=buttons;
  $('sp-connect').onclick=()=>{
    const d=devices[Number($('sp-device').value)];
    if($('sp-device').value==='' || !d?.eligible) return error('请先扫描并选择光谱仪');
    lastSeq=null; exposureDirty=roiDirty=false;
    const options={guided:true,device:{vid_pid:d.vid_pid,by_id:d.by_id,name:d.name}};
    if(d.capture_modes?.length) options.capture_mode=$('sp-capture-mode').value;
    command('connect',options);
  };
  $('sp-disconnect').onclick=()=>command('disconnect');
  $('sp-exposure').oninput=$('sp-exposure-step').onchange=()=>{exposureDirty=true;};
  $('sp-exposure').onkeydown=event=>{if(event.key==='Enter') $('sp-expose').click();};
  $('sp-auto-roi').onclick=()=>{
    roiDirty=false;
    return command('auto_roi',{draft:true});
  };
  $('sp-expose').onclick=async()=>{
    const field=$(camera?.exposure_options?.length?'sp-exposure-step':'sp-exposure');
    if (!field.reportValidity()) return;
    if(await command('exposure',{auto:false,value:Number(field.value),draft:true})) exposureDirty=false;
  };

  const preview=$('sp-preview'), box=$('sp-roi-drag');
  const roiInputs=['x0','x1','y0','y1'].map(axis=>$('sp-roi-'+axis));
  let drag=null;
  function enteredRange() {
    if(!camera || roiInputs.some(input=>input.value==='')) return null;
    const [x0,x1,y0,y1]=roiInputs.map(input=>Number(input.value));
    if(![x0,x1,y0,y1].every(Number.isInteger) || x0<0 || y0<0 || x0>x1 || y0>y1 ||
        x1>=camera.width || y1>=camera.height) return null;
    return {x:x0,y:y0,w:x1-x0+1,h:y1-y0+1};
  }
  function showRangeInput(region) {
    const values=[region.x,region.x+region.w-1,region.y,region.y+region.h-1];
    roiInputs.forEach((input,i)=>{
      input.max=(i<2?camera.width:camera.height)-1;
      if(!roiDirty) input.value=values[i];
    });
    if(!drag) drawRangeDraft();
  }
  function drawRangeDraft() {
    const region=roiDirty && enteredRange();
    if(region && !preview.hidden) {
      drawRangeBox({x:region.x/camera.width,y:region.y/camera.height,
                    w:region.w/camera.width,h:region.h/camera.height});
    } else box.hidden=true;
  }
  for(const input of roiInputs) input.oninput=()=>{roiDirty=true;drawRangeDraft();};
  $('sp-roi-form').onsubmit=async event=>{
    event.preventDefault();
    if($('sp-roi-apply').disabled) return;
    const region=enteredRange();
    if(!region) {error('请填写有效的像素坐标，起点不能大于终点，终点不能超出画面。');return;}
    if(await command('roi',{...region,draft:true})) roiDirty=false;
  };
  function cursor(event) {
    const rect=preview.getBoundingClientRect();
    return {x:Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),
            y:Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))};
  }
  function selection(a,b) {
    return {x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),w:Math.abs(a.x-b.x),h:Math.abs(a.y-b.y)};
  }
  preview.onpointerdown=event=>{
    if (!camera || $('sp-roi-apply').disabled || !event.isPrimary || event.button!==0) return;
    drag=cursor(event);
    preview.setPointerCapture(event.pointerId);
  };
  function drawRangeBox(b) {
    const r=preview.getBoundingClientRect();
    const parent=preview.parentElement.getBoundingClientRect();
    Object.assign(box.style,{
      left:(r.left-parent.left+b.x*r.width)+'px',top:(r.top-parent.top+b.y*r.height)+'px',
      width:b.w*r.width+'px',height:b.h*r.height+'px',
    });
    box.hidden=false;
  }
  preview.onpointermove=event=>{
    if(drag) drawRangeBox(selection(drag,cursor(event)));
  };
  preview.onpointerup=event=>{
    if(!drag) return;
    const b=selection(drag,cursor(event));
    drag=null;box.hidden=true;
    const x=Math.floor(b.x*camera.width), y=Math.floor(b.y*camera.height);
    const w=Math.ceil((b.x+b.w)*camera.width)-x, h=Math.ceil((b.y+b.h)*camera.height)-y;
    if(w>=2 && h>=2) {roiDirty=false;command('roi',{x,y,w,h,draft:true});}
    else drawRangeDraft();
  };
  preview.onpointercancel=()=>{drag=null;drawRangeDraft();};
  $('sp-process').onclick=()=>command('processing',{average:Number($('sp-average').value),smooth:$('sp-smooth').checked,hold:$('sp-hold').checked});
  $('sp-record').onclick=()=>command('record_start',{interval:Number($('sp-interval').value)});
  $('sp-calibrate').onclick=()=>command('calibrate',{points:$('sp-points').value.split(/[,，\n]+/).filter(s=>s.trim()).map(s=>s.split(/[:：]/).map(Number))});
  document.querySelectorAll('[data-sp-op]').forEach(b=>b.onclick=()=>command(b.dataset.spOp));
  function chart(record, canvasId='sp-chart') {
    const canvas=$(canvasId), c=canvas.getContext('2d'), w=canvas.width, h=canvas.height;
    c.clearRect(0,0,w,h); c.font='16px system-ui, sans-serif'; c.fillStyle='#c6d7e3';
    const ratio=canvasId==='sp-ratio-chart', absorbance=$('sp-ratio-mode').value==='absorbance';
    c.fillText(ratio ? (absorbance?'吸光度 · 无量纲':'透过率 I/I₀ · 无量纲') : '强度 / counts',left,22);
    if(!record) { c.fillText('等待光谱数据',left,82);return; }
    const xs=record.wavelength_nm || record.pixel, ys=record.display;
    const valid=ys.filter(Number.isFinite);
    const min=Math.min(0,...valid), max=Math.max(1,...valid), xlo=Math.min(...xs), xhi=Math.max(...xs);
    const gw=w-left-right, gh=h-top-bottom;
    const yAt=v=>top+gh*(max-v)/(max-min);
    for(let i=0;i<=4;i++) {
      const f=i/4,y=top+gh*f,x=left+gw*f;
      c.strokeStyle='#2b3a47';c.lineWidth=1;c.beginPath();
      c.moveTo(left,y);c.lineTo(w-right,y);c.moveTo(x,top);c.lineTo(x,h-bottom);c.stroke();
      c.fillStyle='#bacbd9';c.textAlign='right';
      c.fillText((max-(max-min)*f).toFixed(ratio?2:1),left-9,y+5);
      c.textAlign='center';c.fillText((xlo+(xhi-xlo)*f).toFixed(record.wavelength_nm?1:0),x,h-bottom+20);
    }
    if(min<0) {
      c.strokeStyle='#647a89';c.beginPath();c.moveTo(left,yAt(0));c.lineTo(w-right,yAt(0));c.stroke();
    }
    c.strokeStyle='#55d6bd';
    if(record.wavelength_nm && !ratio && xhi>xlo) {
      const colors=[[380,'#9382ff'],[440,'#7baaff'],[480,'#6ce8fb'],[530,'#7de287'],[575,'#f5e580'],[610,'#ffb273'],[660,'#ff797f'],[780,'#bf8291']];
      const colorAt=nm=>colors[(colors.findIndex(([n])=>n>=nm)+colors.length)%colors.length][1];
      const gradient=c.createLinearGradient(left,0,w-right,0);
      gradient.addColorStop(0,xlo>780?colors.at(-1)[1]:colorAt(xlo));
      for(const [nm,color] of colors) if(nm>xlo && nm<xhi) gradient.addColorStop((nm-xlo)/(xhi-xlo),color);
      gradient.addColorStop(1,xhi>780?colors.at(-1)[1]:colorAt(xhi));
      c.strokeStyle=gradient;
    }
    c.lineWidth=2;c.beginPath();
    let started=false;
    ys.forEach((v,i)=>{
      if(!Number.isFinite(v)){started=false;return;}
      const x=left+gw*(xs[i]-xlo)/(xhi-xlo||1),y=yAt(v);
      started?c.lineTo(x,y):c.moveTo(x,y);started=true;
    });
    c.stroke();c.fillStyle='#bacbd9';c.textAlign='center';
    c.fillText(record.wavelength_nm?'波长 / nm':'绝对像素 / px',left+gw/2,h-12);
    c.textAlign='left';
  }
  function drawLamp() {
    const view=lamp || savedLampId;
    chart(view?{pixel:view.pixels,display:view.intensity,wavelength_nm:null}:lampLive,'sp-lamp-chart');
    if(!lamp) return;
    const c=$('sp-lamp-chart').getContext('2d'), first=lamp.pixels[0], last=lamp.pixels.at(-1);
    for(const nm of lampWaves) {
      const text=$('sp-lamp-pixel-'+nm).value;
      if(text==='') continue;
      const x=left+(chartWidth-left-right)*(Number(text)-first)/(last-first||1);
      if(x<left || x>chartWidth-right) continue;
      c.strokeStyle=nm===lampSelected?'#ffd46a':'#7995aa';c.beginPath();c.moveTo(x,top);c.lineTo(x,380-bottom);c.stroke();
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
    const lampHint = !roiReady || roiScanning ? '请先确定采样范围' :
      zeroActive ? '请先清除样品零参考' : '确认光源后点击采集，系统会自动等待 32 帧平均';
    $('sp-lamp-status').textContent=referenceJob?.op==='lamp_capture'?('正在平均 '+lampFrames+' / 32 帧'):lamp?`已冻结第 ${lamp.seq} 帧 · 当前选择 ${lampSelected} nm · ${lamp.dark_id?'已扣暗场':'原始灯谱'}`:
      (lampLive?`实时预览 · 已平均 ${lampFrames} 帧 · `:'等待实时光谱 · ')+lampHint;
    if(!lamp && savedLampId && !referenceJob) $('sp-lamp-status').textContent='已载入保存的灯谱和峰位。重新选峰时请采集新灯谱。';
    const r=s.calibration_report;
    if(r) {
      const points=r.points.map(p=>`${p.reference_nm} nm ← 像素 ${p.pixel}；拟合误差 ${p.error_nm.toFixed(3)} nm${Math.abs(p.error_nm)>3?'（偏差较大，请检查峰对应）':''}`).join('\n');
      const checks=r.checks.length?r.checks.map(p=>`独立验证 ${p.reference_nm} nm：预测 ${p.fitted_nm.toFixed(2)} nm，差值 ${p.error_nm.toFixed(2)} nm${Math.abs(p.error_nm)>3?'（差异较大，请重新检查选峰）':''}`).join('\n'):'未提供独立验证峰。';
      $('sp-lamp-report').textContent=`已保存${r.degree===1?'线性':'二次'}标定 · ${r.direction==='decreasing'?'反向波长轴 · ':''}拟合 RMS ${r.rms_nm.toFixed(3)} nm\n${points}\n${checks}\n已覆盖像素 ${r.pixel_range.join('–')}，范围外为外推。\n${r.warning}`;
    } else $('sp-lamp-report').textContent='尚未进行皓宇灯标定';
  }
  $('sp-lamp-confirm').onchange=buttons;
  $('sp-lamp-capture').onclick=()=>prepareReference('lamp_capture','sp-lamp-confirm');
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
    if(x<left || x>chartWidth-right) return;
    const pixel=lamp.pixels[0]+(x-left)/(chartWidth-left-right)*(lamp.pixels.at(-1)-lamp.pixels[0]);
    const nearest=lamp.peaks.reduce((a,b)=>Math.abs(a.pixel-pixel)<Math.abs(b.pixel-pixel)?a:b);
    $('sp-lamp-pixel-'+lampSelected).value=Math.abs(nearest.pixel-pixel)<=12 && !e.shiftKey ? nearest.pixel : Math.round(pixel);error('');drawLamp();
  };
  $('sp-lamp-calibrate').onclick=()=>{
    if(!lamp) return command('peaks_save');
    const points=[],check_points=[];
    for(const nm of lampWaves) {
      const value=$('sp-lamp-pixel-'+nm).value, used=$('sp-lamp-use-'+nm).checked;
      if(value==='') { if(used) return error(`请先填写 ${nm} nm 对应的实测像素`); continue; }
      (used?points:check_points).push([Number(value),nm]);
    }
    command('peaks_save',{snapshot_id:lamp.id,points,check_points,degree:Number($('sp-lamp-degree').value)});
  };
  async function poll() {
    try {
      const s=await request('/api/spectrometer-state'); connected=s.connected; fresh=s.age_ms<3000; lifecycle=s.status;
      $('sp-status').textContent=({connecting:'连接中',connected:fresh?'采集中':'等待新帧',stopping:'正在断开',error:'采集异常',disconnected:'未连接'})[s.status]||s.status;
      $('sp-message').textContent=s.message+(s.demo?' · 合成演示数据':''); paintError(s.error || localError);
      updateHistory(s);
        saturationPolicy=s.saturation || saturationPolicy;
        if(!saturationDirty) {
          $('sp-saturation-threshold').value=saturationPolicy.threshold;
          $('sp-saturation-block').checked=saturationPolicy.block;
        }
        $('sp-saturation-state').textContent='当前阈值 '+saturationPolicy.threshold+' counts · '+(saturationPolicy.block?'已开启拦截':'仅提示，不拦截');
      camera=s.device;
      currentCalibration=s.wavelength_calibration?.id || null;
      if(camera) {
        if(connected && camera.capture_mode) $('sp-capture-mode').value=camera.capture_mode;
        const format=camera.source_fourcc ? ' · '+camera.source_fourcc : '';
        $('sp-identity').textContent=`${camera.name} · ${camera.vid_pid} · ${camera.width} × ${camera.height}${format}${camera.sample_bits?' · '+camera.sample_bits+' 位视频数据':''}`;
      }
      preparingExposure=connected && s.message==='正在自动调整曝光，请保持光源稳定';
      $('sp-record-state').textContent=s.recording?'正在记录 JSONL':'未记录';
      $('sp-profile-state').textContent=s.history_message || (connected?'可载入记录，也可以从曝光开始手动校正。':'连接后扫描适配当前设备的记录。');
      if(s.processing && !processingDirty) { $('sp-average').value=s.processing.buffer_size; $('sp-smooth').checked=s.processing.savgol_enabled; $('sp-hold').checked=s.processing.hold_peaks; }
      const r=s.record;
      if(r?.exposure){
        const exp=r.exposure;
        const field=$('sp-exposure');
        const choices=camera?.exposure_options || [];
        const key=JSON.stringify(choices);
        $('sp-exposure-options').hidden=!choices.length;
        $('sp-exposure-number').hidden=!!choices.length;
        const select=$('sp-exposure-step');
        if(key!==exposureChoicesKey && choices.length) {
          select.replaceChildren(...choices.map(option=>new Option(`${Number(option.milliseconds.toPrecision(6))} ms`,option.value)));
        }
        exposureChoicesKey=key;
        if(!exposureDirty && exp.value!=null) {
          field.value=exp.value;
          select.value=exp.value;
        }
        if(exp.min!=null) field.min=exp.min;
        if(exp.max!=null) field.max=exp.max;
        exposureDefault=exp.default ?? 300;
        if(exp.milliseconds!=null) $('sp-exposure-readout').textContent=
          `当前 ${Number(exp.milliseconds.toPrecision(5))} ms · 手动锁定${exp.native!=null?' · DirectShow '+exp.native:''}`;
      }
      if(referenceJob && !pending) {
        const job=referenceJob;
        if(!connected || s.error || !$(job.confirmId).checked || Date.now()-job.started>20000) referenceJob=null;
        else if((r?.stats.buffer_len || 0)>=32) {
          referenceJob=null;
          command(job.op,{confirmed:true});
        }
      }
      ratioRecord=r;drawRatio();
      const capture=s.absorbance_capture;
      if(capture && JSON.stringify(capture)!==absorbanceSignature) {
        absorbanceSignature=JSON.stringify(capture);
        $('sp-absorbance-result').textContent='已保存：'+capture.name+' · '+capture.captured_at+' · '+capture.valid_pixels+' 个有效像素';
        $('sp-absorbance-files').replaceChildren();
        for(const [kind,url] of Object.entries(capture.files)) {
          const a=document.createElement('a');a.href=url;a.download='';
          a.textContent={csv:'CSV 数据',png:'吸光度曲线 PNG',json:'完整测量 JSON'}[kind]||kind;
          $('sp-absorbance-files').append(a);
        }
      }
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
      const blankCapture=s.halogen_capture;
      halogenCollecting=blankCapture?.status==='collecting';
      const blankProgress=$('sp-halogen-progress');
      blankProgress.hidden=!halogenCollecting;
      blankProgress.max=blankCapture?.total || 32;
      blankProgress.value=blankCapture?.count || 0;
      const halogenStatus = (s.halogen?.saved?`空白 I₀ 已保存${s.halogen.frames?'（平均 '+s.halogen.frames+' 帧）':''}。`:'')+(s.halogen?.message || '尚未保存空白卤素灯参考 I₀');
      $('sp-halogen-state').textContent = halogenCollecting
        ? `正在采集空白 I₀ · ${blankCapture.count} / ${blankCapture.total} 帧`
        : blankCapture?.status==='error' ? blankCapture.message
        : connected && (!roiReady || roiScanning) ? '请先确定采样范围。'
        : connected && !r?.stats.dark_corrected
          ? 'I₀需要匹配的暗场。' + (dark.status==='collecting'?'请等待暗场采集完成。':dark.message)
        : connected && r?.stats.zero_reference ? '请先清除样品零参考，再保存 I₀。'
        : connected && r?.stats.saturated ? (saturationPolicy.block?'当前画面达到饱和阈值；可修改阈值或关闭拦截。':'当前画面达到饱和阈值；拦截已关闭，可继续记录 I₀ 和吸光度。')
        : connected && !s.halogen?.active && r?.intensity?.length && Math.max(...r.intensity)<=1
          ? '参考信号太弱。请打开入光口、对准卤素灯并移走样品。'
        : halogenStatus;
      darkSavable=dark.savable===true;
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
        if(camera && r.roi) showRangeInput(r.roi);
        const st=r.stats;
        $('sp-stats').textContent=`${r.t_wall} · 平均 ${st.buffer_len} 帧 · 峰值 ${st.intensity_max.toFixed(1)} counts · 峰位置 ${st.peak_wavelength_nm===null ? st.peak_pixel+' px':st.peak_wavelength_nm.toFixed(2)+' nm'}${st.low_light?' · 低光':''}${st.saturated?(saturationPolicy.block?' · 达到饱和阈值（拦截）':' · 达到饱和阈值（仅提示）'):''}${st.zero_reference?' · 零参考已启用':''}${st.dark_corrected?' · 暗场已扣除':''}`;
        $('sp-cal-state').textContent=r.calibration?`${r.calibration.points.length} 点标定 · R² ${r.calibration.r_squared?.toFixed(6)} · ${r.calibration.calibration_id}`:'未标定 · 横轴为像素';
      }
      if(!r) {lastSeq=null;chart(null);preview.hidden=true;box.hidden=true;$('sp-empty').hidden=false;}
      if(!connected && r) $('sp-stats').textContent='采集已停止，显示最后一帧 · '+r.t_wall;
      const signature=JSON.stringify(s.files);
      if(signature!==lastFiles) {
        lastFiles=signature; $('sp-files').replaceChildren();
        for(const [kind,url] of Object.entries(s.files||{})) { const a=document.createElement('a');a.href=url;a.download='';a.textContent={csv:'CSV',json:'JSON',graph_png:'光谱图',frame_png:'原始帧',jsonl:'连续记录 JSONL'}[kind]||kind;$('sp-files').append(a); }
      }
    } catch(e) {connected=false;fresh=false;paintError('光谱仪服务不可用：'+e.message);$('sp-status').textContent='服务离线';}
    finally {buttons();setTimeout(poll,500);}
  }
  chart(null);drawLamp();buttons();scan();poll();
})();
