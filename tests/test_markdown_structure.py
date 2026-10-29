"""Markdown结构分块保持行语义、代码文本和独立来源，不使用私有文档。"""
from pathlib import Path
import pytest
from app.markdown_document import markdown_spans,markdown_row
from app.document_normalizer import NormalizedDocument
from app.chunking import chunk_document,ChunkingConfig
from app.schemas import Block,SourceLocation
from tests.test_product_access import team


def document(text):
    blocks=[]
    for i,(a,b,kind) in enumerate(markdown_spans(text)):
        blocks.append(Block(id=str(i),document_id='d',parse_version_id='v',order_index=i,
            block_type='paragraph' if kind=='text' else kind,text=text[a:b],heading_path='手册 / 检查',
            sources=[SourceLocation(format='md',line_start=text[:a].count('\n')+1,
                                    line_end=text[:b].rstrip('\r\n').count('\n')+1,char_start=a,char_end=b)]))
    return NormalizedDocument(document_id='d',format='md',page_count=1,blocks=blocks)


@pytest.mark.parametrize('newline',['\n','\r\n'])
def test_table_rows_keep_headers_and_original_disjoint_locations(newline):
    text=newline.join(['# 检查','', '| 问题 | 处理 |', '|---|---|',
                       '| 名称冲突 | 核对版本 |','| 请求失败 | 保留旧结果 |','| 权限撤回 | 停止输出 |',''])
    doc=document(text);before=[b.model_dump() for b in doc.blocks]
    chunks=chunk_document(doc,ChunkingConfig(max_chars=200,overlap_chars=20))
    rows=[c for c in chunks if c.chunk_type=='table_rows'];assert len(rows)==3
    for chunk in rows:
        assert '| 问题 | 处理 |' in chunk.text and len(chunk.sources)==2
        reconstructed=''.join(text[s.char_start:s.char_end] for s in chunk.sources)
        assert reconstructed.strip()==chunk.text
        assert chunk.heading_path=='手册 / 检查'
    assert [b.model_dump() for b in doc.blocks]==before


def test_inline_pipes_and_escaped_pipes_do_not_split_cells():
    assert markdown_row(r'| `a|b` | c\|d |')==['`a|b`',r'c\|d']
    text='| 命令 | 含义 |\n|---|---|\n| `a|b` | c\\|d |\n'
    assert markdown_spans(text)==[(0,len(text),'markdown_table')]


@pytest.mark.parametrize('heading,title',[
    ('# 客户端 C#\n','客户端 C#'),
    ('# 标识###\n','标识###'),
    ('# 标识 ###\n','标识'),
    ('  ## 子题F# ##\r\n','子题F#'),
])
def test_local_markdown_heading_preserves_literal_hashes_and_only_removes_separated_closing_marks(team,heading,title):
    env,_,clients=team;admin=clients['admin'];text=heading+'\n说明原文。\n'
    uploaded=admin.post('/api/documents',files={'file':('hash-title.md',text.encode(),'text/markdown')})
    assert uploaded.status_code==201;identifier=uploaded.json()['document']['id']
    response=admin.post('/api/documents/'+identifier+'/parse',json={'idempotency_key':'literal-heading'})
    from app.parse_worker import ParseWorker
    assert ParseWorker(env.settings,env.repository).run_forever(max_tasks=1)==1
    task=env.repository.get_task(response.json()['task']['id']);assert task.status=='succeeded'
    version=env.repository.get(identifier).active_parse_version_id
    blocks=env.repository.blocks(version)
    assert blocks[0].text==heading and all(b.heading_path==title for b in blocks)
    assert all(s.section_path==title for b in blocks for s in b.sources)
    assert all(c.heading_path==title for c in env.repository.chunks(identifier,version))
    assert not env.embedding.calls and not env.model.calls


def test_long_code_is_split_only_on_lines_and_not_merged_with_prose():
    text='## 示例\n\n说明文字。\n\n```python\n# 这里不是标题\n'+''.join('    value_'+str(i)+' = "text"\n' for i in range(20))+'```\n\n后续说明。\n'
    doc=document(text);chunks=chunk_document(doc,ChunkingConfig(max_chars=160,overlap_chars=20))
    code=[c for c in chunks if c.chunk_type=='code'];assert len(code)>1
    assert sum(b.block_type=='section_header' for b in doc.blocks)==1
    original=next(b.text for b in doc.blocks if b.block_type=='code')
    assert ''.join(c.text for c in code)==original
    assert all('说明文字' not in c.text and '后续说明' not in c.text and len(c.text)<=160 for c in code)
    assert any(c.text.startswith('    value_') for c in code)
    for chunk in code:
        source=chunk.sources[0];assert text[source.char_start:source.char_end]==chunk.text


def test_table_and_code_single_line_overflow_is_explicit_not_truncated():
    for text in ['| A | B |\n|---|---|\n| '+('x'*250)+' | value |\n','```\n'+('x'*250)+'\n```\n',
                 '| '+('x'*250)+' | B |\n|---|---|\n']:
        doc=document(text);before=[b.model_dump() for b in doc.blocks]
        with pytest.raises(ValueError,match='超过分块上限'):chunk_document(doc,ChunkingConfig(max_chars=200,overlap_chars=20))
        assert [b.model_dump() for b in doc.blocks]==before


def test_unclosed_fence_is_preserved_as_code_not_a_fake_heading():
    text='# 根标题\n\n~~~text\n# 字面量\n| a | b |\n|---|---|\n'
    spans=markdown_spans(text);assert [s[2] for s in spans]==['section_header','code']
    assert text[spans[1][0]:spans[1][1]]=='~~~text\n# 字面量\n| a | b |\n|---|---|\n'


def test_real_local_parse_preserves_heading_hierarchy_and_does_not_execute_code(team,tmp_path):
    env,users,clients=team;admin=clients['admin'];sentinel=tmp_path/'must-not-exist'
    base=admin.post('/api/knowledge-bases',json={'name':'结构手册','chunk_max_chars':400,'chunk_overlap_chars':40}).json()
    text='# 网关手册\n\n## 错误处理\n\n| 问题 | 后果 | 处理 |\n|---|---|---|\n| 名称冲突 | 无法对应 | 核对版本 |\n| 超时 | 状态未知 | 不重发 |\n\n```python\n# 不是标题\nfrom pathlib import Path\nPath('+repr(str(sentinel))+').write_text("executed")\n```\n'
    result=admin.post('/api/admin/knowledge/bases/'+base['id']+'/documents',files={'file':('guide.md',text.encode(),'text/markdown')},headers={'Idempotency-Key':'structured-md'})
    assert result.status_code==201;identifier=result.json()['document']['id']
    assert admin.post('/api/documents/'+identifier+'/parse',json={'idempotency_key':'parse-md'}).status_code==202
    from app.parse_worker import ParseWorker
    assert ParseWorker(env.settings,env.repository).run_forever(max_tasks=1)==1
    item=env.repository.get(identifier);assert item.status=='parsed'
    blocks=env.repository.blocks(item.active_parse_version_id);chunks=env.repository.chunks(identifier,item.active_parse_version_id)
    assert any(b.block_type=='markdown_table' for b in blocks)
    assert all(c.heading_path=='网关手册 / 错误处理' for c in chunks if c.chunk_type in ('code','table_rows'))
    assert len([c for c in chunks if c.chunk_type=='table_rows'])==2 and not sentinel.exists()
    assert not env.embedding.calls and not env.model.calls
    assert not any(w.code=='txt_logical_page' for w in env.repository.get_parse_version(item.active_parse_version_id).warnings)


def test_markdown_table_snapshot_includes_raw_layout_and_uses_raw_fallback():
    from app.rag_table_projection import table_projection,table_structure_signature
    doc=document('| 项目 | 值 |\n|---|---|\n| 缓存 | 保留 |\n| 权限 | 复核 |\n')
    chunk=chunk_document(doc,ChunkingConfig())[1]
    chunk.parse_version_id='v';block=doc.blocks[0]
    assert table_projection(chunk,block)['reason']=='markdown_raw_rows'
    before=table_structure_signature(doc.blocks);block.sources[0].line_start=99
    assert table_structure_signature(doc.blocks)!=before


def test_parse_overflow_keeps_old_version_and_chunks(team):
    env,_,clients=team;admin=clients['admin']
    text='| 项目 | 说明 |\n|---|---|\n| 值 | '+('abcd'*100)+' |\n'
    result=admin.post('/api/documents',files={'file':('long-row.md',text.encode(),'text/markdown')})
    assert result.status_code==201;identifier=result.json()['document']['id']
    from app.parse_worker import ParseWorker
    worker=ParseWorker(env.settings,env.repository)
    assert admin.post('/api/documents/'+identifier+'/parse',json={'idempotency_key':'first'}).status_code==202
    assert worker.run_forever(max_tasks=1)==1
    original=env.repository.get(identifier).active_parse_version_id
    assert original;chunks=[c.model_dump() for c in env.repository.chunks(identifier,original)]
    from dataclasses import replace
    worker=ParseWorker(replace(env.settings,chunk_max_chars=200,chunk_overlap_chars=20),env.repository)
    result=admin.post('/api/documents/'+identifier+'/parse',json={'idempotency_key':'second','force':True})
    assert result.status_code==202
    assert worker.run_forever(max_tasks=1)==1
    task=env.repository.get_task(result.json()['task']['id'])
    assert task.status=='failed' and task.error_code=='chunking_invalid'
    assert '超过分块上限' in task.error_message
    assert env.repository.get(identifier).active_parse_version_id==original
    assert [c.model_dump() for c in env.repository.chunks(identifier,original)]==chunks
    assert not env.embedding.calls and not env.model.calls


@pytest.mark.parametrize('format',['md','txt'])
def test_markdown_policy_identity_creates_new_version_without_changing_other_formats(team,monkeypatch,format):
    import app.markdown_document as markdown
    from app.parse_worker import ParseWorker
    env,_,clients=team;admin=clients['admin'];raw='相同原文，策略升级不能修改旧解析记录。\n'.encode()
    response=admin.post('/api/documents',files={'file':('same-source.'+format,raw)})
    identifier=response.json()['document']['id'];worker=ParseWorker(env.settings,env.repository)
    versions=[];snapshots=[]
    for number,policy in enumerate(('isolated-md-policy-A','isolated-md-policy-B','isolated-md-policy-B')):
        monkeypatch.setattr(markdown,'MARKDOWN_PARSER_VERSION',policy)
        task=admin.post('/api/documents/'+identifier+'/parse',json={'idempotency_key':'policy-'+str(number),'force':bool(number)})
        assert task.status_code==202 and worker.run_forever(max_tasks=1)==1
        version=env.repository.get(identifier).active_parse_version_id;versions.append(version)
        snapshots.append(env.repository.get_parse_version(version).model_dump())
    assert (versions[0]!=versions[1]) is (format=='md')
    assert versions[1]==versions[2]
    assert env.repository.get_parse_version(versions[0]).model_dump()==snapshots[0]
    assert len(env.repository.list_parse_versions(identifier))==(2 if format=='md' else 1)
    assert not env.embedding.calls and not env.model.calls
