import {api,logFrontend} from './common.js?v=world-media1';
import {
  drawDotCalibrationOverlay,hitTestDot,modelPointsFromObservation,projectModelPoints,
  rectangleTemplatePoints,rmsOverlayError,
} from './dot_lock.js?v=world-media1';

const clone=value=>structuredClone(value);
const uid=prefix=>`${prefix}-${globalThis.crypto?.randomUUID?.().slice(0,8)??Math.random().toString(36).slice(2,10)}`;
const finite=value=>Number.isFinite(Number(value));
const fmt=(value,digits=2)=>finite(value)?Number(value).toFixed(digits):'—';
const inViewport=point=>point&&point.x>=0&&point.x<=1&&point.y>=0&&point.y<=1;

export class DotCalibrationUi {
  constructor(byId){
    this.byId=byId;
    this.state=null;
    this.data={targets:[],active_target_id:null,last_solution:null};
    this.editor={editing:false,draft:{},enabled:new Set(),selected:null,dragging:false,adding:false,capture:null};
    this.overlayTimer=null;
    this.controlsBound=false;
  }

  activeTarget(){return this.data.targets.find(target=>target.id===this.data.active_target_id)||null;}
  referenceIds(){
    if(!this.state)return{};
    return{
      pan:this.state.tracking.axes.pan.reference_id??null,
      tilt:this.state.tracking.axes.tilt.reference_id??null,
      zoom:this.state.zoom_fov?.active?(this.state.tracking.axes.zoom.reference_id??null):null,
    };
  }
  captureContext(){
    const axis=name=>{
      const runtime=this.state.tracking.axes[name]||{};
      return{
        raw_count:runtime.raw_count??null,
        reference_count:runtime.reference_count??null,
        reference_angle:runtime.reference_angle??0,
        relative_count:runtime.relative_count??null,
      };
    };
    return{
      captured_mono:Number(this.state.server_mono||0),
      references:this.referenceIds(),
      axes:{pan:axis('pan'),tilt:axis('tilt'),zoom:axis('zoom')},
      camera:clone(this.state.render_camera),
    };
  }

  async persist(){
    this.data=await api('/api/world-calibration/dot-calibration',{method:'PATCH',body:JSON.stringify({values:this.data})});
    return this.data;
  }

  async updateOverlay(immediate=false){
    if(this.overlayTimer){clearTimeout(this.overlayTimer);this.overlayTimer=null;}
    const send=async()=>{
      try{
        await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{
          target_id:this.data.active_target_id,
          editing:this.editor.editing,
          draft_points:this.editor.editing?this.editor.draft:{},
          selected_marker:this.editor.selected,
        }})});
      }catch(error){logFrontend('calibration','dot_overlay_update_failed',{error:String(error)});}
    };
    if(immediate)return send();
    this.overlayTimer=setTimeout(send,45);
  }

  setStatus(message,kind='warn'){
    const element=this.byId('dotLockStatus');if(!element)return;
    element.textContent=message;element.className=`status-${kind}`;
  }

  bind(){
    if(this.controlsBound)return;this.controlsBound=true;
    const id=this.byId;
    id('newFreeDotTarget').onclick=()=>this.newTarget('free');
    id('newRectangleDotTarget').onclick=()=>this.newTarget('rectangle');
    id('deleteDotTarget').onclick=()=>this.deleteTarget();
    id('dotTargetSelect').onchange=async event=>{this.data.active_target_id=event.target.value||null;this.cancelEdit();await this.persist();this.render();};
    id('addDotLock').onclick=()=>{if(!this.activeTarget())return;this.editor.adding=true;this.editor.editing=true;this.setStatus('CLICK THE VIEWPORT TO PLACE THE NEW MARKER');this.render();};
    id('deleteDotLock').onclick=()=>this.deleteSelectedMarker();
    id('prepareDotObservation').onclick=()=>this.prepareObservation();
    id('saveDotObservation').onclick=()=>this.saveObservation();
    id('clearDotDraft').onclick=()=>this.cancelEdit();
    id('solveDotCalibration').onclick=()=>this.solve();
    id('applyDotCalibration').onclick=()=>this.apply();
    id('dotOverlayGfx').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{enabled:event.target.checked,target_id:this.data.active_target_id}})});
    };
    for(const field of ['dotTargetName','dotLockDistance','dotTargetWidth','dotTargetHeight'])id(field).addEventListener('change',()=>this.commitTargetFields());
    id('dotEdgeMidpoints').addEventListener('change',()=>this.toggleEdgeMidpoints());
    const canvas=id('dotLockCanvas');
    canvas.addEventListener('pointerdown',event=>this.pointerDown(event));
    canvas.addEventListener('pointermove',event=>this.pointerMove(event));
    canvas.addEventListener('pointerup',event=>this.pointerUp(event));
    canvas.addEventListener('pointercancel',event=>this.pointerUp(event));
  }

  async newTarget(type){
    const target={
      id:uid('target'),name:type==='rectangle'?'Measured rectangle':'Calibration target',type,
      approx_distance_m:10,width_m:null,height_m:null,edge_midpoints:false,
      markers:[],observations:[],model_points:{},
    };
    this.data.targets.push(target);this.data.active_target_id=target.id;this.data.last_solution=null;
    this.editor={editing:true,draft:{},enabled:new Set(),selected:null,dragging:false,adding:type==='free',capture:this.captureContext()};
    if(type==='rectangle')this.installRectangleMarkers(target,false);
    await this.persist();await this.updateOverlay(true);this.render();
    this.setStatus(type==='rectangle'?'DRAG THE FOUR MARKERS ONTO THE REAL RECTANGLE, THEN SAVE THE BASELINE':'CLICK THE VIEWPORT TO PLACE AT LEAST FOUR MARKERS','warn');
  }

  installRectangleMarkers(target,edgeMidpoints){
    const template=rectangleTemplatePoints(edgeMidpoints);
    target.edge_midpoints=edgeMidpoints;
    target.markers=template.map(point=>({id:point.id,label:point.id}));
    this.editor.draft=Object.fromEntries(template.map(point=>[point.id,{x:point.x,y:point.y}]));
    this.editor.enabled=new Set(template.map(point=>point.id));
    this.editor.selected=template[0]?.id??null;
  }

  async toggleEdgeMidpoints(){
    const target=this.activeTarget();if(!target||target.type!=='rectangle')return;
    const enabled=this.byId('dotEdgeMidpoints').checked;
    if(target.observations.length){this.byId('dotEdgeMidpoints').checked=target.edge_midpoints;this.setStatus('CLEAR SAVED OBSERVATIONS BEFORE CHANGING THE RECTANGLE MARKER LAYOUT','bad');return;}
    this.installRectangleMarkers(target,enabled);await this.persist();await this.updateOverlay(true);this.render();
  }

  async deleteTarget(){
    const target=this.activeTarget();if(!target)return;
    this.data.targets=this.data.targets.filter(item=>item.id!==target.id);
    this.data.active_target_id=this.data.targets[0]?.id??null;this.data.last_solution=null;this.cancelEdit(false);await this.persist();await this.updateOverlay(true);this.render();
  }

  async commitTargetFields(){
    const target=this.activeTarget();if(!target)return;
    target.name=this.byId('dotTargetName').value.trim()||target.name;
    const distance=Number(this.byId('dotLockDistance').value);if(finite(distance)&&distance>0)target.approx_distance_m=distance;
    const width=this.byId('dotTargetWidth').value.trim(),height=this.byId('dotTargetHeight').value.trim();
    target.width_m=width&&Number(width)>0?Number(width):null;target.height_m=height&&Number(height)>0?Number(height):null;
    this.data.last_solution=null;await this.persist();this.render();
  }

  async deleteSelectedMarker(){
    const target=this.activeTarget(),selected=this.editor.selected;if(!target||!selected)return;
    if(target.observations.length){this.setStatus('CLEAR SAVED OBSERVATIONS BEFORE DELETING TARGET MARKERS','bad');return;}
    target.markers=target.markers.filter(marker=>marker.id!==selected);delete this.editor.draft[selected];this.editor.enabled.delete(selected);this.editor.selected=target.markers[0]?.id??null;
    await this.persist();await this.updateOverlay(true);this.render();
  }

  predictions(){
    const target=this.activeTarget();if(!target||!this.state)return[];
    return projectModelPoints(target.model_points||{},this.state.render_camera,this.canvasSize().width,this.canvasSize().height);
  }

  prepareObservation(){
    const target=this.activeTarget();if(!target){this.setStatus('CREATE OR SELECT A TARGET FIRST','bad');return;}
    this.editor.editing=true;this.editor.adding=target.type==='free'&&!target.markers.length;this.editor.capture=this.captureContext();this.editor.draft={};this.editor.enabled=new Set();
    if(!target.observations.length){
      if(target.type==='rectangle'&&!target.markers.length)this.installRectangleMarkers(target,target.edge_midpoints);
      for(const marker of target.markers){if(this.editor.draft[marker.id])this.editor.enabled.add(marker.id);}
      this.setStatus(target.markers.length?'ALIGN THE BASELINE MARKERS, THEN SAVE OBSERVATION 1':'CLICK THE VIEWPORT TO PLACE AT LEAST FOUR BASELINE MARKERS','warn');
    }else{
      for(const point of this.predictions())if(inViewport(point.predicted)){this.editor.draft[String(point.id)]={...point.predicted};this.editor.enabled.add(String(point.id));}
      this.editor.selected=[...this.editor.enabled][0]??target.markers[0]?.id??null;
      this.setStatus('CAMERA POSE CAPTURED — DRAG VISIBLE MARKERS ONTO THE REAL FEATURES, THEN SAVE','warn');
    }
    this.updateOverlay();this.render();
  }

  cancelEdit(update=true){
    this.editor={editing:false,draft:{},enabled:new Set(),selected:null,dragging:false,adding:false,capture:null};
    if(update)this.updateOverlay();this.render();
  }

  ensureMarker(target){
    const id=`M${target.markers.length+1}`;target.markers.push({id,label:id});return id;
  }

  canvasSize(){
    const canvas=this.byId('dotLockCanvas'),rect=canvas.getBoundingClientRect(),dpr=Math.min(globalThis.devicePixelRatio||1,2);
    const width=Math.max(320,Math.round(rect.width*dpr)),height=Math.max(180,Math.round(rect.height*dpr));
    if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
    return{canvas,width,height,rect,dpr};
  }

  pointerPoint(event){const {rect}=this.canvasSize();return{x:(event.clientX-rect.left)/rect.width,y:(event.clientY-rect.top)/rect.height};}
  pointerDown(event){
    const target=this.activeTarget();if(!target||!this.editor.editing)return;
    const point=this.pointerPoint(event),{width,height,canvas}=this.canvasSize();
    const candidates=Object.entries(this.editor.draft).map(([id,observed])=>({id,observed}));
    const hit=hitTestDot(candidates,point,width,height,22);
    if(hit){this.editor.selected=String(hit.id);this.editor.dragging=true;canvas.setPointerCapture?.(event.pointerId);this.render();return;}
    if(this.editor.adding){
      if(target.markers.length>=20){this.setStatus('MAXIMUM 20 MARKERS','bad');return;}
      const id=this.ensureMarker(target);this.editor.draft[id]=point;this.editor.enabled.add(id);this.editor.selected=id;this.editor.dragging=true;canvas.setPointerCapture?.(event.pointerId);this.persist().catch(()=>{});this.updateOverlay();this.render();return;
    }
    if(this.editor.selected){this.editor.draft[this.editor.selected]=point;this.editor.enabled.add(this.editor.selected);this.editor.dragging=true;canvas.setPointerCapture?.(event.pointerId);this.updateOverlay();this.render();}
  }
  pointerMove(event){if(!this.editor.dragging||!this.editor.selected)return;this.editor.draft[this.editor.selected]=this.pointerPoint(event);this.updateOverlay();this.draw();}
  pointerUp(event){if(!this.editor.dragging)return;this.editor.dragging=false;this.byId('dotLockCanvas').releasePointerCapture?.(event.pointerId);this.updateOverlay(true);this.render();}

  observationPoints(){
    return Object.fromEntries([...this.editor.enabled].filter(id=>this.editor.draft[id]).map(id=>[id,{x:Number(this.editor.draft[id].x),y:Number(this.editor.draft[id].y)}]));
  }

  async saveObservation(){
    const target=this.activeTarget();if(!target||!this.editor.editing||!this.editor.capture){this.setStatus('PRESS “ADJUST THIS VIEW” BEFORE SAVING AN OBSERVATION','bad');return;}
    const points=this.observationPoints(),count=Object.keys(points).length,baseline=target.observations.length===0;
    if(baseline&&count<4){this.setStatus('THE BASELINE VIEW NEEDS AT LEAST FOUR MARKERS','bad');return;}
    if(!baseline&&count<1){this.setStatus('ALIGN AT LEAST ONE VISIBLE MARKER','bad');return;}
    const observation={id:uid('obs'),...clone(this.editor.capture),points};
    target.observations.push(observation);
    if(baseline){
      const size=this.canvasSize();
      target.model_points=modelPointsFromObservation({camera:observation.camera,points:Object.entries(points).map(([id,p])=>({id,...p})),targetDistance:target.approx_distance_m,width:size.width,height:size.height});
    }
    this.data.last_solution=null;await this.persist();this.cancelEdit(false);await this.updateOverlay(true);this.render();
    if(target.observations.length>=2)await this.solve(true);else this.setStatus('BASELINE SAVED — MOVE PAN/TILT/ZOOM, STOP THE CAMERA, THEN PRESS “ADJUST THIS VIEW”','ok');
  }

  async solve(silent=false){
    const targetCount=this.data.targets.reduce((sum,target)=>sum+target.observations.length,0);
    if(targetCount<2){if(!silent)this.setStatus('SAVE AT LEAST TWO OBSERVATIONS BEFORE REFINING','bad');return;}
    try{
      this.setStatus('CALCULATING REFINEMENT…','warn');
      const solution=await api('/api/world-calibration/dot-calibration/solve',{method:'POST'});
      this.data.last_solution=solution;
      this.setStatus(`REFINEMENT READY — ${fmt(solution.rms_before_px,1)} px → ${fmt(solution.rms_after_px,1)} px RMS · APPLY TO USE IT`,'ok');this.render();
    }catch(error){this.setStatus(String(error).replace(/^Error:\s*/,''),'bad');if(!silent)logFrontend('calibration','dot_solve_failed',{error:String(error)});}
  }

  async apply(){
    if(!this.data.last_solution?.solved)return;
    try{
      const result=await api('/api/world-calibration/dot-calibration/apply',{method:'POST'});
      this.setStatus(`REFINEMENT APPLIED — PAN ${fmt(result.pan_ratio,6)} · TILT ${fmt(result.tilt_ratio,6)} · ZOOM CURVE ${fmt(result.zoom_curvature,4)}`,'ok');
    }catch(error){this.setStatus(String(error),'bad');}
  }

  async clearObservations(){
    const target=this.activeTarget();if(!target)return;target.observations=[];target.model_points={};this.data.last_solution=null;await this.persist();this.cancelEdit(false);this.render();this.setStatus('OBSERVATIONS CLEARED — TARGET MEASUREMENTS AND MARKER LAYOUT RETAINED','warn');
  }

  renderTargetFields(){
    const target=this.activeTarget(),id=this.byId;
    const select=id('dotTargetSelect');
    const key=this.data.targets.map(t=>`${t.id}:${t.name}`).join('|');
    if(select.dataset.key!==key||select.value!==String(this.data.active_target_id??'')){
      select.dataset.key=key;select.replaceChildren(...this.data.targets.map(target=>{const option=document.createElement('option');option.value=target.id;option.textContent=target.name;option.selected=target.id===this.data.active_target_id;return option;}));
      if(!this.data.targets.length){const option=document.createElement('option');option.value='';option.textContent='No target';select.append(option);}
    }
    for(const [elementId,value] of [['dotTargetName',target?.name??''],['dotLockDistance',target?.approx_distance_m??10],['dotTargetWidth',target?.width_m??''],['dotTargetHeight',target?.height_m??'']]){
      const element=id(elementId);if(element!==document.activeElement)element.value=value;
    }
    id('dotEdgeMidpoints').checked=!!target?.edge_midpoints;id('dotEdgeMidpoints').disabled=!target||target.type!=='rectangle';
    id('dotTargetWidth').disabled=!target||target.type!=='rectangle';id('dotTargetHeight').disabled=!target||target.type!=='rectangle';
    id('deleteDotTarget').disabled=!target;id('addDotLock').disabled=!target;id('prepareDotObservation').disabled=!target;
    id('saveDotObservation').disabled=!target||!this.editor.editing;id('clearDotDraft').disabled=!this.editor.editing;
    id('dotOverlayGfx').checked=!!this.state?.calibration_overlay?.enabled;
  }

  renderMarkerList(){
    const target=this.activeTarget(),root=this.byId('dotMarkerList');root.replaceChildren();if(!target)return;
    for(const marker of target.markers){
      const wrap=document.createElement('div');wrap.className='marker-control';
      const button=document.createElement('button');button.type='button';button.className=`marker-chip${this.editor.selected===marker.id?' selected':''}`;button.textContent=marker.label||marker.id;button.onclick=()=>{this.editor.selected=marker.id;this.editor.adding=false;this.render();};
      const label=document.createElement('label');label.className='tool-check';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=this.editor.enabled.has(marker.id);checkbox.disabled=!this.editor.editing;checkbox.onchange=()=>{if(checkbox.checked){this.editor.enabled.add(marker.id);if(!this.editor.draft[marker.id]){const predicted=this.predictions().find(p=>String(p.id)===String(marker.id))?.predicted;if(predicted)this.editor.draft[marker.id]={...predicted};}}else this.editor.enabled.delete(marker.id);this.updateOverlay();this.draw();};label.append(checkbox,document.createTextNode(' use'));
      wrap.append(button,label);root.append(wrap);
    }
  }

  renderObservations(){
    const target=this.activeTarget(),root=this.byId('dotObservationList');root.replaceChildren();if(!target)return;
    for(const [index,obs] of target.observations.entries()){
      const chip=document.createElement('span');chip.className='observation-chip';const axes=obs.axes||{};chip.textContent=`O${index+1} · ${Object.keys(obs.points||{}).length} pt · P ${axes.pan?.raw_count??'—'} · T ${axes.tilt?.raw_count??'—'} · Z ${axes.zoom?.relative_count??'—'}`;root.append(chip);
    }
    if(target.observations.length){const clear=document.createElement('button');clear.type='button';clear.className='danger subtle';clear.textContent='Clear observations (keep target)';clear.onclick=()=>this.clearObservations();root.append(clear);}
  }

  renderSolution(){
    const solution=this.data.last_solution,root=this.byId('dotSolution'),apply=this.byId('applyDotCalibration');
    if(!solution?.solved){root.innerHTML='<div>Status</div><div>No refinement calculated</div>';apply.disabled=true;return;}
    const p=solution.proposed||{},c=solution.current||{},coverage=solution.coverage||{};
    const rows=[
      ['Error',`${fmt(solution.rms_before_px,1)} → ${fmt(solution.rms_after_px,1)} px RMS`],
      ['Matched markers',solution.matched_points??'—'],['Observation sets',solution.observation_sets??'—'],
      ['Pan ratio',`${fmt(c.pan_ratio,6)} → ${fmt(p.pan_ratio,6)}`],['Tilt ratio',`${fmt(c.tilt_ratio,6)} → ${fmt(p.tilt_ratio,6)}`],
      ['Zoom curve',`${fmt(c.zoom_curvature,4)} → ${fmt(p.zoom_curvature,4)}`],
      ['Coverage',`Pan ${coverage.pan??0} counts · Tilt ${coverage.tilt??0} · Zoom ${coverage.zoom??0}`],
    ];root.innerHTML=rows.map(([a,b])=>`<div>${a}</div><div>${b}</div>`).join('');apply.disabled=false;
  }

  draw(){
    if(!this.state)return;const target=this.activeTarget(),{canvas,width,height}=this.canvasSize(),ctx=canvas.getContext('2d');ctx.clearRect(0,0,width,height);
    if(!target)return;
    const predicted=projectModelPoints(target.model_points||{},this.state.render_camera,width,height);
    drawDotCalibrationOverlay(ctx,width,height,{predicted,observed:this.editor.editing?this.editor.draft:{},selected:this.editor.selected,showConnections:target.type==='rectangle',markers:target.markers});
    if(this.editor.editing){const error=rmsOverlayError(predicted,this.observationPoints(),1920,1080);if(error!==null){ctx.save();ctx.fillStyle='rgba(7,16,24,.8)';ctx.fillRect(10,10,180,30);ctx.fillStyle='#ffce67';ctx.font='700 14px system-ui';ctx.fillText(`Current error ${fmt(error,1)} px RMS`,20,30);ctx.restore();}}
  }

  render(){
    if(!this.state)return;this.renderTargetFields();this.renderMarkerList();this.renderObservations();this.renderSolution();
    const viewport=this.byId('dotLockViewport');viewport.classList.toggle('editing',this.editor.editing);viewport.classList.toggle('dragging',this.editor.dragging);this.draw();
    const target=this.activeTarget();
    if(!target)this.setStatus('CREATE A FREE OR RECTANGLE TARGET','warn');
    else if(!this.editor.editing&&target.observations.length)this.setStatus(`${target.name.toUpperCase()} — ${target.observations.length} OBSERVATION${target.observations.length===1?'':'S'} SAVED · MOVE CAMERA THEN ADJUST THIS VIEW`,'ok');
  }

  updateState(state,event){
    this.state=state;const incoming=state.world_calibrations?.active?.dot_calibration;
    if(incoming&&!this.editor.editing)this.data=clone(incoming);
    if(!this.controlsBound)this.bind();
    this.render();
  }
}
