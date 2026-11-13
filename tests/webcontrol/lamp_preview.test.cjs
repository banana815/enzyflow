const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('webcontrol/static/spectrometer.js','utf8');
async function check(frames,frozen){
 const els=new Map(),strokes={},requests=[];
 const element=id=>{assert(!['sp-auto','sp-x','sp-y','sp-w','sp-h','sp-roi'].includes(id),'removed manual ROI/auto exposure control referenced');if(!els.has(id))els.set(id,{id,width:900,height:380,value:'',checked:true,dataset:{},setAttribute(){},replaceChildren(){},append(){},getContext(){return new Proxy({},{get(_,k){return k==='stroke'?()=>{strokes[id]=(strokes[id]||0)+1}:()=>{}}})}});return els.get(id)};
 const r={seq:1,pixel:[1,2,3],intensity:[2,30,2],display:[2,30,2],wavelength_nm:null,exposure:{auto:false,value:157},roi:{x:1,y:1,w:3,h:1},stats:{buffer_len:frames,zero_reference:false,intensity_max:30,peak_pixel:2,peak_wavelength_nm:null},t_wall:'test'};
 const s={roi_selection:{ready:true,scanning:false,message:'saved'},connected:true,age_ms:0,status:'connected',record:r,dark:{status:'ready',message:'ready'},files:{},lamp:frozen?{id:'fixed',seq:1,created:'test',pixels:r.pixel,intensity:[1,20,1],peaks:[{pixel:2}]}:null};
 vm.runInNewContext(source,{document:{getElementById:element,querySelector:()=>({content:'token'}),querySelectorAll:q=>q==='.sp-live'?['sp-dark-start','sp-lamp-capture','sp-lamp-calibrate','sp-lamp-prepare'].map(element):[]},fetch:async(path,options)=>{if(options.body) requests.push(JSON.parse(options.body));return {ok:true,json:async()=>s}},AbortSignal:{timeout(){}},setTimeout(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert(strokes['sp-lamp-chart']>0,'lamp curve should be drawn');
 assert(element('sp-lamp-status').textContent.includes(frozen?'已冻结':'实时预览'));
 assert.equal(element('sp-lamp-capture').disabled,frames<32||frozen);
 assert(!element('sp-error').textContent.includes('服务不可用'));
 element('sp-exposure').value='157';await element('sp-expose').onclick();
 assert.deepEqual(requests.at(-1),{op:'exposure',auto:false,value:157});
}
(async()=>{await check(8,false);await check(32,false);await check(32,true);console.log('PASS: live preview at 8/32 frames, freeze gate, frozen chart');})().catch(e=>{console.error(e);process.exit(1)});
