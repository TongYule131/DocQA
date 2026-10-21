"""后台启用状态随主库备份；迁移不会自动开启团队模式或创建默认账号。"""
ADMIN_TABLES = """
CREATE TABLE product_installation (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    team_enabled INTEGER NOT NULL DEFAULT 0 CHECK(team_enabled IN (0,1)),
    cookie_secure INTEGER NOT NULL DEFAULT 1 CHECK(cookie_secure IN (0,1)),
    initialized_at TEXT
);
INSERT OR IGNORE INTO product_installation(singleton) VALUES (1);
ALTER TABLE knowledge_bases ADD COLUMN updated_at TEXT;
UPDATE knowledge_bases SET updated_at=created_at WHERE updated_at IS NULL;
"""
