"""托管角色由自身轮转文件，避免Windows重定向句柄阻止长期日志轮换。"""
import logging
from logging.handlers import RotatingFileHandler
import os


def configure(data_dir,role):
    if os.getenv('DOCQA_MANAGED_LOGGING')!='1':return
    directory=data_dir/'.local-service';directory.mkdir(parents=True,exist_ok=True)
    handler=RotatingFileHandler(directory/(role+'.application.log'),maxBytes=10*1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    root=logging.getLogger()
    for current in list(root.handlers):root.removeHandler(current);current.close()
    root.addHandler(handler);root.setLevel(logging.INFO)
    for name in ('uvicorn','uvicorn.error','uvicorn.access'):
        logger=logging.getLogger(name);logger.handlers.clear();logger.propagate=True
    logging.getLogger('uvicorn.access').disabled=True
