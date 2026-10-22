"""持久化意图树；每次读取数据库，不用编辑无关节点来刷新缓存。"""
import json
import re
from uuid import uuid4
from fastapi import HTTPException
from app.repository import now_iso
from app.knowledge_tasks import dump


from app.intent_routing import normalized, decide


class IntentManagement:
    def __init__(self,repository):self.repository=repository

    def list(self):
        with self.repository.connect() as db:rows=[dict(r) for r in db.execute('SELECT * FROM intent_nodes ORDER BY sort_order,created_at,id')]
        mapping={r['id']:r for r in rows}
        for row in rows:
            row['examples']=json.loads(row.pop('examples_json'));row['enabled']=bool(row['enabled'])
            row['keywords']=json.loads(row.pop('keywords_json','[]'))
            parents=[];parent=row['parent_id'];seen={row['id']}
            while parent and parent in mapping and parent not in seen:
                seen.add(parent);parents.insert(0,mapping[parent]['name']);parent=mapping[parent]['parent_id']
            row['level']=len(parents);row['path']=' / '.join(parents+[row['name']])
        return rows

    def save(self,body,identifier=None):
        value=dict(body);editing=identifier is not None;identifier=identifier or 'intent-'+uuid4().hex;stamp=now_iso()
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,63}',value['code']):raise HTTPException(422,'意图编码使用小写字母、数字和连字符，以字母开头')
        if value['kind']=='KB' and not value.get('base_id'):raise HTTPException(422,'知识问答意图需要关联知识库')
        if value['kind']=='MCP' and not value.get('tool_id'):raise HTTPException(422,'工具意图需要关联已注册工具')
        mode=value.get('match_mode','exact'); family=value.get('interaction_kind'); keywords=value.get('keywords',[])
        if mode not in ('exact','interaction','keywords') or family not in (None,'welcome','about','thanks','correction'):
            raise HTTPException(422,'匹配策略无效')
        if mode=='interaction' and (value['kind']!='SYSTEM' or not family):
            raise HTTPException(422,'纯交互匹配须选择SYSTEM类型及交互类别')
        if mode=='keywords' and (value['kind']!='KB' or not keywords):
            raise HTTPException(422,'关键词匹配仅适用于知识问答，并须填写关键词')
        if not isinstance(keywords,list) or len(keywords)>20 or any(not isinstance(w,str) or not 2<=len(normalized(w))<=30 for w in keywords):
            raise HTTPException(422,'关键词最多20个，每个归一化后2～30字符')
        if len({normalized(w) for w in keywords})!=len(keywords):raise HTTPException(422,'关键词不可重复')
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM intent_nodes WHERE id=?',(identifier,)).fetchone()
            if editing and not existing:raise HTTPException(404,'意图节点不存在')
            if existing and existing['revision']!=value.get('expected_revision'):raise HTTPException(409,'意图已变更，请刷新后编辑')
            if db.execute('SELECT 1 FROM intent_nodes WHERE code=? AND id!=?',(value['code'],identifier)).fetchone():raise HTTPException(409,'意图编码已存在')
            parent=value.get('parent_id');seen={identifier};depth=0
            while parent:
                if parent in seen:raise HTTPException(422,'不能形成循环父子关系')
                seen.add(parent);row=db.execute('SELECT * FROM intent_nodes WHERE id=?',(parent,)).fetchone()
                if not row:raise HTTPException(422,'父节点不存在')
                if row['kind']!=value['kind']:raise HTTPException(422,'父子节点类型必须一致')
                parent=row['parent_id'];depth+=1
                if depth>=6:raise HTTPException(422,'意图树最多6层')
            descendants=db.execute('WITH RECURSIVE children(id,depth) AS (SELECT id,1 FROM intent_nodes WHERE parent_id=? UNION ALL SELECT n.id,c.depth+1 FROM intent_nodes n JOIN children c ON n.parent_id=c.id) SELECT COALESCE(MAX(depth),0) FROM children',(identifier,)).fetchone()[0]
            if depth+descendants>=6:raise HTTPException(422,'移动后子树将超过6层')
            if db.execute('SELECT 1 FROM intent_nodes WHERE parent_id=? AND kind!=?',(identifier,value['kind'])).fetchone():raise HTTPException(422,'请先调整子节点类型')
            if value.get('base_id') and not db.execute('SELECT 1 FROM knowledge_bases WHERE id=?',(value['base_id'],)).fetchone():raise HTTPException(422,'关联知识库不存在')
            if value['kind']=='MCP':
                if not value.get('base_id'):raise HTTPException(422,'工具意图须关联知识库，以沿用成员授权')
                if not db.execute('SELECT 1 FROM mcp_tools WHERE id=?',(value.get('tool_id'),)).fetchone():raise HTTPException(422,'请先登记MCP工具')
            if any(not isinstance(s,str) or not s.strip() or len(s)>500 for s in value.get('examples',[])):raise HTTPException(422,'示例问题须为1～500字符')
            if value['kind']=='SYSTEM' and value['enabled'] and value.get('examples') and not value.get('response_text','').strip():raise HTTPException(422,'系统交互节点需要回复内容')
            params=(value['code'],value['name'].strip(),value.get('parent_id'),value['kind'],value.get('base_id'),value.get('tool_id'),value.get('description',''),dump(value.get('examples',[])),value.get('response_text',''),value.get('top_k'),value.get('sort_order',0),int(value.get('enabled',True)))
            if existing:
                db.execute('UPDATE intent_nodes SET code=?,name=?,parent_id=?,kind=?,base_id=?,tool_id=?,description=?,examples_json=?,response_text=?,top_k=?,sort_order=?,enabled=?,revision=revision+1,updated_at=? WHERE id=?',params+(stamp,identifier))
            else:db.execute('INSERT INTO intent_nodes(code,name,parent_id,kind,base_id,tool_id,description,examples_json,response_text,top_k,sort_order,enabled,created_at,updated_at,id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',params+(stamp,stamp,identifier))
            db.execute('UPDATE intent_nodes SET match_mode=?,interaction_kind=?,keywords_json=? WHERE id=?', (mode,family,dump(keywords),identifier))
        return next(r for r in self.list() if r['id']==identifier)

    def delete(self,identifier,revision):
        with self.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM intent_nodes WHERE parent_id=?',(identifier,)).fetchone():raise HTTPException(409,'请先处理子节点')
            if not db.execute('DELETE FROM intent_nodes WHERE id=? AND revision=?',(identifier,revision)).rowcount:raise HTTPException(409,'节点已变更或不存在')

    def evaluate(self,question,base_id=None,scope='knowledge'):
        route,decision=decide(question,base_id,self.list(),scope)
        return {'route':route,'decision':decision}

    def resolve(self,question,base_id,scope='knowledge'):
        return self.evaluate(question,base_id,scope)['route']
