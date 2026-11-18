const POSE_FIELDS=['pose_sequence','camera','live_camera','render_camera','tracking','zoom_fov','sync','server_mono'];
const sequence=value=>Number.isSafeInteger(value?.pose_sequence)?value.pose_sequence:-1;

export function mergeOrderedFullState(current,incoming){
  if(!current||sequence(incoming)>=sequence(current))return incoming;
  const merged={...incoming};
  for(const field of POSE_FIELDS)if(field in current)merged[field]=current[field];
  return merged;
}

export function mergeOrderedPose(current,pose){
  if(!current)return{state:null,applied:false};
  if(sequence(pose)<sequence(current))return{state:current,applied:false};
  return{state:{...current,...pose,tracking:{...current.tracking,...pose.tracking,axes:{...current.tracking?.axes,...pose.tracking?.axes}}},applied:true};
}
