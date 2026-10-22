import hashlib
import json

import pytest

from scripts.check_docs import check, resolve_archived_document


def fixture(tmp_path):
    docs=tmp_path/'docs';docs.mkdir()
    archive=docs/'archive';archive.mkdir()
    old=archive/'old.md';old.write_text('# frozen\n',encoding='utf-8')
    digest=hashlib.sha256(old.read_bytes()).hexdigest()
    (archive/'manifest.json').write_text(json.dumps({'schema_version':'docqa-doc-archive-v1','files':[
        {'original':'docs/old.md','archived':'docs/archive/old.md','sha256':digest}]}),encoding='utf-8')
    (docs/'README.md').write_text('[历史](archive/old.md)\n',encoding='utf-8')
    return old,digest


def test_archived_original_is_byte_verified_and_old_freeze_path_resolves(tmp_path):
    old,digest=fixture(tmp_path)
    freeze=tmp_path/'freeze.json';freeze.write_text(json.dumps({'hashes':{'docs/old.md':digest}}),encoding='utf-8')
    report=check(tmp_path,[freeze])
    assert report['passed'] and report['archives_verified']==report['frozen_document_refs_verified']==1
    assert resolve_archived_document(tmp_path,'docs/old.md')==old


def test_tampered_frozen_archive_does_not_pass_link_only_checks(tmp_path):
    old,_=fixture(tmp_path);old.write_text('changed',encoding='utf-8')
    report=check(tmp_path)
    assert not report['passed'] and report['issues'][0]['kind']=='archive_hash_mismatch'


def test_broken_current_link_is_reported(tmp_path):
    fixture(tmp_path)
    (tmp_path/'docs/README.md').write_text('[missing](missing.md)',encoding='utf-8')
    assert any(i['kind']=='broken_link' for i in check(tmp_path)['issues'])


def test_git_ignored_evidence_is_reported_separately(tmp_path):
    fixture(tmp_path)
    (tmp_path/'docs/README.md').write_text('[evidence](../data/missing.json)',encoding='utf-8')
    result=check(tmp_path)
    assert result['passed'] and len(result['deferred_local_evidence'])==1


def test_archive_manifest_cannot_escape_project_root(tmp_path):
    fixture(tmp_path)
    manifest=tmp_path/'docs/archive/manifest.json'
    content=json.loads(manifest.read_text());content['files'][0]['archived']='../outside.md'
    manifest.write_text(json.dumps(content),encoding='utf-8')
    with pytest.raises(ValueError,match='escapes'):
        check(tmp_path)


def test_external_links_and_fenced_examples_do_not_trigger_network_or_false_failures(tmp_path):
    fixture(tmp_path)
    (tmp_path/'docs/README.md').write_text('[web](https://example.invalid)\n```md\n[x](missing.md)\n```\n',encoding='utf-8')
    assert check(tmp_path)['passed']


def test_deployment_links_are_checked_after_document_relocation(tmp_path):
    fixture(tmp_path)
    deploy=tmp_path/'deploy/docling';deploy.mkdir(parents=True)
    (deploy/'README.md').write_text('[obsolete](../../docs/old-location.md)',encoding='utf-8')
    assert any(i['kind']=='broken_link' and 'deploy' in i['path'] for i in check(tmp_path)['issues'])


def test_stale_readme_version_is_caught_without_importing_application(tmp_path):
    fixture(tmp_path)
    app = tmp_path/'app'; app.mkdir()
    (app/'migrations.py').write_text('SCHEMA_VERSION = 13\nraise RuntimeError("must not import")\n', encoding='utf-8')
    readme = tmp_path/'README.md'
    readme.write_text('| 数据库 schema | `5` |\n', encoding='utf-8')
    result = check(tmp_path)
    assert not result['passed']
    assert any(i['kind']=='readme_version_mismatch' and i['expected']=='13' for i in result['issues'])
    readme.write_text('| 数据库 schema | `13` |\n', encoding='utf-8')
    assert check(tmp_path)['passed']


def test_missing_or_changed_example_defaults_fail_without_echoing_values(tmp_path):
    fixture(tmp_path)
    app = tmp_path/'app'; app.mkdir()
    (app/'config.py').write_text('value("DOCQA_TEAM_MODE", "0")\nvalue("DEEPSEEK_API_KEY", "")\n', encoding='utf-8')
    example = tmp_path/'.env.example'
    example.write_text('DEEPSEEK_API_KEY=private-value-must-not-echo\n', encoding='utf-8')
    result = check(tmp_path)
    assert not result['passed'] and result['config_defaults_checked']==2
    assert 'private-value-must-not-echo' not in json.dumps(result)
    example.write_text('DOCQA_TEAM_MODE=0\nDEEPSEEK_API_KEY=\n', encoding='utf-8')
    assert check(tmp_path)['passed']


def test_dependency_documents_are_pruned_but_project_fixture_docs_are_checked(tmp_path):
    fixture(tmp_path)
    own = tmp_path/'tests/fixtures/sample'; own.mkdir(parents=True)
    (own/'README.md').write_text('[project](../../../docs/README.md)', encoding='utf8')
    vendor = own/'node_modules/pkg'; vendor.mkdir(parents=True)
    (vendor/'README.md').write_text('[vendor-only](../../../../outside.md)', encoding='utf8')
    result = check(tmp_path)
    assert result['passed'] and result['documents']==3
    (own/'README.md').write_text('[missing](missing.md)', encoding='utf8')
    assert not check(tmp_path)['passed']
