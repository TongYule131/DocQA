"""冻结整页关卡：原始/候选报告比较，不访问模型或修改gold。"""
from __future__ import annotations
import statistics


def annotation_identity(report):
    identities = {}
    for page in report['pages']:
        if page['id'] in identities:
            raise ValueError('评估标注页重复')
        regions = [(r['id'], r.get('reference_characters'), tuple(c['span'] for c in r.get('critical', [])))
                   for r in page['regions']]
        tables = [(t['id'], tuple((c['r0'], c['r1'], c['c0'], c['c1'], c['expected'], c['blank'])
                                for c in t['cells'])) for t in page.get('tables', [])]
        edges = [(e['before'], e['after']) for e in page.get('edges', [])]
        identities[page['id']] = (page.get('source_kind'), regions, tables, edges)
    if not identities:
        raise ValueError('评估标注范围为空')
    return identities


def missing(report):
    return {(p['id'],r['id'],c['span']) for p in report['pages'] for r in p['regions'] for c in r['critical'] if not c['found']}


def assess(baseline,candidate,local_summary,routed_summary):
    if baseline['split']!='holdout' or candidate['split']!='holdout':raise ValueError('必须使用保留集')
    if {p['id'] for p in baseline['pages']}!={p['id'] for p in candidate['pages']}:raise ValueError('评估页范围不同')
    if annotation_identity(baseline) != annotation_identity(candidate):
        raise ValueError('评估标注范围不同，不能比较分数')
    base,cand=baseline['overall'],candidate['overall']
    relative=(base['cer']-cand['cer'])/base['cer'] if base['cer'] else 0.
    summaries=[baseline['inference_summary'],local_summary,routed_summary]
    ids={row['id'] for row in summaries[0]['pages']}
    if ids != {p['id'] for p in baseline['pages']} or any(
            len(s['pages']) != len(ids) or {r['id'] for r in s['pages']} != ids for s in summaries):
        raise ValueError('推理页不完整')
    times=[{p['id']:p['elapsed_seconds'] for p in s['pages']} for s in summaries]
    latency_ratio=statistics.median(sum(t[identity] for t in times) for identity in ids)/statistics.median(times[0].values())
    peak=max(s['gpu']['peak_bytes'] for s in summaries)/1024**3
    new_errors=sorted(missing(candidate)-missing(baseline))
    by=candidate['by_source_kind']
    checks={
        'typeset_cer_le_2pct':by['real_typeset_rasterized']['cer']<=.02,
        'real_scan_cer_le_5pct':by['real_low_resolution_scan']['cer']<=.05,
        'cer_relative_improvement_ge_20pct':relative>=.2 if base['cer'] else cand['cer']==0,
        'critical_recall_ge_99pct':cand['critical_recall']>=.99,
        'no_new_critical_misses':not new_errors,
        'reading_order_ge_98pct':cand['order_accuracy']>=.98,
        'reading_order_no_regression':cand['order_correct']>=base['order_correct'],
        'table_cells_ge_98pct':cand['table_accuracy']>=.98,
        'table_cells_no_regression':cand['table_cells_correct']>=base['table_cells_correct'],
        'gpu_device_peak_le_6_5gib':peak<=6.5,
        'actual_staged_median_latency_le_2x':latency_ratio<=2,
        'no_pending_text_disagreements':cand['review_required_pages']==0,
        'all_inference_complete':all(s['completed'] for s in summaries),
    }
    return {'accepted_for_default':all(checks.values()),'checks':checks,'relative_cer_improvement':relative,
            'actual_staged_median_latency_ratio':latency_ratio,'gpu_device_peak_gib':peak,
            'new_critical_misses':[{'page':p,'region':r,'span':s} for p,r,s in new_errors],
            'scope':'visible_holdout_annotated_regions_edges_and_cells_not_global_accuracy'}
