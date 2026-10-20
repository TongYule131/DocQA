import copy
import hashlib
import json

import pytest

from scripts.ocr_quality_metrics import contains_critical_span, edit_counts, load_corpus, score_predictions,promotion_gate


def test_cer_counts_insertion_deletion_and_substitution_without_rewriting():
    assert edit_counts('采样规则；勘误单', '采样规则；甚勘误单')['insertions'] == 1
    assert edit_counts('不能公开', '能公开')['deletions'] == 1
    assert edit_counts('14.3%', '14.8%')['substitutions'] == 1
    assert edit_counts('4分（5分）', '4 分(5 分)')['edits'] == 2
    assert edit_counts('甲', '乙乙乙')['cer'] == 3
    assert edit_counts('', '噪声')['cer'] is None
    assert edit_counts('甲\r\n乙', '甲 乙')['cer'] == 0


@pytest.mark.parametrize('actual,span,expected', [
    ('125%', '25%', False), ('-25%', '25%', False), ('14.30%', '14.3%', False),
    ('11.3', '1.3', False), ('1.30', '1.3', False), ('从25%提升', '25%', True),
    ('各1\n张', '各1张', True), ('15张以上', '5张以上', False),
    ('不予公开', '不予公开', True), ('予以公开', '不予公开', False),
    ('万吨 2277.9 1.3', '2277.9', True), ('2277.91.3', '2277.9', False),
])
def test_critical_numeric_boundaries(actual, span, expected):
    assert contains_critical_span(actual, span) == expected


def corpus(tmp_path):
    image=tmp_path/'region.png'; image.write_bytes(b'fixed image')
    row={'id':'one','document_id':'doc','split':'dev','image':'region.png',
         'image_sha256':hashlib.sha256(image.read_bytes()).hexdigest(),
         'expected':'不能公开25%','critical_spans':['不能公开','25%'],
         'category':'normal','source_kind':'synthetic','reviewed':True}
    return {'schema_version':'docqa-ocr-corpus-v1','frozen':True,'regions':[row]}


def test_missing_and_failed_outputs_remain_in_denominator(tmp_path):
    data=corpus(tmp_path)
    score=score_predictions(data,[])
    assert score['overall']['cer'] == 1
    assert score['overall']['critical_recall'] == 0
    assert score['overall']['failures'] == 1
    score=score_predictions(data,[{'id':'one','text':'不能公开25%','error':'inference_failed'}])
    assert score['overall']['exact_regions'] == 0
    assert score['overall']['critical_recall'] == 0
    assert score['reading_order']['status'] == 'not_evaluated'
    with pytest.raises(ValueError): score_predictions(data,[{'id':'unknown','text':'anything'}])


@pytest.mark.parametrize('corrupt', ['image','unreviewed','family','critical','escape','duplicate'])
def test_manifest_binding_and_split_leakage(tmp_path,corrupt):
    data=corpus(tmp_path)
    if corrupt=='image':data['regions'][0]['image_sha256']='0'*64
    if corrupt=='unreviewed':data['regions'][0]['reviewed']=False
    if corrupt=='critical':data['regions'][0]['critical_spans']=['125%']
    if corrupt=='escape':data['regions'][0]['image']='../foreign.png'
    if corrupt in {'family','duplicate'}:
        row=copy.deepcopy(data['regions'][0]); row['split']='holdout'
        if corrupt=='family':row['id']='two'
        data['regions'].append(row)
    (tmp_path/'corpus.json').write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises((ValueError,FileNotFoundError)):load_corpus(tmp_path)


def test_promotion_requires_quality_and_resources_without_loosening_gold(tmp_path):
    data=corpus(tmp_path)
    second=copy.deepcopy(data['regions'][0]);second.update(id='two',category='difficult')
    data['regions'].append(second)
    base=score_predictions(data,[{'id':'one','text':'不能公开125%'},{'id':'two','text':'不能公开25%'}])
    good=score_predictions(data,[{'id':'one','text':'不能公开25%'},{'id':'two','text':'不能公开25%'}])
    run={'split':'holdout','corpus_sha256':'fixed','gpu':{'peak_bytes':1024**3},'completed':True,
         'predictions':[{'elapsed_seconds':1},{'elapsed_seconds':1}]}
    assert promotion_gate(base,good,run,run)['accepted_for_default']
    wrong=copy.deepcopy(run);wrong['gpu']['peak_bytes']=8*1024**3
    assert not promotion_gate(base,good,run,wrong)['accepted_for_default']
    gate=promotion_gate(good,base,run,run)
    assert not gate['accepted_for_default']
    assert gate['new_missing_critical']==[{'id':'one','span':'25%'}]
    wrong=copy.deepcopy(run);wrong['split']='dev'
    with pytest.raises(ValueError):promotion_gate(base,good,run,wrong)
