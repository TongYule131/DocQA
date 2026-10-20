import hashlib,json,sys
from pathlib import Path

import httpx
import pytest

from scripts import convert_with_ocr_profile,download_ocr_medium


def test_download_defaults_to_plan_with_no_network_or_directory(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['download','--output',str(tmp_path/'models')])
    monkeypatch.setattr(download_ocr_medium.httpx,'Client',lambda **kw:pytest.fail('计划阶段禁止网络'))
    assert download_ocr_medium.main()==0
    assert json.loads(capsys.readouterr().out)['mode']=='plan_only'
    assert not (tmp_path/'models').exists()


def test_model_download_checks_sha_and_never_overwrites_failure(tmp_path,monkeypatch):
    expected=b'expected weights';digest=hashlib.sha256(expected).hexdigest()
    monkeypatch.setattr(download_ocr_medium,'MODELS',{'model.onnx':('det',digest)})
    monkeypatch.setattr(sys,'argv',['download','--output',str(tmp_path),'--download'])
    real=httpx.Client
    calls=[]
    def reply(request):calls.append(str(request.url));return httpx.Response(200,content=b'wrong weights')
    monkeypatch.setattr(download_ocr_medium.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(reply),**kw))
    with pytest.raises(ValueError,match='SHA256'):download_ocr_medium.main()
    assert (tmp_path/'model.download').read_bytes()==b'wrong weights'
    assert not (tmp_path/'model.onnx').exists()
    with pytest.raises(ValueError,match='未完成下载'):download_ocr_medium.main()
    assert len(calls)==1


def test_existing_valid_model_does_not_download(tmp_path,monkeypatch):
    content=b'valid model';(tmp_path/'model.onnx').write_bytes(content)
    monkeypatch.setattr(download_ocr_medium,'MODELS',{'model.onnx':('det',hashlib.sha256(content).hexdigest())})
    monkeypatch.setattr(sys,'argv',['download','--output',str(tmp_path),'--download'])
    monkeypatch.setattr(download_ocr_medium.httpx,'Client',lambda **kw:pytest.fail('已校验权重不下载'))
    assert download_ocr_medium.main()==0


@pytest.mark.parametrize('start,end,suffix',[(0,1,'pdf'),(2,1,'pdf'),(1,5,'pdf'),(1,1,'txt')])
def test_converter_refuses_unsafe_ranges_before_model_load(tmp_path,monkeypatch,start,end,suffix):
    monkeypatch.setattr(sys,'argv',['convert','--input',str(tmp_path/f'source.{suffix}'),
        '--models',str(tmp_path/'models'),'--output',str(tmp_path/'result'),
        '--start-page',str(start),'--end-page',str(end)])
    with pytest.raises(ValueError):convert_with_ocr_profile.main()
    assert not (tmp_path/'result').exists()
