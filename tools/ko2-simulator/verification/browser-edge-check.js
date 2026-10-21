async (page) => {
 const assert=(v,m)=>{if(!v)throw new Error(m)};
 await page.addInitScript(()=>{
   const proto=AudioContext.prototype, original=proto.createGain;
   proto.createGain=function(){const g=original.call(this), connect=g.connect.bind(g), c=this;
     g.connect=function(dest,...args){if(dest===c.destination){const a=c.createAnalyser();a.fftSize=2048;window.__audioProbe=a;connect(a);a.connect(dest);return dest;}return connect(dest,...args);};return g;};
 });
 await page.route('**/Samples/A/00-kick.wav',async route=>{await new Promise(r=>setTimeout(r,650));await route.continue();});
 await page.goto('http://127.0.0.1:8803/');
 await page.locator('#btn-play').click();await page.locator('#btn-stop').click();
 await page.waitForFunction(()=>window.simulator.snapshot().ready);
 await page.waitForTimeout(200);
 let canceled=await page.evaluate(()=>window.simulator.snapshot());
 assert(!canceled.playing && canceled.activeVoices===0,'stop during decode must cancel start');
 await page.locator('#btn-play').click();await page.waitForTimeout(500);
 const peak=await page.evaluate(()=>{const b=new Float32Array(window.__audioProbe.fftSize);window.__audioProbe.getFloatTimeDomainData(b);return Math.max(...b.map(Math.abs));});
 assert(peak>0.0001,'real decoded audio reaches output');
 await page.locator('#btn-stop').click();await page.waitForTimeout(150);
 await page.getByRole('tab',{name:/B/}).click();
 await page.locator('#pad-grid button').filter({hasText:'主句一'}).click();
 await page.waitForTimeout(100);assert((await page.evaluate(()=>window.simulator.snapshot())).activeVoices>0,'audition voice');
 await page.locator('#btn-stop').click();
 assert((await page.evaluate(()=>window.simulator.snapshot())).activeVoices===0,'audition stops');
 await page.unroute('**/Samples/A/00-kick.wav');
 await page.route('**/Samples/A/00-kick.wav',route=>route.fulfill({status:404,body:'missing test'}));
 await page.reload();await page.locator('#btn-play').click();
 await page.locator('#error-banner').waitFor({state:'visible'});
 assert(await page.locator('#btn-play').isDisabled(),'missing sample disables playback');
 assert(!(await page.evaluate(()=>window.simulator.snapshot())).ready,'missing sample not ready');
 await page.unroute('**/Samples/A/00-kick.wav');
 await page.reload();
 return {stopDuringLoad:true,restartAfterLoad:true,outputPeak:peak,auditionStop:true,missingAudioGate:true};
}
