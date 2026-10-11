import {connectState} from './common.js?v=world-media1';
import {renderScene} from './render.js?v=world-media1';
import {updatePreviewMedia} from './preview_media.js?v=world-media1';
const stage=document.getElementById('previewStage'),canvas=document.getElementById('compositeCanvas'),badge=document.getElementById('previewBadge');let state;
connectState(incoming=>{state=incoming;updatePreviewMedia(stage,state.engineering,true);},'preview');
function frame(){if(state){const result=renderScene(canvas,state,{transparent:true,allowProvisional:true});badge.textContent=result.projection.mode==='calibrated'?'CALIBRATED':result.projection.mode==='provisional'?'PROVISIONAL · WORLD CALIBRATION REQUIRED':'TRACKING INVALID';badge.className=`view-badge ${result.projection.mode==='calibrated'?'calibrated':result.projection.mode==='provisional'?'provisional':'blocked'}`;}requestAnimationFrame(frame);}frame();
