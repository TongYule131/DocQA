from app.rag_presentation import context_covered,fact_text


def test_outer_layout_whitespace_does_not_duplicate_an_already_written_condition():
    condition=' \n61.8% 的教师存在迁移障碍， 导致岗位任务脱节 '
    text='报告指出61.8%的教师存在迁移障碍，导致岗位任务脱节。'
    assert context_covered(condition,text)
    fact={'text':text,'refs':[1],'context':[{'kind':'scope','text':condition}]}
    assert fact_text(fact)==text+'[1]'


def test_a_missing_condition_is_still_displayed_verbatim():
    condition=' 仅限2025年预测值，未经审计 '
    fact={'text':'金额为7亿元。','refs':[1],'context':[{'kind':'scope','text':condition}]}
    assert not context_covered(condition,fact['text'])
    assert condition in fact_text(fact) and '未经审计' in fact_text(fact)
