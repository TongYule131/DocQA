import json,sys
from scripts import run_ocr_layout


def test_default_plan_does_not_load_ocr_or_create_output(tmp_path,monkeypatch,capsys):
    inputs=tmp_path/'inputs';inputs.mkdir()
    (inputs/'plan.json').write_text(json.dumps({'pages':[{'id':'one','split':'dev'}]}),encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['run','--inputs',str(inputs),'--output',str(tmp_path/'outputs')])
    assert run_ocr_layout.main()==0
    assert json.loads(capsys.readouterr().out)['mode']=='plan_only'
    assert not (tmp_path/'outputs').exists()
