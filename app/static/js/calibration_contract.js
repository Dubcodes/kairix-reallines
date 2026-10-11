export function calculateMovementCalibration({start_count,end_count,actual_angle,ppr,multiplier}){
  if(start_count===null||start_count===undefined||end_count===null||end_count===undefined||actual_angle===''||actual_angle===null||actual_angle===undefined)throw new Error('Start, end and known angle are required');
  const start=Number(start_count),end=Number(end_count),angle=Number(actual_angle),cycles=Number(ppr),decode=Number(multiplier);
  if(![start,end,angle,cycles,decode].every(Number.isFinite))throw new Error('Counts, angle, PPR and multiplier must be numeric');
  const delta=end-start;if(delta===0)throw new Error('Move the camera before marking the end');
  if(angle===0)throw new Error('Known camera angle must be non-zero and signed');
  if(cycles<=0||![1,2,4].includes(decode))throw new Error('PPR and quadrature multiplier are invalid');
  const encoder_turns=Math.abs(delta)/(cycles*decode),camera_fraction=Math.abs(angle)/360,ratio=encoder_turns/camera_fraction,direction=Math.sign(delta)===Math.sign(angle)?1:-1;
  return{delta,encoder_turns,camera_fraction,encoder_revs_per_camera_rev:ratio,direction};
}
