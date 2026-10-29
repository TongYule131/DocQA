"""N04隔离账号的完整设置／凭证边界；不访问真实账号或供应商。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from app.account_security import guard_session_stream
from app.product_access import COOKIE,verify_password
from tests.test_product_access import team,PASSWORD


def test_owner_can_edit_display_name_without_rebinding_account(team):
    env,users,clients=team
    response=clients['alice'].put('/api/auth/profile',json={'display_name':'新的合成显示名'})
    assert response.status_code==200,response.text
    user=clients['alice'].get('/api/auth/me').json()['user']
    assert user['id']==users['alice']['id'] and user['username']=='alice' and user['display_name']=='新的合成显示名'
    assert clients['bob'].get('/api/auth/me').json()['user']['display_name']=='bob'


def test_owner_can_change_password_and_revoke_current_session(team):
    env,users,clients=team
    response=clients['alice'].post('/api/auth/password',json={'old_password':PASSWORD,'new_password':'N04-new-password','confirm_password':'N04-new-password'})
    assert response.status_code==200,response.text
    assert clients['alice'].get('/api/documents').status_code==401


def test_administrator_reset_rejects_any_client_chosen_password(team):
    env,users,clients=team
    response=clients['admin'].post('/api/admin/users/'+users['alice']['id']+'/password',json={'password':'arbitrary-client-password'})
    assert response.status_code==422,response.text
    assert clients['alice'].get('/api/auth/me').json()['user']['id']==users['alice']['id']


def test_fixed_reset_requires_change_before_any_business_access(team):
    env,users,clients=team
    response=clients['admin'].post('/api/admin/users/'+users['alice']['id']+'/password',json={})
    assert response.status_code==200,response.text
    assert clients['alice'].get('/api/documents').status_code==401
    login=clients['alice'].post('/api/auth/login',json={'username':'alice','password':'123456'})
    assert login.status_code==200 and login.json()['user']['must_change_password'] is True
    assert clients['alice'].get('/api/documents').status_code==403


@pytest.mark.parametrize('name',['admin','alice'])
def test_self_display_name_is_current_in_both_roles_and_does_not_change_credentials(team,name):
    env,users,clients=team;client=clients[name]
    with env.repository.connect() as db:before=dict(db.execute('SELECT * FROM product_users WHERE id=?',(users[name]['id'],)).fetchone())
    assert client.put('/api/auth/profile',json={'display_name':'  <新名称>  '}).json()['display_name']=='<新名称>'
    with env.repository.connect() as db:after=dict(db.execute('SELECT * FROM product_users WHERE id=?',(users[name]['id'],)).fetchone())
    assert {k:v for k,v in before.items() if k!='display_name'}=={k:v for k,v in after.items() if k!='display_name'}
    assert client.get('/api/auth/profile').json()['username']==name
    for body in [{'display_name':'  '},{'display_name':'x'*81},{'display_name':'valid','user_id':users['bob']['id']},{'display_name':'valid','username':'bob'}]:
        assert client.put('/api/auth/profile',json=body).status_code==422
    assert client.get('/api/auth/me').json()['user']['display_name']=='<新名称>'


@pytest.mark.parametrize('body,status',[
    ({'old_password':'wrong-private-old','new_password':'valid-new','confirm_password':'valid-new'},400),
    ({'old_password':PASSWORD,'new_password':'different','confirm_password':'mismatched'},422),
    ({'old_password':PASSWORD,'new_password':PASSWORD,'confirm_password':PASSWORD},422),
    ({'old_password':PASSWORD,'new_password':'abc','confirm_password':'abc'},422),
    ({'old_password':PASSWORD,'new_password':'x'*33,'confirm_password':'x'*33},422),
    ({'old_password':PASSWORD,'new_password':'valid-new','confirm_password':'valid-new','user_id':'another'},422),
])
def test_failed_change_preserves_hash_sessions_and_hides_password_values(team,body,status):
    env,users,clients=team;alice=clients['alice']
    with env.repository.connect() as db:before=db.execute('SELECT password_hash FROM product_users WHERE id=?',(users['alice']['id'],)).fetchone()[0]
    response=alice.post('/api/auth/password',json=body);assert response.status_code==status,response.text
    assert all(value not in response.text for value in [body['old_password'],body['new_password'],body['confirm_password']])
    with env.repository.connect() as db:
        assert db.execute('SELECT password_hash FROM product_users WHERE id=?',(users['alice']['id'],)).fetchone()[0]==before
        assert not db.execute('SELECT 1 FROM product_account_events').fetchone()
    assert alice.get('/api/documents').status_code==200


def test_change_revokes_every_old_session_but_preserves_other_accounts_and_history(team):
    env,users,clients=team;alice=clients['alice'];extra=TestClient(alice.app)
    try:
        extra.post('/api/auth/login',json={'username':'alice','password':PASSWORD})
        old_cookie=alice.cookies.get(COOKIE)
        response=alice.post('/api/auth/password',json={'old_password':PASSWORD,'new_password':'合成新密码','confirm_password':'合成新密码'})
        assert response.status_code==200
        assert extra.get('/api/documents').status_code==401 and clients['bob'].get('/api/documents').status_code==200
        alice.cookies.clear();alice.cookies.set(COOKIE,old_cookie)
        assert alice.get('/api/auth/profile').status_code==401
        assert alice.post('/api/auth/login',json={'username':'alice','password':PASSWORD}).status_code==401
        result=alice.post('/api/auth/login',json={'username':'alice','password':'合成新密码'})
        assert result.status_code==200 and not result.json()['user']['must_change_password']
        assert result.json()['user']['id']==users['alice']['id']
    finally:extra.close()


@pytest.mark.parametrize('name',['admin','alice'])
def test_forced_change_cannot_be_bypassed_by_pages_old_apis_or_admin_role(team,name):
    env,users,clients=team;admin=clients['admin'];target=clients[name]
    assert admin.post('/api/admin/users/'+users[name]['id']+'/password',json={}).status_code==200
    login=target.post('/api/auth/login',json={'username':name,'password':'123456'});assert login.status_code==200
    target.headers['X-CSRF-Token']=login.json()['csrf_token']
    for path in ['/api/documents','/api/admin/users','/api/knowledge-bases','/api/auth/profile']:
        response=target.get(path);assert response.status_code==403 and response.json()['code']=='password_change_required'
    for path in ['/app','/single','/admin','/knowledge']:
        response=target.get(path,follow_redirects=False);assert response.status_code==303 and response.headers['location']=='/account/password'
    assert target.get('/account/password').status_code==200
    assert target.get('/static/account-password.js').status_code==200
    assert target.post('/api/auth/password',json={'old_password':'123456','new_password':'123456','confirm_password':'123456'}).status_code==422
    assert target.post('/api/auth/password',json={'old_password':'123456','new_password':'N04-final-password','confirm_password':'N04-final-password'}).status_code==200
    login=target.post('/api/auth/login',json={'username':name,'password':'N04-final-password'});assert login.status_code==200 and not login.json()['user']['must_change_password']
    assert target.get('/api/documents').status_code==200


def test_profile_password_and_fixed_reset_require_authentication_and_csrf(team):
    env,users,clients=team
    for path,body,method in [('/api/auth/profile',{'display_name':'name'},'put'),('/api/auth/password',{'old_password':PASSWORD,'new_password':'valid-new','confirm_password':'valid-new'},'post'),('/api/admin/users/'+users['alice']['id']+'/password',{},'post')]:
        assert getattr(clients['anonymous'],method)(path,json=body).status_code==401
        assert getattr(clients['admin'],method)(path,json=body,headers={'X-CSRF-Token':'wrong'}).status_code==403
        assert getattr(clients['admin'],method)(path,json=body,headers={'Origin':'https://other.invalid'}).status_code==403


def test_wrong_old_password_is_rate_limited_without_changing_credentials(team):
    env,users,clients=team;alice=clients['alice'];body={'old_password':'incorrect','new_password':'N04-private-new','confirm_password':'N04-private-new'}
    for _ in range(5):assert alice.post('/api/auth/password',json=body).status_code==400
    assert alice.post('/api/auth/password',json=body).status_code==429
    assert alice.get('/api/auth/me').status_code==200
    with env.repository.connect() as db:assert verify_password(PASSWORD,db.execute('SELECT password_hash FROM product_users WHERE id=?',(users['alice']['id'],)).fetchone()[0])


def test_audit_is_private_password_free_and_atomic_with_credentials(team):
    env,users,clients=team;admin,alice=clients['admin'],clients['alice']
    alice.put('/api/auth/profile',json={'display_name':'新的名称'})
    admin.post('/api/admin/users/'+users['alice']['id']+'/password',json={})
    login=alice.post('/api/auth/login',json={'username':'alice','password':'123456'});alice.headers['X-CSRF-Token']=login.json()['csrf_token']
    secret='N04-secret-final';alice.post('/api/auth/password',json={'old_password':'123456','new_password':secret,'confirm_password':secret})
    rows=admin.get('/api/admin/account-events',params={'user_id':users['alice']['id']})
    assert rows.status_code==200
    assert {r['action'] for r in rows.json()}=={'display_name_changed','password_reset','password_changed'}
    assert {r['credential_version'] for r in rows.json() if r['action']!='display_name_changed'}=={1,2}
    assert all(s not in rows.text for s in [secret,PASSWORD,'123456','password_hash','csrf_token'])
    assert clients['bob'].get('/api/admin/account-events').status_code==403
    assert admin.get('/api/admin/account-events',params={'limit':101}).status_code==422
    assert not env.embedding.calls and not env.model.calls


def test_audit_failure_rolls_back_password_flag_and_session_revocation(team):
    env,users,clients=team;admin=clients['admin'];target=users['alice']['id']
    with env.repository.connect() as db:
        original=db.execute('SELECT password_hash FROM product_users WHERE id=?',(target,)).fetchone()[0]
        db.execute("CREATE TRIGGER n04_audit_failure BEFORE INSERT ON product_account_events BEGIN SELECT RAISE(ABORT,'synthetic-audit-failure'); END")
    other=TestClient(admin.app,raise_server_exceptions=False);other.cookies.update(admin.cookies);other.headers.update({'X-CSRF-Token':admin.headers['X-CSRF-Token']})
    try:assert other.post('/api/admin/users/'+target+'/password',json={}).status_code==500
    finally:other.close()
    with env.repository.connect() as db:
        assert db.execute('SELECT password_hash FROM product_users WHERE id=?',(target,)).fetchone()[0]==original
        assert not db.execute('SELECT 1 FROM product_account_state WHERE user_id=? AND must_change_password=1',(target,)).fetchone()
    assert clients['alice'].get('/api/documents').status_code==200


def test_two_concurrent_changes_do_not_overwrite_the_first_committed_password(team,monkeypatch):
    import app.product_access as module
    env,users,clients=team;alice=clients['alice'];extra=TestClient(alice.app)
    login=extra.post('/api/auth/login',json={'username':'alice','password':PASSWORD});extra.headers['X-CSRF-Token']=login.json()['csrf_token']
    barrier=Barrier(2);original=module.password_hash
    def synchronized(password):
        if password.startswith('N04-race-'):barrier.wait(timeout=15)
        return original(password)
    monkeypatch.setattr(module,'password_hash',synchronized)
    def attempt(pair):
        client,password=pair
        return password,client.post('/api/auth/password',json={'old_password':PASSWORD,'new_password':password,'confirm_password':password}).status_code
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[(alice,'N04-race-one'),(extra,'N04-race-two')]))
        assert sum(status==200 for _,status in results)==1 and all(status in [200,401,409] for _,status in results)
        winner=next(password for password,status in results if status==200)
        with env.repository.connect() as db:
            assert verify_password(winner,db.execute('SELECT password_hash FROM product_users WHERE id=?',(users['alice']['id'],)).fetchone()[0])
            assert db.execute("SELECT COUNT(*) FROM product_account_events WHERE action='password_changed'").fetchone()[0]==1
    finally:extra.close()


def test_connected_session_stream_stops_after_session_is_revoked(team):
    env,users,clients=team;access=clients['alice'].app.state.product_access;cookie=clients['alice'].cookies.get(COOKIE);closed=[]
    async def source():
        try:yield b'first';yield b'private-second'
        finally:closed.append(True)
    response=SimpleNamespace(body_iterator=source());guard_session_stream(response,access,cookie,users['alice']['id'])
    async def check():
        iterator=response.body_iterator;assert await anext(iterator)==b'first'
        assert clients['admin'].post('/api/admin/users/'+users['alice']['id']+'/password',json={}).status_code==200
        with pytest.raises(StopAsyncIteration):await anext(iterator)
    asyncio.run(check());assert closed==[True]


def test_reenabled_account_still_reports_persisted_forced_change(team):
    env,users,clients=team;admin=clients['admin'];path='/api/admin/users/'+users['alice']['id']
    assert admin.patch(path,json={'active':False}).status_code==200
    assert admin.post(path+'/password',json={}).status_code==200
    response=admin.patch(path,json={'active':True})
    assert response.status_code==200 and response.json()['must_change_password'] is True
    assert clients['alice'].post('/api/auth/login',json={'username':'alice','password':'123456'}).json()['user']['must_change_password'] is True


def test_legacy_128_character_password_can_change_to_current_policy_without_lockout(team):
    import hashlib
    from app.product_access import ITERATIONS
    env,users,clients=team;legacy='L'*128;salt='01'*16
    encoded=f'pbkdf2_sha256${ITERATIONS}${salt}$'+hashlib.pbkdf2_hmac('sha256',legacy.encode(),bytes.fromhex(salt),ITERATIONS).hex()
    with env.repository.connect() as db:db.execute('UPDATE product_users SET password_hash=? WHERE id=?',(encoded,users['alice']['id']))
    alice=clients['alice'];login=alice.post('/api/auth/login',json={'username':'alice','password':legacy});assert login.status_code==200
    alice.headers['X-CSRF-Token']=login.json()['csrf_token']
    assert alice.post('/api/auth/password',json={'old_password':legacy,'new_password':'abcd','confirm_password':'abcd'}).status_code==200
    assert alice.post('/api/auth/login',json={'username':'alice','password':'abcd'}).status_code==200
