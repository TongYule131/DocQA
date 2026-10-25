"""给实际JUnit记录源码身份，禁止将旧运行重新绑定为新代码的通过证据。"""
import pytest


def pytest_addoption(parser):
    parser.addoption('--feedback-case',help='已获本题分享同意的最小回归包，仅本机')
    parser.addoption('--feedback-fixture',help='供目标定向测试实际使用的独立输入文件')


@pytest.fixture
def feedback_repro_input(request):
    """测试须实际消费返回输入，JUnit只绑定本测试而非整套报告。"""
    from hashlib import sha256
    import json
    from pathlib import Path
    def record_property(name,value):request.node.user_properties.append((name,value))
    case_path=request.config.getoption('--feedback-case');fixture_path=request.config.getoption('--feedback-fixture')
    if not case_path and not fixture_path:
        yield None
        return
    if not case_path or not fixture_path:raise ValueError('feedback case and fixture must be supplied together')
    bundle=json.loads(Path(case_path).read_text(encoding='utf-8'));content=Path(fixture_path).read_bytes()
    from app.retrieval_channels import fingerprint
    assert fingerprint(json.loads(bundle['payload_json']))==bundle['case_sha256'],'case payload identity changed'
    record_property('docqa.case_sha256',bundle['case_sha256'])
    record_property('docqa.feedback_generation',str(bundle['generation']))
    record_property('docqa.fixture_sha256',sha256(content).hexdigest())
    yield {'bundle':bundle,'input':json.loads(content)}
    after=sha256(Path(fixture_path).read_bytes()).hexdigest()
    record_property('docqa.fixture_sha256_after',after)
    assert after==sha256(content).hexdigest(),'feedback fixture changed during test'


@pytest.fixture(scope='session',autouse=True)
def record_source_identity(request,record_testsuite_property):
    if not request.config.option.xmlpath:
        yield
        return
    from app.runtime import source_identity
    before=source_identity()
    record_testsuite_property('docqa.source_fingerprint',before)
    yield
    after=source_identity()
    record_testsuite_property('docqa.source_fingerprint_after',after)
    assert before==after,'application source changed during this test run'
