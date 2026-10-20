'use strict';
// Waits between registry read-backs. npm can take several minutes to show a version it just accepted.
function readbackDelays(totalMs=600000,firstMs=15000,capMs=60000) {
  const delays=[0];
  let spent=0,next=firstMs;
  while(spent<totalMs) {
    const delay=Math.min(next,totalMs-spent);
    delays.push(delay);spent+=delay;next=Math.min(next*2,capMs);
  }
  return delays;
}
module.exports={readbackDelays};
