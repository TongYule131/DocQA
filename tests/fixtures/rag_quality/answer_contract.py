"""CC0合成协议样本：8个独立文本、24组正反例；不是新OCR或模型质量成绩。"""
import copy


def make_cases():
    documents = [
        ("annual", "甲社2025年营业收入1280万元，数据未经审计。2026年预算收入1500万元，预算不是实际收入。",
         "2025年营业收入及口径是什么？", "营业收入为1280万元。", "1280", "9999",
         [("period", "2025年"), ("unit", "万元"), ("condition", "未经审计")]),
        ("learning", "实验共60名学生，其中48名完成两次测验；正确率从40%升至85%，只统计完成两次测验者。",
         "实验正确率和统计范围是什么？", "正确率从40%升至85%。", "85%", "95%",
         [("unit", "%"), ("scope", "只统计完成两次测验者")]),
        ("policy", "普通设备自签收之日起7日内可申请退货；定制设备不适用。",
         "退货期限和例外是什么？", "普通设备可在7日内申请退货。", "7", "17",
         [("subject", "普通设备"), ("condition", "自签收之日起"), ("condition", "定制设备不适用")]),
        ("factory", "乙厂2024年检查120件，其中合格108件；仅统计白班，夜班不在本次范围。",
         "合格数量及统计范围是什么？", "合格108件。", "108", "118",
         [("period", "2024年"), ("unit", "件"), ("scope", "仅统计白班")]),
        ("visits", "丙馆2025年服务3200人次，重复到访按多个人次记录，不能作为独立人数。",
         "服务量和人数口径如何区分？", "服务量为3200人次。", "3200", "3300",
         [("period", "2025年"), ("unit", "人次"), ("condition", "重复到访按多个人次记录")]),
        ("budget", "丁社2026年预算支出800万元，尚未执行。2025年实际支出也为800万元，已经审计。",
         "预算支出与实际支出有何区别？", "预算支出为800万元。", "800", "900",
         [("period", "2026年"), ("unit", "万元"), ("condition", "尚未执行")]),
        ("refund", "戊社2025年退款金额为0万元，该年度经营金额未经审计；补助栏空白表示未填报。",
         "退款和空白补助有什么区别？", "退款金额为0万元。", "0", "10",
         [("period", "2025年"), ("unit", "万元"), ("condition", "未经审计")]),
        ("survey", "己校受访者50人，其中40人愿意继续学习；结论仅来自本次访谈，不是盲法实验结论。",
         "愿意继续学习的人数与证据类型是什么？", "40人愿意继续学习。", "40", "49",
         [("unit", "人"), ("scope", "仅来自本次访谈"), ("condition", "不是盲法实验结论")]),
    ]
    cases=[]
    for n,(doc,text,question,fact,number,wrong,bindings) in enumerate(documents):
        positive={"status":"answered","conclusion":[{"text":fact,"refs":[1],
            "context":[{"kind":kind,"text":value,"ref":1} for kind,value in bindings]}],
            "explanation":[],"clarification_questions":[],"missing_points":[],
            "evidence_quotes":[{"ref":1,"span":1}],
            "answer_parts":[{"part_id":"p1","state":"answered","fact_indices":[0],"missing_detail":""}]}
        for mutation in ["invented_number","context_wrong_source","coverage_status"]:
            negative=copy.deepcopy(positive)
            if mutation=="invented_number":
                negative["conclusion"][0]["text"]=fact.replace(number,wrong)
            elif mutation=="context_wrong_source":
                negative["conclusion"][0]["context"][-1]["text"]="该范围已经独立验证"
            else:
                negative["status"]="partial"
                negative["missing_points"]=["没有给出正式定义。"]
            cases.append({"case_id":doc+":"+mutation,"document_id":doc,
                "split":"validation" if n>=6 else "development", "question":question,"source":text,
                "positive":positive,"negative":negative,"negative_kind":mutation,
                "required_context":bindings,"licence":"CC0-1.0",
                "boundary":"单段合成源、人工预标注，不代表独立盲测或语义自动判官"})
    return cases


def legacy_payload(payload):
    value=copy.deepcopy(payload)
    value.pop("answer_parts",None)
    for fact in value["conclusion"]+value["explanation"]:
        fact.pop("context",None)
    return value


def semantic_probes():
    """删掉真实限定但保留合法字段：语义应失败，结构通过不能算事实通过。"""
    selected={}
    for case in make_cases():
        selected.setdefault(case['document_id'],case)
    probes=[]
    for case in selected.values():
        value=copy.deepcopy(case)
        value['case_id']=case['document_id']+':omitted_qualification'
        value['negative']['conclusion']=copy.deepcopy(value['positive']['conclusion'])
        value['negative']['conclusion'][0]['context'].pop()
        value['negative_kind']='semantic_omission'
        value['requires_semantic_review']=True
        probes.append(value)
    return probes
