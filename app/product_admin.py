"""显式本地初始化首个管理员，密码只通过 getpass 输入，不从命令参数传递。"""
import argparse
import getpass
from pathlib import Path

from app.config import Settings
from app.product_access import ProductAccess
from app.repository import Repository
from app.runtime import RuntimeGuard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    data = args.data_dir.resolve()
    print(f'目标数据目录：{data}')
    # 该入口不执行隐式旧库迁移，也不覆盖已有管理员密码。
    runtime = RuntimeGuard(data, 'account-setup')
    runtime.prepare_database(False)
    repository = Repository(data / 'docqa.db')
    repository.initialize()
    with repository.connect() as db:
        if db.execute("SELECT 1 FROM product_users WHERE role='admin'").fetchone():
            parser.error('管理员已存在；请使用现有管理员管理账号')
    password = getpass.getpass('管理员密码（4～32位）：')
    if password != getpass.getpass('再次输入密码：'):
        parser.error('两次密码不一致')
    ProductAccess(repository, Settings(data_dir=data, team_mode=True), None).create_user(args.username, args.username, password, 'admin')
    print('管理员已创建；未启动服务或发起模型请求。')


if __name__ == '__main__':
    main()
