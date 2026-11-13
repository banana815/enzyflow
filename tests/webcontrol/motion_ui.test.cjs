const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements=new Map(),intervals=[];
function element(id){
 if(!elements.has(id)) elements.set(id,{id,value:'12',checked:false,hidden:false,dataset:{},children:[],style:{setProperty(){}},classList:{toggle(){}},querySelector(selector){return element(id+selector)},addEventListener(){},setAttribute(){},removeAttribute(){},append(...x){this.children.push(...x)},replaceChildren(...x){this.children=x},focus(){},scrollIntoView(){}});
 return elements.get(id);
}
let state={connected:true,configured:true,session:'s',owner:'page-test',events:[],long_id:1,capabilities:['continuous_move','continuous_reset','pump_rates'],
 axes:[1,2,3].map(id=>({id,name:'axis '+id,position_mm:100,anchored:true,min_mm:30,max_mm:id===1?310:210,physical_min_mm:20,physical_max_mm:id===1?320:220,lead_mm:4,forward_sign:-1,max_jog_mm:1})),
 saved_positions:{reliable:true,saved_at:new Date().toISOString(),axes:[{id:1,position_mm:100}],reason:'confirmed'}};
vm.runInNewContext(fs.readFileSync('webcontrol/static/app.js','utf8'),{
 document:{getElementById:element,querySelector:()=>({content:'token'}),createElement:tag=>element('new-'+Math.random()),createTextNode:text=>({textContent:text})},
 window:{crypto:{randomUUID:()=> 'page-test'},addEventListener(){}},
 AbortController,fetch:async()=>({ok:true,json:async()=>state}),setTimeout(){},clearTimeout(){},setInterval(fn){intervals.push(fn)}
});
(async()=>{
 await new Promise(r=>setImmediate(r));
 assert(!element('restore-positions').disabled);
 assert(element('saved-position-state').textContent.includes('100.000'));
 state={...state,job:{kind:'reset_continuous',reset:false,state:'running',progress:.5,members:[{axis:1,start_mm:100,target_mm:110,position_mm:105,rpm:12,done:false}]}};
 await intervals[0]();
 assert.equal(element('job-title').textContent,'连续定位');
 assert(element('restore-positions').disabled);
 assert.equal(element('stop-button').textContent,'停止运动');
 console.log('PASS: saved position restore readiness, continuous job and stop UI');
})().catch(e=>{console.error(e);process.exit(1)});
