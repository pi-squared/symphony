"""Adopted DocumentAnalysis campaign pointer/click implementation. See provenance.json."""

CURSOR = r"""(() => {
  const install = () => {
    if (document.getElementById('qa-cursor')) return;
    const cursor = document.createElement('div'); cursor.id = 'qa-cursor';
    cursor.style.cssText = 'position:fixed;left:0;top:0;width:26px;height:32px;z-index:2147483647;pointer-events:none;transform:translate(-100px,-100px)';
    cursor.innerHTML = '<svg width="26" height="32" viewBox="0 0 26 32"><path d="M3 2 L3 25 L9 19 L14 30 L19 27 L14 17 L24 17 Z" fill="#ffdf00" stroke="#111827" stroke-width="2"/></svg>';
    document.documentElement.appendChild(cursor);
    document.addEventListener('pointermove', e => {cursor.style.transform = `translate(${e.clientX}px,${e.clientY}px)`}, true);
    document.addEventListener('pointerdown', e => {
      const ring = document.createElement('div');
      ring.style.cssText = `position:fixed;left:${e.clientX-20}px;top:${e.clientY-20}px;width:40px;height:40px;border:5px solid #ef4444;border-radius:50%;z-index:2147483646;pointer-events:none;box-sizing:border-box;background:#ef444433`;
      document.documentElement.appendChild(ring);
      ring.animate([{transform:'scale(.6)',opacity:1},{transform:'scale(1.8)',opacity:0}],{duration:650});
      setTimeout(()=>ring.remove(),700);
    }, true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',install); else install();
})();"""

class Interaction:
    def caption(self, label):
        self.page.evaluate("""label => {
            let node=document.getElementById('qa-action-caption');
            if(!node){node=document.createElement('div');node.id='qa-action-caption';
                node.style.cssText='position:fixed;left:16px;bottom:16px;max-width:520px;padding:10px 14px;background:#0f172aee;color:#fff;font:15px sans-serif;border-radius:8px;z-index:2147483645;pointer-events:none';
                document.documentElement.appendChild(node);}
            node.textContent=label;
        }""",label)

    def click(self, locator, settle_ms=650):
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box:
            self.page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2,steps=12)
        self.page.wait_for_timeout(200)
        locator.click()
        if settle_ms:
            self.page.wait_for_timeout(settle_ms)
