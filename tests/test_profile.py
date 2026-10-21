from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from app.profile import MAX_BYTES, normalize_avatar
from tests.test_product_access import team


def picture(color='blue', size=(360, 200), format='PNG'):
    output = BytesIO()
    info = PngImagePlugin.PngInfo(); info.add_text('private', 'must be stripped')
    Image.new('RGB', size, color).save(output, format=format, pnginfo=info)
    return output.getvalue()


def put(client, data, **options):
    return client.put('/api/auth/avatar', files={'file': ('avatar.png', data, 'image/png')}, **options)


@pytest.mark.parametrize('format', ['PNG', 'JPEG', 'WEBP'])
def test_avatar_normalizes_size_and_strips_metadata(format):
    result = normalize_avatar(picture(format=format))
    with Image.open(BytesIO(result)) as image:
        assert image.size == (256, 256)
        assert image.format == 'PNG'
        assert not image.info


def test_avatar_persists_privately_with_atomic_replacement_and_reset(team):
    env, users, clients = team
    alice, bob = clients['alice'], clients['bob']
    assert alice.get('/api/auth/profile').json()['avatar_url'] is None
    saved = put(alice, picture())
    assert saved.status_code == 200, saved.text
    url = saved.json()['avatar_url']
    original = alice.get(url)
    assert original.status_code == 200 and original.headers['content-type'] == 'image/png'
    assert 'no-store' in original.headers['cache-control']
    assert original.headers['x-content-type-options'] == 'nosniff'
    assert bob.get('/api/auth/profile').json()['avatar_url'] is None
    assert bob.get(url).status_code == 404  # URL不携带或选择其他用户的身份。
    assert clients['anonymous'].get(url).status_code == 401
    assert put(alice, b'<svg onload="alert(1)"></svg>').status_code == 422
    assert alice.get(url).content == original.content
    assert alice.get('/api/auth/profile').json()['avatar_url'] == url
    # 新认证会话仍读取同一数据库中的头像；不是浏览器临时预览。
    from fastapi.testclient import TestClient
    with TestClient(alice.app) as fresh:
        from tests.test_product_access import PASSWORD
        assert fresh.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD}).status_code == 200
        assert fresh.get('/api/auth/avatar').content == original.content
    assert put(bob, picture('red')).status_code == 200
    assert bob.get('/api/auth/avatar').content != original.content
    assert alice.delete('/api/auth/avatar').json()['avatar_url'] is None
    assert alice.get('/api/auth/avatar').status_code == 404
    assert bob.get('/api/auth/avatar').status_code == 200
    assert not env.embedding.calls and not env.model.calls


def test_avatar_csrf_and_origin_are_required(team):
    _, _, clients = team
    alice = clients['alice']
    assert put(alice, picture(), headers={'X-CSRF-Token': ''}).status_code == 403
    assert put(alice, picture(), headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert alice.delete('/api/auth/avatar', headers={'X-CSRF-Token': ''}).status_code == 403
    assert clients['anonymous'].get('/api/auth/profile').status_code == 401


@pytest.mark.parametrize('data', [b'', b'not an image', b'<svg/>', b'x' * (MAX_BYTES + 1)],
                         ids=['empty', 'invalid', 'svg', 'too-large'])
def test_avatar_rejects_invalid_files(data):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        normalize_avatar(data)
    assert error.value.status_code == 422


def test_avatar_rejects_oversized_dimensions_and_animation():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        normalize_avatar(picture(size=(4097, 1)))
    output = BytesIO()
    Image.new('RGB', (20, 20), 'red').save(output, format='PNG', save_all=True,
        append_images=[Image.new('RGB', (20, 20), 'blue')], duration=100)
    with pytest.raises(HTTPException):
        normalize_avatar(output.getvalue())
