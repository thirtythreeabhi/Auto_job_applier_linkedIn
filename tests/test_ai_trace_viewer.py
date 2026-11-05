from pathlib import Path


def test_control_panel_contains_independent_read_only_ai_trace_viewer(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    assert "AI Traces" in html
    assert "AI Prompts" in html
    assert "Edit system prompts" in html
    assert "fetch('/api/ai-traces?'" in html
    assert "fetch('/api/ai-traces/'" in html
    assert "Export JSON" in html
    assert "traceDecisionFilter" in html
    assert "traceStatusFilter" in html
    assert "traceFromFilter" in html and "traceToFilter" in html
    assert "Tool calls and workflow events" in html
    assert "Candidate context" in html


def test_trace_api_contract_is_documented_in_frontend_source():
    html = (Path(__file__).resolve().parents[1] / "templates" / "control_panel.html").read_text(encoding="utf-8")

    assert "GET /api/ai-traces?limit=50&offset=0" in html
    assert "GET /api/ai-traces/<trace_id>" in html
    assert "items:[{trace_id,run_id,job_id" in html
    assert "Events use {event_id,sequence,timestamp,event_type,name,payload}" in html
    assert "event_type `workflow`" in html and "`llm_tool_call`" in html
