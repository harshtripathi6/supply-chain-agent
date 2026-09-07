"""Optional UI check with installed Chrome: pip install playwright, start app on 7870.

Checks real missing-config flow, then intercepts API calls with test-only traces.
Does not write fake lessons or traces to the running application.
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
        page.goto("http://127.0.0.1:7870")
        page.get_by_text("Ready when you are",exact=True).wait_for()
        page.screenshot(path=str(output/"browser-initial.png"),full_page=True)
        # Real server must surface missing config, not start a fake run.
        page.get_by_role("button",name="Run teaching incidents").click()
        page.get_by_text("Missing configuration:",exact=False).wait_for()
        assert page.get_by_role("button",name="Run teaching incidents").is_enabled()
        page.get_by_role("button",name="New experiment",exact=True).click()
        page.get_by_text("Experiment selected.",exact=False).wait_for()
        experiment=page.get_by_role("textbox",name="Experiment identifier")
        selected=experiment.input_value()
        experiment.fill("invalid")
        page.get_by_role("button",name="Use identifier").click()
        page.locator('#notice').filter(has_text='string_pattern_mismatch').wait_for()
        experiment.fill(selected)
        page.get_by_role("button",name="Use identifier").click()

        current={"trace":None}
        def routes(route):
            url=route.request.url
            if route.request.method=="POST" and url.endswith('/api/runs'):
                current["trace"]=fixture(route.request.post_data_json["phase"])
                route.fulfill(status=202,json={"id":current["trace"]["id"]})
            elif url.endswith('/events'):
                data=''.join(f"id: {e['id']}\ndata: {json.dumps(e)}\n\n" for e in current["trace"]["events"])
                route.fulfill(content_type="text/event-stream",body=data+'event: end\ndata: {"status":"completed"}\n\n')
            else:
                route.fulfill(json=current["trace"])
        page.route("**/api/runs**",routes)
        for phase,name,count in [("teach","Run teaching incidents",2),("compare","Compare fresh runs",3)]:
            page.get_by_role("button",name=name).click()
            page.get_by_text("Run complete. Trace ready to download.",exact=True).wait_for()
            assert page.locator('.case').count()==count
            assert page.get_by_role('link',name='Download JSON trace').is_visible()
            if phase=='compare':
                assert page.locator('.verdict').all_text_contents()==['Tie']*3
                page.locator('.source-link').first.click()
                assert page.locator('details[open]').count()>=1
            page.screenshot(path=str(output/f"browser-{phase}-test-fixture.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(output/"browser-mobile-test-fixture.png"),full_page=True)
        assert not errors,errors
        browser.close()
    print("PASS: real configuration/identifier flows; test-fixture teaching, comparison, citations, mobile layout; no JS errors.")


if __name__=='__main__':
    main()
