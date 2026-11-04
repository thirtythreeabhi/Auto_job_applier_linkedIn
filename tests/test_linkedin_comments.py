import json
from types import SimpleNamespace


def test_draft_store_persists_and_deduplicates_posts(tmp_path):
    from modules.linkedin_comments import add_draft, get_by_url, list_drafts, update_status
    path = tmp_path / 'drafts.sqlite3'
    url = 'https://www.linkedin.com/feed/update/urn:li:activity:123/'
    first = add_draft(url, 'A post about reliable retries.', 'Author', 'Relevant to retry work.',
                      'Retries should be safe and observable.', path=path)
    second = add_draft(url, 'duplicate', 'Other', 'Other', 'Other', path=path)
    assert first == second
    assert get_by_url(url, path=path)['comment'] == 'Retries should be safe and observable.'
    assert len(list_drafts(path=path)) == 1
    assert update_status(first, 'skipped', path=path)
    assert list_drafts(path=path) == []
    assert get_by_url(url, path=path)['status'] == 'skipped'


def test_comment_draft_endpoint_generates_and_saves_grounded_comment(client, tmp_path, monkeypatch):
    import app
    import modules.linkedin_comments as drafts
    import modules.ai.connections as connections
    monkeypatch.setattr(drafts, 'DB_PATH', tmp_path / 'drafts.sqlite3')
    response = SimpleNamespace(content=json.dumps({
        'relevant': True,
        'rationale': 'The post discusses background job retries documented in the candidate context.',
        'comment': 'Retries need clear states and safe recovery paths.'
    }))

    class Model:
        def invoke(self, prompt):
            assert 'CANDIDATE CONTEXT' in prompt
            assert 'LINKEDIN POST' in prompt
            return response

    fake_client = SimpleNamespace(model=Model(), model_name='test-model', provider='test')
    monkeypatch.setattr(connections, 'create_ai_client', lambda **kwargs: fake_client)
    monkeypatch.setattr(connections, 'close_ai_client', lambda client: None)

    url = 'https://www.linkedin.com/feed/update/urn:li:activity:123/'
    result = client.post('/api/comment-drafts', json={
        'post_url': url, 'author': 'Example', 'post_text': 'How do you make job retries safe?'
    })
    assert result.status_code == 201
    data = result.get_json()
    assert data['draft']['comment'] == 'Retries need clear states and safe recovery paths.'
    assert client.get('/api/comment-drafts').get_json()[0]['post_url'] == url
    assert client.post('/api/comment-drafts', json={
        'post_url': url, 'post_text': 'Duplicate post'
    }).status_code == 409


def test_comment_draft_endpoint_rejects_non_linkedin_url(client):
    response = client.post('/api/comment-drafts', json={
        'post_url': 'https://example.com/post', 'post_text': 'Something'
    })
    assert response.status_code == 400


def test_irrelevant_post_is_not_saved_as_a_draft(client, tmp_path, monkeypatch):
    import modules.linkedin_comments as drafts
    import modules.ai.connections as connections
    monkeypatch.setattr(drafts, 'DB_PATH', tmp_path / 'drafts.sqlite3')

    class Model:
        def invoke(self, prompt):
            return SimpleNamespace(content=json.dumps({
                'relevant': False, 'rationale': 'The post has no meaningful connection to the candidate context.',
                'comment': ''
            }))

    fake_client = SimpleNamespace(model=Model(), model_name='test-model', provider='test')
    monkeypatch.setattr(connections, 'create_ai_client', lambda **kwargs: fake_client)
    monkeypatch.setattr(connections, 'close_ai_client', lambda client: None)
    response = client.post('/api/comment-drafts', json={
        'post_url': 'https://www.linkedin.com/feed/update/urn:li:activity:456/',
        'post_text': 'A post unrelated to software engineering.'
    })
    assert response.status_code == 200
    assert response.get_json()['relevant'] is False
    assert client.get('/api/comment-drafts').get_json() == []
