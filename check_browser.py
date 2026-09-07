"""Optional UI check: installed Chrome, Playwright, and local server on 7870.

All API requests are intercepted: no paid provider calls or experiment mutations.
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from agent import compare, teach
from test_demo import FakeMemory, FakeModel


def fixture(phase):
    trace = dict(id="c"*32, phase=phase, status="completed", events=[])
    def emit(kind, **data):
        trace["events"].append(dict(id=len(trace["events"])+1,type=kind,**data))
    (teach if phase == "teach" else compare)(FakeModel(),FakeMemory(),trace["id"],emit)
    emit("complete", phase=phase)
    return trace


def main():
    output=Path(".demo");output.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(channel="chrome",headless=True)
        page=browser.new_page(viewport={"width":1440,"height":1100},device_scale_factor=1)
        errors=[];page.on("pageerror",lambda err:errors.append(str(err)))
        current={"trace":None,"starts":0}
        def routes(route):
            url=route.request.url
            if url.endswith('/api/status'):
                route.fulfill(json={"experiment":"sc-browser-test","model":"test-only","active":None,"missing":[]})
            elif route.request.method=="POST" and url.endswith('/api/runs'):
                current["starts"]+=1
                if current["starts"]==1:
                    route.fulfill(status=500,content_type="text/plain",body="Internal Server Error")
                    return
                current["trace"]=fixture(route.request.post_data_json["phase"])
                route.fulfill(status=202,json={"id":current["trace"]["id"]})
            elif url.endswith('/events'):
                data=''.join(f"id: {e['id']}\ndata: {json.dumps(e)}\n\n" for e in current["trace"]["events"])
                route.fulfill(content_type="text/event-stream",body=data+'event: end\ndata: {"status":"completed"}\n\n')
            else:
                route.fulfill(json=current["trace"],headers={"Content-Disposition":"attachment; filename=test-trace.json"})
        page.route("**/api/**",routes)
        page.goto("http://127.0.0.1:7870")
        page.get_by_text("Ready when you are",exact=True).wait_for()
        page.screenshot(path=str(output/"browser-initial.png"),full_page=True)
        page.get_by_role("button",name="Run teaching incidents").click()
        page.get_by_text("Request failed (HTTP 500).",exact=False).wait_for()
        assert page.get_by_role("button",name="Run teaching incidents").is_enabled()
        for phase,name,count in [("teach","Run teaching incidents",2),("compare","Compare fresh runs",3)]:
            page.get_by_role("button",name=name).click()
            page.get_by_text("Run complete. Trace ready to download.",exact=True).wait_for()
            assert page.locator('.case').count()==count
            assert page.get_by_role('link',name='Download JSON trace').is_visible()
            if phase=='compare':
                assert page.locator('.verdict').all_text_contents()==['Memory wins','Memory wins','Tie']
                assert page.locator('.arms .arm').count()==9
                assert '2 / 3 cases improved' in page.locator('#summary').inner_text()
                page.locator('.source-link').first.click()
                assert page.locator('details[open]').count()>=1
            page.screenshot(path=str(output/f"browser-{phase}-test-fixture.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output/"browser-mobile-test-fixture.png"),full_page=True)
        assert not errors,errors
        browser.close()
    print("PASS: real page with test-only API responses; HTTP errors, teaching, 3-arm comparison, citations, mobile layout; no JS errors.")


if __name__=='__main__':
    main()
