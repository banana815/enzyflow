const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('webcontrol/static/spectrometer.js','utf8');
async function check(frames,frozen,darkReady=true,absorbance=false){
 const els=new Map(),strokes={},requests=[];
 const element=id=>{assert(!['sp-auto','sp-x','sp-y','sp-w','sp-h','sp-roi'].includes(id),'removed manual ROI/auto exposure control referenced');if(!els.has(id))els.set(id,{id,width:900,height:380,value:'',checked:true,dataset:{},setAttribute(){},replaceChildren(){},append(){},add(){},reportValidity(){return true},getContext(){return new Proxy({},{get(_,k){return k==='stroke'?()=>{strokes[id]=(strokes[id]||0)+1}:()=>{}}})}});return els.get(id)};
 const r={seq:1,pixel:[1,2,3],intensity:[2,30,2],display:[2,30,2],wavelength_nm:null,exposure:{auto:false,value:157},roi:{x:1,y:1,w:3,h:1},stats:{buffer_len:frames,zero_reference:false,intensity_max:30,peak_pixel:2,peak_wavelength_nm:null},t_wall:'test'};
 element('sp-ratio-mode').value='transmittance';
 r.photometry={active:absorbance,absorbance:[.30103,null,-.01],transmittance:[.5,null,1.02]};
 const s={roi_selection:{ready:true,scanning:false,message:'saved'},connected:true,age_ms:0,status:'connected',record:r,dark:{status:darkReady?'ready':'none',message:'test'},files:{},lamp:frozen?{id:'fixed',seq:1,created:'test',pixels:r.pixel,intensity:[1,20,1],peaks:[{pixel:2}]}:null};
 vm.runInNewContext(source,{document:{getElementById:element,querySelector:()=>({content:'token'}),querySelectorAll:q=>q==='.sp-live'?['sp-dark-start','sp-lamp-capture','sp-lamp-calibrate','sp-dark-save','sp-history-load','sp-roi-load','sp-peak-load','sp-roi-save','sp-halogen-save','sp-exposure-auto','sp-exposure-default','sp-expose'].map(element):[]},fetch:async(path,options)=>{if(options.body) requests.push(JSON.parse(options.body));return {ok:true,json:async()=>path.endsWith('-devices')?{devices:[]}:s}},AbortSignal:{timeout(){}},Option:function(label,value){Object.assign(this,{label,value})},setTimeout(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert(strokes['sp-lamp-chart']>0,'lamp curve should be drawn');
 assert(element('sp-lamp-status').textContent.includes(frozen?'已冻结':'实时预览'));
 assert.equal(element('sp-lamp-capture').disabled,frozen);
 if (!frozen) {
   await element('sp-lamp-capture').onclick();
   assert.equal(requests.at(-1).op,frames<32?'processing':'lamp_capture');
 }
 assert(!element('sp-error').textContent.includes('服务不可用'));
 assert.equal(element('sp-absorbance-save').disabled,!absorbance);
 if(absorbance){
   element('sp-absorbance-name').value=' BTB 中文_pH7.00 ';
   await element('sp-absorbance-save').onclick();
   assert.deepEqual(requests.at(-1),{op:'absorbance_save',name:'BTB 中文_pH7.00'});
   assert.equal(element('sp-ratio-mode').value,'absorbance');
 }

 element('sp-exposure').value='157';await element('sp-expose').onclick();
 assert.deepEqual(requests.at(-1),{op:'exposure',auto:false,value:157,draft:true});
}
(async()=>{await check(8,false,false);await check(32,false,false);await check(32,true);await check(32,false,true,true);console.log('PASS: live preview at 8/32 frames, automatic frame averaging, frozen chart');})().catch(e=>{console.error(e);process.exit(1)});
