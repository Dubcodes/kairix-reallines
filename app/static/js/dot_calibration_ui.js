import {api,logFrontend} from './common.js?v=world-media1';
import {drawGroundGrid,groundOffsetMetres} from './render.js?v=world-media1';
import {
  drawDotCalibrationOverlay,hitTestDot,modelPointsFromObservation,projectModelPoints,
  rectangleTemplatePoints,rmsOverlayError,withoutObservation,nudgeDotPointPx,
} from './dot_lock.js?v=world-media1';

const clone=value=>structuredClone(value);
const uid=prefix=>`${prefix}-${globalThis.crypto?.randomUUID?.().slice(0,8)??Math.random().toString(36).slice(2,10)}`;
const finite=value=>Number.isFinite(Number(value));
const fmt=(value,digits=2)=>finite(value)?Number(value).toFixed(digits):'—';
const inViewport=point=>point&&point.x>=0&&point.x<=1&&point.y>=0&&point.y<=1;
const MARKER_COORDINATE_FIELDS=Object.freeze({X:'dotMarkerX',Y:'dotMarkerY'});

export class DotCalibrationUi {
  constructor(byId){
    this.byId=byId;
    this.state=null;
    this.data={targets:[],active_target_id:null,last_solution:null};
    this.editor={editing:false,draft:{},enabled:new Set(),selected:null,dragging:false,adding:false,capture:null};
    this.anchorObservationId=null;
    this.lastOverlayAnchorId=undefined;
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

  currentAxisCounts(){
    if(!this.state)return{};
    const axes=this.state.tracking?.axes||{};
    return{
      pan:axes.pan?.raw_count??null,
      tilt:axes.tilt?.raw_count??null,
      zoom:axes.zoom?.relative_count??null,
    };
  }

  observationMatchesCurrentPose(observation){
    if(!observation||!this.state)return false;
    const refs=this.referenceIds(),stored=observation.references||{};
    if((stored.pan??null)!==(refs.pan??null)||(stored.tilt??null)!==(refs.tilt??null))return false;
    if((stored.zoom??null)!==(refs.zoom??null))return false;
    const current=this.currentAxisCounts(),axes=observation.axes||{};
    const pan=axes.pan?.raw_count,tilt=axes.tilt?.raw_count,zoom=axes.zoom?.relative_count;
    if(pan!==null&&pan!==undefined&&current.pan!==null&&Math.abs(Number(current.pan)-Number(pan))>3)return false;
    if(tilt!==null&&tilt!==undefined&&current.tilt!==null&&Math.abs(Number(current.tilt)-Number(tilt))>3)return false;
    if(zoom!==null&&zoom!==undefined&&current.zoom!==null&&Math.abs(Number(current.zoom)-Number(zoom))>8)return false;
    return true;
  }

  anchorObservation(){
    const target=this.activeTarget();if(!target||this.editor.editing)return null;
    const observations=target.observations||[];
    const preferred=observations.find(obs=>obs.id===this.anchorObservationId);
    if(preferred&&this.observationMatchesCurrentPose(preferred))return preferred;
    return observations.find(obs=>this.observationMatchesCurrentPose(obs))||null;
  }

  async updateOverlay(immediate=false){
    if(this.overlayTimer){clearTimeout(this.overlayTimer);this.overlayTimer=null;}
    const send=async()=>{
      try{
        const anchor=this.anchorObservation();
        await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{
          target_id:this.data.active_target_id,
          editing:this.editor.editing,
          draft_points:this.editor.editing?this.editor.draft:{},
          reference_points:anchor?.points||{},
          reference_observation_id:anchor?.id??null,
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
    id('dotOverlayScale').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{marker_scale:Number(event.target.value)}})});
    };
    id('dotOverlayLabels').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{show_labels:event.target.checked}})});
    };
    id('dotOverlayVectors').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{show_vectors:event.target.checked}})});
    };
    id('dotGroundGridHere').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{ground_grid_calibration:event.target.checked}})});
    };
    id('dotGroundGridGfx').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{ground_grid_gfx:event.target.checked}})});
    };
    id('dotGroundGridSpacing').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{grid_spacing_m:Number(event.target.value)}})});
    };
    id('dotGroundGridExtent').onchange=async event=>{
      await api('/api/calibration-overlay',{method:'PATCH',body:JSON.stringify({values:{grid_extent_m:Number(event.target.value)}})});
    };
    for(const field of ['dotTargetName','dotLockDistance','dotTargetWidth','dotTargetHeight','dotTargetBottomHeight'])id(field).addEventListener('change',()=>this.commitTargetFields());
    id('solveDotWorldReference').onclick=()=>this.solveWorldReference();
    id('dotEdgeMidpoints').addEventListener('change',()=>this.toggleEdgeMidpoints());
    const canvas=id('dotLockCanvas');
    canvas.addEventListener('pointerdown',event=>this.pointerDown(event));
    canvas.addEventListener('pointermove',event=>this.pointerMove(event));
    canvas.addEventListener('pointerup',event=>this.pointerUp(event));
    canvas.addEventListener('pointercancel',event=>this.pointerUp(event));
    document.addEventListener('keydown',event=>this.keyDown(event));
    for(const axis of ['X','Y'])this.byId(MARKER_COORDINATE_FIELDS[axis]).addEventListener('change',event=>this.commitMarkerCoordinate(axis,event.target.value));
  }

  async newTarget(type){
    const target={
      id:uid('target'),name:type==='rectangle'?'Measured rectangle':'Calibration target',type,
      approx_distance_m:10,width_m:null,height_m:null,bottom_height_m:0,edge_midpoints:false,
      markers:[],observations:[],model_points:{},
    };
    this.data.targets.push(target);this.data.active_target_id=target.id;this.data.last_solution=null;this.anchorObservationId=null;
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
    this.data.active_target_id=this.data.targets[0]?.id??null;this.data.last_solution=null;this.anchorObservationId=null;this.cancelEdit(false);await this.persist();await this.updateOverlay(true);this.render();
  }

  async commitTargetFields(){
    const target=this.activeTarget();if(!target)return;
    target.name=this.byId('dotTargetName').value.trim()||target.name;
    const distance=Number(this.byId('dotLockDistance').value);if(finite(distance)&&distance>0)target.approx_distance_m=distance;
    const width=this.byId('dotTargetWidth').value.trim(),height=this.byId('dotTargetHeight').value.trim();
    target.width_m=width&&Number(width)>0?Number(width):null;target.height_m=height&&Number(height)>0?Number(height):null;
    const bottom=Number(this.byId('dotTargetBottomHeight').value);target.bottom_height_m=finite(bottom)?bottom:0;
    this.data.last_solution=null;this.data.world_candidate=null;await this.persist();this.render();
    if(target.observations.length>=2)await this.recalculateAll();
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
    const candidates=Object.entries(this.editor.draft).filter(([id])=>this.editor.enabled.has(id)).map(([id,observed])=>({id,observed}));
    const hit=hitTestDot(candidates,point,width,height,28);
    if(hit){this.editor.selected=String(hit.id);this.editor.dragging=true;canvas.focus({preventScroll:true});canvas.setPointerCapture?.(event.pointerId);this.updateOverlay(true);this.render();return;}
    if(this.editor.adding){
      if(target.markers.length>=20){this.setStatus('MAXIMUM 20 MARKERS','bad');return;}
      const id=this.ensureMarker(target);this.editor.draft[id]=point;this.editor.enabled.add(id);this.editor.selected=id;this.editor.dragging=true;canvas.setPointerCapture?.(event.pointerId);this.persist().catch(()=>{});this.updateOverlay();this.render();return;
    }
    // A blank click only deselects. It must never teleport whichever marker was
    // previously selected; that made off-screen-marker editing unpredictable.
    this.editor.selected=null;this.editor.dragging=false;this.updateOverlay(true);this.render();
  }
  pointerMove(event){if(!this.editor.dragging||!this.editor.selected)return;this.editor.draft[this.editor.selected]=this.pointerPoint(event);this.updateOverlay();this.draw();this.renderMarkerCoordinates();}
  pointerUp(event){if(!this.editor.dragging)return;this.editor.dragging=false;this.byId('dotLockCanvas').releasePointerCapture?.(event.pointerId);this.updateOverlay(true);this.render();}

  keyDown(event){
    if(!this.editor.editing||!this.editor.selected||!this.editor.enabled.has(this.editor.selected))return;
    if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
    const target=event.target;
    if(target?.isContentEditable||['INPUT','TEXTAREA','SELECT'].includes(target?.tagName))return;
    const point=this.editor.draft[this.editor.selected];if(!point)return;
    const next=nudgeDotPointPx(point,event.key,event);if(!next)return;
    event.preventDefault();this.editor.draft[this.editor.selected]=next;
    this.updateOverlay();this.draw();this.renderMarkerCoordinates();
  }

  commitMarkerCoordinate(axis,value){
    const id=this.editor.selected;
    if(!this.editor.editing||!id||!this.editor.enabled.has(id)||!this.editor.draft[id])return;
    const n=Number(value),denominator=axis==='X'?1920:1080;
    if(!Number.isFinite(n)||n<-.25*denominator||n>1.25*denominator){
      this.setStatus('MARKER COORDINATE MUST BE WITHIN THE SUPPORTED VIEWPORT RANGE','bad');
      this.renderMarkerCoordinates();return;
    }
    const coordinate=axis==='X'?'x':'y';
    this.editor.draft[id][coordinate]=n/denominator;
    this.updateOverlay();this.draw();this.renderMarkerCoordinates();
  }

  renderMarkerCoordinates(){
    const id=this.editor.selected,point=id?this.editor.draft[id]:null;
    const valid=Boolean(this.editor.editing&&point&&this.editor.enabled.has(id));
    this.byId('dotSelectedLabel').textContent=valid?`Selected marker: ${id}`:'Selected marker: —';
    for(const [axis,coordinate,size] of [['X','x',1920],['Y','y',1080]]){
      const input=this.byId(MARKER_COORDINATE_FIELDS[axis]);input.disabled=!valid;
      if(input!==document.activeElement)input.value=valid?(Number(point[coordinate])*size).toFixed(2):'';
    }
  }

  centerMarker(markerId){
    const target=this.activeTarget();if(!target)return;
    if(!this.editor.editing)this.prepareObservation();
    this.editor.adding=false;this.editor.selected=markerId;this.editor.enabled.add(markerId);
    this.editor.draft[markerId]={x:.5,y:.5};
    this.setStatus(`${markerId} PLACED AT VIEW CENTRE — DRAG IT ONTO THE REAL FEATURE`,'warn');
    this.updateOverlay(true);this.render();
  }

  observationPoints(){
    return Object.fromEntries([...this.editor.enabled].filter(id=>this.editor.draft[id]).map(id=>[id,{x:Number(this.editor.draft[id].x),y:Number(this.editor.draft[id].y)}]));
  }

  async saveObservation(){
    const target=this.activeTarget();if(!target||!this.editor.editing||!this.editor.capture){this.setStatus('PRESS “ADJUST THIS VIEW” BEFORE SAVING AN OBSERVATION','bad');return;}
    const points=this.observationPoints(),count=Object.keys(points).length,baseline=target.observations.length===0;
    const captured=this.editor.capture.axes||{},now=this.currentAxisCounts();
    for(const [axis,tolerance] of [['pan',3],['tilt',3],['zoom',8]]){
      const saved=axis==='zoom'?captured.zoom?.relative_count:captured[axis]?.raw_count;
      const current=now[axis];
      if(saved!==null&&saved!==undefined&&current!==null&&current!==undefined&&Math.abs(Number(current)-Number(saved))>tolerance){
        this.setStatus(`CAMERA ${axis.toUpperCase()} MOVED DURING DOT ADJUSTMENT — CANCEL AND RECAPTURE THIS VIEW`,'bad');return;
      }
    }
    if(baseline&&count<4){this.setStatus('THE BASELINE VIEW NEEDS AT LEAST FOUR MARKERS','bad');return;}
    if(!baseline&&count<1){this.setStatus('ALIGN AT LEAST ONE VISIBLE MARKER','bad');return;}
    const observation={id:uid('obs'),...clone(this.editor.capture),points};
    target.observations.push(observation);this.anchorObservationId=observation.id;
    if(baseline){
      const size=this.canvasSize();
      target.model_points=modelPointsFromObservation({camera:observation.camera,points:Object.entries(points).map(([id,p])=>({id,...p})),targetDistance:target.approx_distance_m,width:size.width,height:size.height});
    }
    this.data.last_solution=null;this.data.world_candidate=null;await this.persist();this.cancelEdit(false);await this.updateOverlay(true);this.render();
    if(target.observations.length>=2)await this.recalculateAll();else this.setStatus('BASELINE SAVED — MOVE PAN/TILT/ZOOM, STOP THE CAMERA, THEN PRESS “ADJUST THIS VIEW”','ok');
  }

  async recalculateAll(){
    const target=this.activeTarget();if(!target)return;
    this.setStatus('UPDATING CALIBRATION FITS FROM ALL SAVED OBSERVATIONS…','warn');
    // One saved observation is an immutable measurement, not a command to
    // replace the live projection. World candidates must be reviewed before use.
    try{await this.solve(true);}catch(error){logFrontend('calibration','automatic_refinement_failed',{error:String(error)});}
    if(target.type==='rectangle'&&target.width_m&&target.height_m&&target.observations.length>=2){
      try{
        const proposal=await api('/api/world-calibration/dot-calibration/preview-world',{
          method:'POST',body:JSON.stringify({values:{target_id:target.id}}),
        });
        this.data.world_candidate=proposal;this.render();
        this.setStatus(proposal.valid
          ?`WORLD CANDIDATE READY — ${fmt(proposal.rms_pixel_error,2)} px RMS · REVIEW AND ACCEPT WHEN READY`
          :`WORLD CANDIDATE REVIEW ONLY — ${fmt(proposal.rms_pixel_error,2)} px RMS · ACTIVE CAMERA UNCHANGED`,proposal.valid?'ok':'warn');
      }catch(error){
        this.data.world_candidate=null;this.render();
        this.setStatus(`WORLD CANDIDATE NOT READY — ${String(error).replace(/^Error:\s*/,'')}`,'warn');
      }
    }
  }

  async solve(silent=false){
    const targetCount=this.data.targets.reduce((sum,target)=>sum+target.observations.length,0);
    if(targetCount<2){if(!silent)this.setStatus('SAVE AT LEAST TWO OBSERVATIONS BEFORE REFINING','bad');return;}
    try{
      this.setStatus('CALCULATING REFINEMENT…','warn');
      const solution=await api('/api/world-calibration/dot-calibration/solve',{method:'POST'});
      this.data.last_solution=solution;
      if(solution.apply_allowed===false){
        this.setStatus(`REFINEMENT REVIEW ONLY — ${fmt(solution.rms_before_px,1)} px → ${fmt(solution.rms_after_px,1)} px RMS · NOT SAFE TO APPLY`,'bad');
      }else{
        this.setStatus(`SAFE REFINEMENT READY — ${fmt(solution.rms_before_px,1)} px → ${fmt(solution.rms_after_px,1)} px RMS · APPLY TO USE IT`,'ok');
      }
      this.render();
    }catch(error){this.setStatus(String(error).replace(/^Error:\s*/,''),'bad');if(!silent)logFrontend('calibration','dot_solve_failed',{error:String(error)});}
  }

  async apply(){
    if(!this.data.last_solution?.solved)return;
    if(this.data.last_solution.apply_allowed===false){
      this.setStatus('THIS REFINEMENT FAILED THE SAFETY CHECKS — REVIEW THE WARNINGS AND CALCULATE AGAIN','bad');return;
    }
    try{
      const result=await api('/api/world-calibration/dot-calibration/apply',{method:'POST'});
      this.data.last_solution=null;this.data.world_candidate=null;
      await this.updateOverlay(true);this.render();
      await this.recalculateAll();
    }catch(error){this.setStatus(String(error),'bad');}
  }

  async solveWorldReference(){
    const target=this.activeTarget();if(!target)return;
    if(target.type!=='rectangle'||!target.width_m||!target.height_m){
      this.setStatus('ENTER THE REAL RECTANGLE WIDTH AND HEIGHT BEFORE SOLVING THE VENUE/WORLD','bad');return;
    }
    if((target.observations?.length??0)<2){
      this.setStatus('SAVE AT LEAST TWO RECTANGLE OBSERVATIONS BEFORE SOLVING THE VENUE/WORLD','bad');return;
    }
    if(this.data.world_candidate?.valid!==true){
      this.setStatus('NO SAFE WORLD CANDIDATE — ADD MARKERS OR VIEWS AND REVIEW THE AUTOMATIC FIT','bad');return;
    }
    try{
      this.setStatus('SOLVING CAMERA + WORLD FROM THE MEASURED RECTANGLE…','warn');
      const result=await api('/api/world-calibration/dot-calibration/solve-world',{
        method:'POST',body:JSON.stringify({values:{target_id:target.id}}),
      });
      const solution=result.solution||{};
      if(result.valid!==true){
        this.setStatus(`${result.status||'WORLD SOLVE NOT APPLIED'} · ${fmt(solution.rms_pixel_error,1)} px RMS · PREVIOUS CAMERA MODEL RETAINED`,'bad');
      }else{
        this.data.world_candidate=null;
        this.setStatus(`WORLD CALIBRATION APPLIED — ${fmt(solution.rms_pixel_error,1)} px RMS · PIVOT ${fmt(solution.pivot?.x,2)}, ${fmt(solution.pivot?.y,2)}, ${fmt(solution.pivot?.z,2)} m`,'ok');
      }
    }catch(error){
      this.setStatus(String(error).replace(/^Error:\s*/,''),'bad');
      logFrontend('calibration','dot_world_solve_failed',{error:String(error)});
    }
  }

  async deleteObservation(targetId,observationId){
    if(this.editor.editing){this.setStatus('FINISH OR CANCEL THE CURRENT ADJUSTMENT BEFORE DELETING AN OBSERVATION','warn');return;}
    const target=this.data.targets.find(entry=>entry.id===targetId);if(!target)return;
    const original=target.observations||[];
    if(!original.some(obs=>obs.id===observationId))return;
    const backup=clone(this.data),index=original.findIndex(obs=>obs.id===observationId);
    target.observations=withoutObservation(original,observationId);
    this.data.last_solution=null;
    if(this.anchorObservationId===observationId)this.anchorObservationId=null;
    if(!target.observations.length)target.model_points={};
    try{
      this.data.world_candidate=null;
      await this.persist();await this.updateOverlay(true);this.render();
      if(target.observations.length>=2)await this.recalculateAll();
      else this.setStatus(`DELETED O${index+1} — REMAINING OBSERVATIONS KEPT; ADD MORE VIEWS TO REFIT`,'warn');
    }catch(error){
      this.data=backup;this.render();this.setStatus(`DELETE FAILED — ${String(error)}`,'bad');
    }
  }

  async clearObservations(){
    const target=this.activeTarget();if(!target)return;
    target.observations=[];target.model_points={};this.data.last_solution=null;this.data.world_candidate=null;this.anchorObservationId=null;
    await this.persist();this.cancelEdit(false);await this.updateOverlay(true);this.render();
    this.setStatus('OBSERVATIONS CLEARED — 0 SAVED · TARGET MEASUREMENTS AND MARKER LAYOUT RETAINED','warn');
  }

  renderTargetFields(){
    const target=this.activeTarget(),id=this.byId;
    const select=id('dotTargetSelect');
    const key=this.data.targets.map(t=>`${t.id}:${t.name}`).join('|');
    if(select.dataset.key!==key||select.value!==String(this.data.active_target_id??'')){
      select.dataset.key=key;select.replaceChildren(...this.data.targets.map(target=>{const option=document.createElement('option');option.value=target.id;option.textContent=target.name;option.selected=target.id===this.data.active_target_id;return option;}));
      if(!this.data.targets.length){const option=document.createElement('option');option.value='';option.textContent='No target';select.append(option);}
    }
    for(const [elementId,value] of [['dotTargetName',target?.name??''],['dotLockDistance',target?.approx_distance_m??10],['dotTargetWidth',target?.width_m??''],['dotTargetHeight',target?.height_m??''],['dotTargetBottomHeight',target?.bottom_height_m??0]]){
      const element=id(elementId);if(element!==document.activeElement)element.value=value;
    }
    id('dotEdgeMidpoints').checked=!!target?.edge_midpoints;id('dotEdgeMidpoints').disabled=!target||target.type!=='rectangle';
    id('dotTargetWidth').disabled=!target||target.type!=='rectangle';id('dotTargetHeight').disabled=!target||target.type!=='rectangle';id('dotTargetBottomHeight').disabled=!target||target.type!=='rectangle';
    id('solveDotWorldReference').disabled=!target||this.data.world_candidate?.target_id!==target.id||this.data.world_candidate?.valid!==true;
    id('deleteDotTarget').disabled=!target;id('addDotLock').disabled=!target;id('prepareDotObservation').disabled=!target;
    id('saveDotObservation').disabled=!target||!this.editor.editing;id('clearDotDraft').disabled=!this.editor.editing;
    const overlay=this.state?.calibration_overlay||{};
    id('dotOverlayGfx').checked=!!overlay.enabled;
    id('dotOverlayScale').value=String(overlay.marker_scale??2);
    id('dotOverlayLabels').checked=overlay.show_labels!==false;
    id('dotOverlayVectors').checked=overlay.show_vectors!==false;
    id('dotGroundGridHere').checked=!!overlay.ground_grid_calibration;
    id('dotGroundGridGfx').checked=!!overlay.ground_grid_gfx;
    id('dotGroundGridSpacing').value=String(overlay.grid_spacing_m??1);
    id('dotGroundGridExtent').value=String(overlay.grid_extent_m??20);
  }

  renderMarkerList(){
    const target=this.activeTarget(),root=this.byId('dotMarkerList');root.replaceChildren();if(!target)return;
    for(const marker of target.markers){
      const wrap=document.createElement('div');wrap.className='marker-control';
      const button=document.createElement('button');button.type='button';button.className=`marker-chip${this.editor.selected===marker.id?' selected':''}`;button.textContent=marker.label||marker.id;button.title='Click to select · double-click to move to viewport centre';
      button.onclick=()=>{this.editor.selected=marker.id;this.editor.adding=false;this.updateOverlay(true);this.render();};
      button.ondblclick=event=>{event.preventDefault();this.centerMarker(marker.id);};
      const label=document.createElement('label');label.className='tool-check';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=this.editor.enabled.has(marker.id);checkbox.disabled=!this.editor.editing;
      checkbox.onchange=()=>{
        if(checkbox.checked){
          this.editor.enabled.add(marker.id);
          if(!this.editor.draft[marker.id]){
            const predicted=this.predictions().find(p=>String(p.id)===String(marker.id))?.predicted;
            this.editor.draft[marker.id]=predicted&&inViewport(predicted)?{...predicted}:{x:.5,y:.5};
          }
          this.editor.selected=marker.id;
        }else{
          this.editor.enabled.delete(marker.id);delete this.editor.draft[marker.id];
          if(this.editor.selected===marker.id)this.editor.selected=null;
        }
        this.updateOverlay(true);this.render();
      };
      label.append(checkbox,document.createTextNode(' use'));wrap.append(button,label);root.append(wrap);
    }
  }

  renderObservations(){
    const target=this.activeTarget(),root=this.byId('dotObservationList');root.replaceChildren();if(!target)return;
    for(const [index,obs] of target.observations.entries()){
      const chip=document.createElement('span');chip.className='observation-chip';const axes=obs.axes||{};chip.append(document.createTextNode(`O${index+1} · ${Object.keys(obs.points||{}).length} pt · P ${axes.pan?.raw_count??'—'} · T ${axes.tilt?.raw_count??'—'} · Z ${axes.zoom?.relative_count??'—'}`));
      const remove=document.createElement('button');remove.type='button';remove.className='observation-remove';remove.textContent='×';remove.title=`Delete observation O${index+1}`;remove.setAttribute('aria-label',remove.title);remove.disabled=this.editor.editing;remove.onclick=event=>{event.preventDefault();event.stopPropagation();this.deleteObservation(target.id,obs.id);};chip.append(remove);root.append(chip);
    }
    if(target.observations.length){const clear=document.createElement('button');clear.type='button';clear.className='danger subtle';clear.textContent='Clear observations (keep target)';clear.onclick=()=>this.clearObservations();root.append(clear);}
  }

  renderWorldCandidate(){
    const node=this.byId('dotWorldCandidate'),candidate=this.data.world_candidate;
    const active=this.state?.world_calibrations?.active?.solution;
    const target=this.activeTarget(),observations=target?.observations||[];
    const rows=[
      ['Saved views',String(observations.length)],
      ['Current accepted world',active?.rms_pixel_error===undefined?'Not solved':`${fmt(active.rms_pixel_error,2)} px RMS (${active.view_count??'?'} views)`],
      ['Candidate world RMS',candidate?`${fmt(candidate.rms_pixel_error,2)} px`:'Not calculated'],
      ['Candidate worst marker',candidate?`${fmt(candidate.max_pixel_error,2)} px`:'—'],
      ['Candidate quality',candidate?`${candidate.quality||'—'} · ${candidate.valid?'VALID · manual accept':'REVIEW ONLY'}`:'—'],
      ['Target coverage',`${new Set(observations.flatMap(obs=>Object.keys(obs.points||{}))).size} distinct markers retained`],
    ];
    node.replaceChildren(...rows.flatMap(([name,value])=>{
      const a=document.createElement('div'),b=document.createElement('div');a.textContent=name;b.textContent=value;return[a,b];
    }));
  }

  renderSolution(){
    const solution=this.data.last_solution,root=this.byId('dotSolution'),apply=this.byId('applyDotCalibration');
    if(!solution?.solved){root.innerHTML='<div>Status</div><div>No refinement calculated</div>';apply.disabled=true;return;}
    const p=solution.proposed||{},c=solution.current||{},coverage=solution.coverage||{};
    const parameters=new Set(solution.parameters||[]);
    const rows=[
      ['Error',`${fmt(solution.rms_before_px,1)} → ${fmt(solution.rms_after_px,1)} px RMS`],
      ['Horizontal error',`${fmt(solution.rms_x_before_px,1)} → ${fmt(solution.rms_x_after_px,1)} px RMS`],
      ['Vertical error',`${fmt(solution.rms_y_before_px,1)} → ${fmt(solution.rms_y_after_px,1)} px RMS`],
      ['Matched placements',solution.matched_points??'—'],['Observation sets',solution.observation_sets??'—'],
      ['Pan ratio',parameters.has('pan_ratio')?`${fmt(c.pan_ratio,6)} → ${fmt(p.pan_ratio,6)}`:`${fmt(c.pan_ratio,6)} · not enough Pan movement`],
      ['Tilt ratio',parameters.has('tilt_ratio')?`${fmt(c.tilt_ratio,6)} → ${fmt(p.tilt_ratio,6)}`:`${fmt(c.tilt_ratio,6)} · not enough Tilt movement`],
      ['Horizontal FOV',parameters.has('horizontal_fov')?`${fmt(c.horizontal_fov,2)}° → ${fmt(p.horizontal_fov,2)}°`:`${fmt(c.horizontal_fov,2)}° · held by lens model`],
      ['Wide FOV',parameters.has('wide_fov')?`${fmt(c.wide_fov,2)}° → ${fmt(p.wide_fov,2)}°`:`${fmt(c.wide_fov,2)}° · held`],
      ['Tight FOV',parameters.has('tight_fov')?`${fmt(c.tight_fov,2)}° → ${fmt(p.tight_fov,2)}°`:`${fmt(c.tight_fov,2)}° · held`],
      ['Zoom curve',solution.zoom_saturated?'MODEL LIMIT — NOT APPLIED':parameters.has('zoom_curvature')?`${fmt(c.zoom_curvature,4)} → ${fmt(p.zoom_curvature,4)}`:`${fmt(c.zoom_curvature,4)} · held`],
      ['Coverage',`Pan ${coverage.pan??0} counts · Tilt ${coverage.tilt??0} · Zoom ${coverage.zoom??0}`],
    ];
    root.innerHTML=rows.map(([a,b])=>`<div>${a}</div><div>${b}</div>`).join('');
    for(const warning of solution.warnings||[]){const line=document.createElement('div');line.className='form-error dot-solution-warning';line.textContent=warning;root.append(line);}
    if(solution.apply_allowed===false){
      const title=document.createElement('div');title.className='form-error dot-solution-warning';title.textContent='APPLY BLOCKED — this proposal cannot modify the live camera model.';root.append(title);
      for(const blocker of solution.apply_blockers||[]){const line=document.createElement('div');line.className='form-error dot-solution-warning';line.textContent=`• ${blocker}`;root.append(line);}
    }
    apply.disabled=solution.apply_allowed!==true;
    apply.title=apply.disabled?(solution.apply_blockers||[]).join('; ')||'Not safe to apply':'Apply validated encoder mapping; live projection will change';
  }

  draw(){
    if(!this.state)return;const target=this.activeTarget(),{canvas,width,height}=this.canvasSize(),ctx=canvas.getContext('2d');ctx.clearRect(0,0,width,height);
    if(!target)return;
    const overlay=this.state.calibration_overlay||{};
    if(overlay.ground_grid_calibration){
      drawGroundGrid(ctx,this.state.render_camera,width,height,{spacingM:overlay.grid_spacing_m??1,extentM:overlay.grid_extent_m??20,majorEvery:overlay.grid_major_every??5,z:groundOffsetMetres(this.state)});
    }
    const predicted=projectModelPoints(target.model_points||{},this.state.render_camera,width,height);
    const anchor=this.anchorObservation(),observed=this.editor.editing?this.editor.draft:(anchor?.points||{});
    drawDotCalibrationOverlay(ctx,width,height,{predicted,observed,selected:this.editor.selected,showConnections:target.type==='rectangle',markers:target.markers,markerScale:overlay.marker_scale??1,showLabels:overlay.show_labels!==false,showVectors:overlay.show_vectors!==false});
    const error=rmsOverlayError(predicted,this.editor.editing?this.observationPoints():observed,1920,1080);
    if(error!==null){ctx.save();ctx.fillStyle='rgba(7,16,24,.84)';ctx.fillRect(10,10,260,34);ctx.fillStyle=this.editor.editing?'#ffce67':'#9fe6ff';ctx.font='700 14px system-ui';ctx.fillText(`${this.editor.editing?'Current':'Saved-view'} error ${fmt(error,1)} px RMS`,20,32);ctx.restore();}
  }

  render(){
    if(!this.state)return;this.renderTargetFields();this.renderMarkerList();this.renderObservations();this.renderSolution();this.renderWorldCandidate();
    const viewport=this.byId('dotLockViewport');viewport.classList.toggle('editing',this.editor.editing);viewport.classList.toggle('dragging',this.editor.dragging);this.draw();this.renderMarkerCoordinates();
    const target=this.activeTarget();
    if(!target)this.setStatus('CREATE A FREE OR RECTANGLE TARGET','warn');
    else if(!this.editor.editing&&target.observations.length)this.setStatus(`${target.name.toUpperCase()} — ${target.observations.length} OBSERVATION${target.observations.length===1?'':'S'} SAVED · MOVE CAMERA THEN ADJUST THIS VIEW`,'ok');
  }

  updateState(state,event){
    this.state=state;const incoming=state.world_calibrations?.active?.dot_calibration;
    if(incoming&&!this.editor.editing)this.data=clone(incoming);
    if(!this.controlsBound)this.bind();
    const anchorId=this.anchorObservation()?.id??null;
    if(!this.editor.editing&&anchorId!==this.lastOverlayAnchorId){this.lastOverlayAnchorId=anchorId;this.updateOverlay(false);}
    this.render();
  }
}
