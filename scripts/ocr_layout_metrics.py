"""整页OCR/阅读顺序/表格分别计分；只用于离线评估，不参与运行时选框。"""
from __future__ import annotations
import hashlib,json,statistics
from pathlib import Path
try:
    from scripts.ocr_quality_metrics import edit_counts,text_characters,contains_critical_span
except ModuleNotFoundError:
    from ocr_quality_metrics import edit_counts,text_characters,contains_critical_span


def intersection(a,b):
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def area(box):return max(0,box[2]-box[0])*max(0,box[3]-box[1])


def iou(a,b):
    both=intersection(a,b);union=area(a)+area(b)-both
    return both/union if union else 0


def center_in(a,b):
    x,y=(a[0]+a[2])/2,(a[1]+a[3])/2
    return b[0]<=x<=b[2] and b[1]<=y<=b[3]


def region_text(lines,box):
    chosen=[line for line in lines if center_in(line['bbox'],box)]
    # 只为字符度量组装物理行。行内轻微倾斜不应使Date/日期值颠倒；
    # 这不是Docling阅读顺序评分，阅读顺序使用真实输出树的order。
    chosen.sort(key=lambda r:(r['bbox'][1]+r['bbox'][3])/2)
    rows=[]
    for line in chosen:
        cy=(line['bbox'][1]+line['bbox'][3])/2;height=line['bbox'][3]-line['bbox'][1]
        if rows and abs(cy-rows[-1]['cy'])<=max(height,rows[-1]['height'])*.5:
            rows[-1]['lines'].append(line)
        else:rows.append({'cy':cy,'height':height,'lines':[line]})
    return '\n'.join(' '.join(r['text'] for r in sorted(row['lines'],key=lambda r:r['bbox'][0])) for row in rows)


def order_location(region,prediction):
    matches=[u for u in prediction['units'] if u['kind']=='text' and u.get('bbox') and center_in(u['bbox'],region['bbox'])]
    if not matches:
        matches=[u for u in prediction['units'] if u['kind']=='text' and u.get('bbox') and
                 intersection(u['bbox'],region['bbox'])/max(area(region['bbox']),1e-12)>=.5]
    if not matches:return None
    indices=[u['order'] for u in matches]
    anchors=[]
    for unit in matches:
        offset=text_characters(unit['text']).find(text_characters(region['order_anchor']))
        if offset>=0:anchors.append((unit['order'],offset))
    return {'first':min(indices),'last':max(indices),'anchors':anchors}


def score_page(gold:dict,prediction:dict|None)->dict:
    prediction=prediction or {'ocr_lines':[],'units':[],'tables':[],'status':'missing'}
    regions=[];locations={}
    for region in gold['regions']:
        actual=region_text(prediction['ocr_lines'],region['bbox'])
        checks=[{'span':span,'found':contains_critical_span(actual,span)} for span in region['critical_spans']]
        regions.append({'id':region['id'],**edit_counts(region['expected'],actual),'actual':actual,'critical':checks})
        locations[region['id']]=order_location(region,prediction)
    table_reports=[];used_tables=set()
    for table in gold['tables']:
        candidates=[(iou(table['bbox'],p['bbox']),p) for p in prediction['tables'] if p['id'] not in used_tables and p.get('bbox')]
        overlap,actual=max(candidates,key=lambda pair:pair[0]) if candidates else (0,None)
        if overlap<.3:actual=None
        if actual:used_tables.add(actual['id'])
        cells=[]
        for cell in table['cells']:
            key=tuple(cell[k] for k in ['r0','r1','c0','c1'])
            choices=[c for c in actual['cells'] if tuple(c[k] for k in ['r0','r1','c0','c1'])==key] if actual else []
            predicted=choices[0] if len(choices)==1 else None
            text_ok=predicted is not None and text_characters(predicted['text'])==text_characters(cell['text'])
            header_ok=predicted is not None and bool(predicted['column_header'])==bool(cell['column_header'])
            cells.append({'r0':cell['r0'],'r1':cell['r1'],'c0':cell['c0'],'c1':cell['c1'],
                          'expected':cell['text'],'actual':predicted['text'] if predicted else None,
                          'text_and_grid_correct':text_ok,'header_correct':header_ok,'correct':text_ok and header_ok,
                          'blank':not text_characters(cell['text'])})
        order=actual['order'] if actual else None
        locations['table:'+table['id']]={'first':order,'last':order,'anchors':[]} if order is not None else None
        gold_keys={tuple(c[k] for k in ['r0','r1','c0','c1']) for c in table['cells']}
        extras=sum(tuple(c[k] for k in ['r0','r1','c0','c1']) not in gold_keys for c in actual['cells']) if actual else 0
        table_reports.append({'id':table['id'],'matched':bool(actual),'iou':overlap,'cells':cells,'extra_cells':extras,
                              'shape_correct':bool(actual) and actual['rows']==table['rows'] and actual['cols']==table['cols']})
    edges=[]
    for a,b in gold['order_edges']:
        left,right=locations[a],locations[b];correct=False
        if left and right:
            correct=left['last']<right['first']
            if not correct:
                correct=any(x[0]==y[0] and x[1]<y[1] for x in left['anchors'] for y in right['anchors'])
        edges.append({'before':a,'after':b,'correct':correct,'missing':not left or not right})
    return {'id':gold['id'],'source_kind':gold['source_kind'],'split':gold['split'],'regions':regions,'edges':edges,
            'tables':table_reports,'unmatched_predicted_tables':len(prediction['tables'])-len(used_tables),
            'status':prediction.get('status'),'review_required':prediction.get('review_required',False)}


def aggregate(pages):
    regions=[r for p in pages for r in p['regions']];edges=[r for p in pages for r in p['edges']]
    cells=[c for p in pages for t in p['tables'] for c in t['cells']]
    critical=[c for r in regions for c in r['critical']]
    chars=sum(r['reference_characters'] for r in regions)
    return {'pages':len(pages),'annotated_regions':len(regions),'characters':chars,'edits':sum(r['edits'] for r in regions),
            'cer':sum(r['edits'] for r in regions)/chars if chars else None,
            'critical_found':sum(c['found'] for c in critical),'critical_total':len(critical),
            'critical_recall':sum(c['found'] for c in critical)/len(critical) if critical else None,
            'order_correct':sum(e['correct'] for e in edges),'order_total':len(edges),
            'order_accuracy':sum(e['correct'] for e in edges)/len(edges) if edges else None,
            'table_cells_correct':sum(c['correct'] for c in cells),'table_cells_total':len(cells),
            'table_accuracy':sum(c['correct'] for c in cells)/len(cells) if cells else None,
            'table_text_and_grid_correct':sum(c['text_and_grid_correct'] for c in cells),
            'blank_cells_correct':sum(c['correct'] for c in cells if c['blank']),'blank_cells_total':sum(c['blank'] for c in cells),
            'review_required_pages':sum(p['review_required'] for p in pages)}


def load_gold(root:Path):
    freeze=json.loads((root/'freeze.json').read_text(encoding='utf-8'))
    if hashlib.sha256((root/'gold.json').read_bytes()).hexdigest()!=freeze['gold_sha256']:raise ValueError('gold在冻结后发生变化')
    gold=json.loads((root/'gold.json').read_text(encoding='utf-8'))
    families={}
    for p in gold['pages']:
        if not p['reviewed']:raise ValueError('标注未复核')
        if p['document_id'] in families and families[p['document_id']]!=p['split']:raise ValueError('同源跨分组')
        families[p['document_id']]=p['split']
        identifiers={r['id'] for r in p['regions']}|{'table:'+t['id'] for t in p['tables']}
        if any(a not in identifiers or b not in identifiers or a==b for a,b in p['order_edges']):raise ValueError('顺序边无效')
    return gold
