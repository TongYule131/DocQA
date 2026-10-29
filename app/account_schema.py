"""账号安全状态独立增量迁移；旧账号、会话、业务归属与口令哈希保持。"""
ACCOUNT_SCHEMA = """
CREATE TABLE product_account_state (
    user_id TEXT PRIMARY KEY REFERENCES product_users(id),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
    credential_version INTEGER NOT NULL DEFAULT 0 CHECK(credential_version >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE product_account_events (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL REFERENCES product_users(id),
    target_user_id TEXT NOT NULL REFERENCES product_users(id),
    action TEXT NOT NULL CHECK(action IN ('display_name_changed','password_changed','password_reset')),
    credential_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_account_events_target ON product_account_events(target_user_id,created_at);
"""


def migrate(db):
    from app.migrations import _exec_script
    from app.repository import now_iso
    _exec_script(db,ACCOUNT_SCHEMA)
    # 旧账号不因升级被强制登出；只有本轮之后的明确重置设置强制改密。
    db.execute('INSERT INTO product_account_state(user_id,must_change_password,credential_version,updated_at) '
               'SELECT id,0,0,? FROM product_users',(now_iso(),))
