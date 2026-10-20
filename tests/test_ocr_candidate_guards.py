from scripts.ocr_candidate_guards import guard_reason,numeric_signature


def record(old,new,kind='text'):
    return {'original_text':old,'candidate_text':new,'kind':kind,'changed':old!=new}


def test_high_confidence_numeric_disagreement_still_requires_review():
    assert guard_reason(record('电话 (919)','电话 (616)'))=='numeric_disagreement_requires_review'
    assert guard_reason(record('55.6','5\n5\n6'))=='numeric_disagreement_requires_review'
    assert numeric_signature('27,745.86 万元 25%')==['27,745.86','25%']


def test_table_labels_and_blank_cells_cannot_be_silently_rewritten():
    assert guard_reason(record('化肥（折100%）','化肥巴（折100%）','cell'))=='table_cell_disagreement_requires_review'
    assert guard_reason(record('','0','cell'))=='table_cell_disagreement_requires_review'


def test_restrictions_and_ordinary_text_have_distinct_paths():
    assert guard_reason(record('REPORT ONLY IF','REPORT IF'))=='restriction_disagreement_requires_review'
    assert guard_reason(record('不能公开','能公开'))=='restriction_disagreement_requires_review'
    assert guard_reason(record('Date. 7/24/90','Date 7/24/90')) is None
